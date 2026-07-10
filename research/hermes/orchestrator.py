# research/hermes/orchestrator.py
"""Talos Foundry orchestrator (Phase 1D) — wires 1B->1C->1A->1E with budget +
early stopping, triggered write-file->reconcile (never inline)."""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.hermes.candidate_store import CANDIDATE_SUBDIR, _candidate_path, write_candidate
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.evidence_store import upsert_card
from research.hermes.forge import forge
from research.hermes.gatekeeper import evaluate
from research.hermes.sandbox import SandboxExecutor
from research.lib.factor_io import _atomic_to_parquet, _symbol_short
from research.lib.research_ledger import append_event

MAX_FORGE_RETRIES = 3          # P5: bounded repair, then bury


def make_run_sandbox(sandbox: SandboxExecutor, scratch_dir: str | Path):
    """Adapt DockerSandbox.run into forge's run(code, panel)->Series.
    Writes panel to a temp parquet, runs the sandbox, reads the single-column
    candidate.parquet back and re-attaches panel's index."""
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    def run(code: str, panel: pd.DataFrame) -> pd.Series:
        # TemporaryDirectory guarantees cleanup (happy path + exceptions) so
        # per-hypothesis scratch dirs don't accumulate across a nightly run.
        with tempfile.TemporaryDirectory(dir=scratch) as job:
            in_path = Path(job) / "in.parquet"
            panel.to_parquet(in_path)
            out_path = sandbox.run(code, input_parquet=str(in_path), output_dir=str(job))
            out = pd.read_parquet(out_path)
            series = out.iloc[:, 0]
        # Positional correspondence only: this assumes the sandboxed compute()
        # preserved row order/count. A same-length reorder would NOT be caught.
        if len(series) != len(panel):
            raise ValueError(
                f"sandbox output length {len(series)} != panel length {len(panel)}; "
                "cannot restore index"
            )
        series.index = panel.index               # runner drops index; restore it
        return series

    return run


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _graveyard_path(symbol, manifests_dir) -> Path:
    """Dead factors' VALUES live next to the candidates, never in production."""
    return Path(manifests_dir) / CANDIDATE_SUBDIR / f"graveyard_{_symbol_short(symbol)}.parquet"


def _merge_column(existing: "pd.DataFrame | None", name: str, series) -> pd.DataFrame:
    """Add/replace one column, aligning on the union of indexes.

    Read-then-write, no lock: assumes a single-writer nightly-batch model
    (one process, one factor at a time) — same pre-existing pattern as
    evidence_store.upsert_card, not a new risk. A concurrent writer to the
    same candidate/graveyard parquet would race; Task 5's job enqueueing
    should keep foundry runs serialized per symbol."""
    frame = pd.DataFrame({name: series})
    if existing is None or existing.empty:
        return frame
    return existing.drop(columns=[name], errors="ignore").join(frame, how="outer")


def _merge_into_candidates(symbol, manifests_dir, factor_id, series) -> None:
    """agy 3b: write_candidate replaces the WHOLE parquet (os.replace). Read-merge-write
    so a nightly sweep keeps every passing factor, not just the last one."""
    path = _candidate_path(symbol, Path(manifests_dir))
    existing = pd.read_parquet(path) if path.exists() else None
    write_candidate(_merge_column(existing, factor_id, series), symbol, manifests_dir)


def _merge_into_graveyard(symbol, manifests_dir, factor_id, series) -> None:
    """agy 3c: persist DEAD factor values so 1A nearest_correlate can numerically
    dedup against the graveyard (C-6). Nothing else reads this file."""
    path = _graveyard_path(symbol, manifests_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_parquet(path) if path.exists() else None
    _atomic_to_parquet(_merge_column(existing, factor_id, series), path)


def process_hypothesis(hyp, panel, ohlcv, daily_regime, existing_and_dead,
                       symbol, manifests_dir, cfg, llm, run_sandbox) -> str:
    """Run one hypothesis through forge -> evaluate -> ledger -> EvidenceCard,
    merging the outcome into the candidate or graveyard parquet.

    Returns one of three outcome strings (Task 4's run_foundry/should_early_stop
    compare against these exact literals for budget + early-stop bookkeeping):
      "forge_failed" - forge() exhausted its repair retries; no clean series was
                       ever produced, so only a graveyard EvidenceCard is written
                       (nothing to persist numerically for nearest_correlate).
      "candidate"    - forge succeeded and evaluate() passed the statistical
                       gate; the factor's series is merged into
                       candidate_features/cand_<sym>.parquet and its card is
                       upserted with verdict=candidate.
      "rejected"     - forge succeeded but evaluate() failed the gate; the
                       factor's series is merged into
                       candidate_features/graveyard_<sym>.parquet (so future
                       nearest_correlate dedup can compare against it) and its
                       card is upserted with verdict=graveyard.
    """
    fr = forge(hyp, llm, run_sandbox, panel, max_retries=MAX_FORGE_RETRIES)
    code_sha = hashlib.sha256((fr.code or "").encode()).hexdigest()
    common = dict(factor_id=hyp.id, symbol=symbol, source=hyp.source,
                  code_sha256=code_sha, generated_at=_now(), trial_step=fr.attempts,
                  interval=cfg.interval, formula=hyp.description,
                  rationale=f"foundry {hyp.source}")

    if not fr.success:
        # no series exists (code never ran clean) -> card only, nothing to bury numerically
        upsert_card(EvidenceCard(**common, verdict=VERDICT_GRAVEYARD,
                                 death_reason=fr.death_reason or "forge failed"),
                    symbol, manifests_dir)
        return "forge_failed"

    res = evaluate(fr.series, ohlcv, daily_regime, existing_and_dead, symbol, manifests_dir, cfg)
    # write factor_trial AFTER evaluate: foundry_dsr already appends the CURRENT
    # trial in-memory, so pre-writing it would double-count (1A agy-3 #3).
    append_event(manifests_dir, kind="factor_trial", symbol=symbol,
                 detail={"sr_per_bar": res.metrics.get("ir"), "interval": cfg.interval,
                         "factor_id": hyp.id})
    m = res.metrics
    card = EvidenceCard(
        **common,
        gross_ic=m["gross_ic"], ic_nonoverlap=m["ic_nonoverlap"], ir=m["ir"],
        dsr=m["dsr"], pbo=m["pbo"], turnover=m["turnover"], n_samples=m["n_samples"],
        regime_ic=m["regime_ic"], yearly_ic=m["yearly_ic"],
        nearest_factor=m["nearest_factor"], nearest_abs_spearman=m["nearest_abs_spearman"],
        verdict=VERDICT_CANDIDATE if res.passed else VERDICT_GRAVEYARD,
        death_reason=None if res.passed else res.rejection_reason)

    if res.passed:
        _merge_into_candidates(symbol, manifests_dir, hyp.id, fr.series)
    else:
        _merge_into_graveyard(symbol, manifests_dir, hyp.id, fr.series)
    upsert_card(card, symbol, manifests_dir)
    return "candidate" if res.passed else "rejected"


@dataclass(frozen=True)
class Budget:
    max_factors: int = 50           # per-run cap on hypotheses tried
    early_stop_after: int = 8       # consecutive non-candidate outcomes -> stop the night


def should_early_stop(outcomes: list, budget: Budget) -> bool:
    """True once the tail has `early_stop_after` consecutive non-candidate results
    (P5: don't burn the nightly budget once the run is clearly diverging). A
    candidate resets the streak."""
    streak = 0
    for o in reversed(outcomes):
        if o == "candidate":
            break
        streak += 1
    return streak >= budget.early_stop_after
