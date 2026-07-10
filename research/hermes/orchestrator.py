# research/hermes/orchestrator.py
"""Talos Foundry orchestrator (Phase 1D) — wires 1B->1C->1A->1E with budget +
early stopping, triggered write-file->reconcile (never inline)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.hermes.candidate_store import CANDIDATE_SUBDIR, _candidate_path, write_candidate
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.evidence_store import upsert_card
from research.hermes.forge import forge
from research.hermes.gatekeeper import evaluate, GateConfig
from research.hermes.hypothesis_queue import build_queue
from research.hermes.sandbox import SandboxExecutor
from research.lib.factor_io import _atomic_to_parquet, _symbol_short, load_features
from research.lib.research_ledger import append_event

log = logging.getLogger(__name__)

_OHLCV_COLS = ("open", "high", "low", "close", "volume")

MAX_FORGE_RETRIES = 3          # P5: bounded repair, then bury

# process_hypothesis's return literals / should_early_stop's comparison /
# run_foundry's Counter seed all reference these constants (not bare string
# literals) so a future rename is a one-place edit that fails loudly
# (NameError) instead of drifting silently across three call sites.
OUTCOME_FORGE_FAILED = "forge_failed"
OUTCOME_CANDIDATE = "candidate"
OUTCOME_REJECTED = "rejected"


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
        return OUTCOME_FORGE_FAILED

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
    return OUTCOME_CANDIDATE if res.passed else OUTCOME_REJECTED


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
        if o == OUTCOME_CANDIDATE:
            break
        streak += 1
    return streak >= budget.early_stop_after


def _load_daily_regime(symbol, manifests_dir) -> "pd.Series | None":
    """Load a precomputed daily regime series from regime_<sym>.json (stage 2.5
    output), if present and well-formed. Returns None (caller falls back to an
    all-neutral series) rather than raising — regime_ic is informational only
    in gatekeeper.evaluate, so a missing/malformed regime file should degrade
    gracefully, not crash a foundry run."""
    path = Path(manifests_dir) / f"regime_{_symbol_short(symbol)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        breakdown = data["breakdown"]
        if not breakdown:
            return None
        dates = pd.to_datetime([row["date"] for row in breakdown])
        labels = [row["regime"] for row in breakdown]
        return pd.Series(labels, index=dates).sort_index()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.warning("failed to parse regime manifest for %s (%s); falling back to neutral",
                    symbol, e)
        return None


def run_foundry(symbol, manifests_dir, cfg, llm, sandbox, budget, zoo_dir,
                daily_regime=None, run_sandbox=None) -> dict:
    """Sweep the hypothesis queue for one symbol under Budget + early stopping.
    zoo_dir is REQUIRED (agy 4c: build_queue does Path(zoo_dir).rglob -> Path(None)
    raises TypeError)."""
    panel = load_features(symbol, manifests_dir=manifests_dir)
    ohlcv = panel[[c for c in _OHLCV_COLS if c in panel.columns]]
    if "close" not in ohlcv.columns:                # agy 4a: evaluate hard-depends on close
        raise ValueError(f"feature panel for {symbol} has no 'close' column; cannot evaluate")
    existing = panel.drop(columns=list(ohlcv.columns), errors="ignore")

    # agy 3c: include buried factor VALUES so nearest_correlate can dedup vs the graveyard
    gpath = _graveyard_path(symbol, manifests_dir)
    if gpath.exists():
        existing = existing.join(pd.read_parquet(gpath), how="outer", rsuffix="_dead")

    run_sb = run_sandbox or make_run_sandbox(sandbox, Path(manifests_dir) / "_foundry_scratch")
    if daily_regime is None:
        daily_regime = _load_daily_regime(symbol, manifests_dir)
        if daily_regime is not None:
            log.info("loaded daily_regime for %s from regime_%s.json (%d bars)",
                     symbol, _symbol_short(symbol), len(daily_regime))
        else:
            # agy 4b: regime_ic expects DAILY labels (it ffills onto the factor index);
            # a panel-frequency fallback would violate that contract.
            log.warning("daily_regime not supplied for %s; falling back to all-neutral", symbol)
            daily_idx = panel.index.normalize().unique()
            daily_regime = pd.Series("neutral", index=daily_idx)

    queue = build_queue(symbol=symbol, manifests_dir=manifests_dir,
                        zoo_dir=zoo_dir, llm_raw=[])[: budget.max_factors]
    outcomes: list = []
    # `existing` is captured once above and never updated per-iteration: a factor
    # that passes/fails mid-sweep is NOT deduped against by later factors in the
    # SAME run. Deliberate choice (plan Task 4) to keep the loop simple; same-night
    # self-dedup is deferred to the next run, which reloads candidates+graveyard.
    for hyp in queue:
        outcomes.append(process_hypothesis(hyp, panel, ohlcv, daily_regime, existing,
                                           symbol, manifests_dir, cfg, llm, run_sb))
        if should_early_stop(outcomes, budget):
            break
    # seed all three outcome literals at 0 so callers/tests can always index
    # summary["candidate"]/["rejected"]/["forge_failed"] without a KeyError,
    # even on a night where one outcome never occurred.
    summary = Counter({OUTCOME_FORGE_FAILED: 0, OUTCOME_CANDIDATE: 0, OUTCOME_REJECTED: 0})
    summary.update(outcomes)
    return dict(summary)


def _write_job_json(path, data: dict) -> None:
    """Atomic write (mirrors dashboard/server/pipeline_jobs.write_job): a reader
    polling job.json must never observe a partially-written file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def enqueue_foundry_job(symbol, runs_dir, params: dict) -> Path:
    """Write a queued foundry job (write-file->reconcile). Talos NEVER inline-runs;
    a separate foundry runner picks this up. Called by nightly cron / on-demand."""
    job_id = f"foundry_{symbol}_{uuid.uuid4().hex[:8]}"
    job_dir = Path(runs_dir) / "foundry_jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job_path = job_dir / "job.json"
    _write_job_json(job_path, {"job_id": job_id, "symbol": symbol, "params": params,
                               "status": "queued", "created_at": _now()})
    return job_path


def run_foundry_job(job_path, manifests_dir, llm, sandbox, zoo_dir, budget=None) -> dict:
    """Foundry runner reconcile: read job.json, run_foundry, mark done.

    On any exception from run_foundry, the job file is rewritten with
    status="failed" + error + finished_at (mirroring pipeline_manager.py's
    convention) BEFORE the exception is re-raised, so a crashed run leaves a
    diagnostic trail instead of sitting at status="queued" forever."""
    job_path = Path(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    p = job["params"]
    cfg = GateConfig(interval=p.get("interval", "1H"), horizon_h=p.get("horizon_h", 24))
    try:
        summary = run_foundry(job["symbol"], manifests_dir, cfg, llm, sandbox,
                              budget or Budget(), zoo_dir=zoo_dir)
    except Exception as e:
        job["status"] = "failed"; job["error"] = str(e); job["finished_at"] = _now()
        _write_job_json(job_path, job)
        raise
    job["status"] = "done"; job["summary"] = summary; job["finished_at"] = _now()
    _write_job_json(job_path, job)
    return summary
