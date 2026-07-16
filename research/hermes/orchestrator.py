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

import numpy as np
import pandas as pd

from research.hermes.candidate_store import CANDIDATE_SUBDIR, _candidate_path, write_candidate, write_candidate_code
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.evidence_store import load_cards, upsert_card
from research.hermes.field_schema import load_field_schema, reconcile_schema
from research.hermes.forge import forge, BudgetExhausted, ForgeBudget
from research.hermes.gatekeeper import evaluate, forward_returns, gross_ic, GateConfig
from research.hermes.hypothesis import SOURCE_LLM
from research.hermes.hypothesis_queue import DEFAULT_SOURCES, build_queue
from research.hermes.ideator import generate_ideas, summarize_deaths
from research.hermes.sandbox import SandboxExecutor
from research.hermes.split import foundry_split
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

    # Provenance for the code store: the bridge compares this against the image
    # it re-runs under, and a drifted image shows up as a reconciliation failure.
    run.image_id = getattr(sandbox, "image", None)
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
                       symbol, manifests_dir, cfg, llm, run_sandbox,
                       forge_budget=None) -> str:
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
    fr = forge(hyp, llm, run_sandbox, panel, max_retries=MAX_FORGE_RETRIES,
               budget=forge_budget)
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
        # Persist the SOURCE, not just its sha: the bridge must re-run this exact
        # code over the full span (Foundry only ever computed it pre-oos).
        # horizon_h is recorded per factor because gross_ic on the card was
        # measured at THIS horizon — the bridge must reuse it, not the config's first.
        write_candidate_code(hyp.id, symbol, manifests_dir, fr.code or "", {
            "code_sha256": code_sha,
            "base_image_id": getattr(run_sandbox, "image_id", None),
            "interval": cfg.interval,
            "horizon_h": cfg.horizon_h,
        })
    else:
        _merge_into_graveyard(symbol, manifests_dir, hyp.id, fr.series)
    upsert_card(card, symbol, manifests_dir)
    return OUTCOME_CANDIDATE if res.passed else OUTCOME_REJECTED


@dataclass(frozen=True)
class Budget:
    # 20, not 50: the queue is llm-only now, and the ideator asks for 25 ideas per
    # run (a few die in validation). 50 was sized for a 456-strong zoo queue.
    max_factors: int = 20           # per-run cap on hypotheses tried
    # Effectively OFF (>= max_factors). early_stop_after existed to bail out of a
    # diverging night and move the budget on -- but with a single source there is
    # nothing to move on TO, so it would only truncate the sample. The first runs
    # exist precisely to MEASURE idea quality, and 8 outcomes cannot tell "the
    # ideas are bad" from "the draw was unlucky".
    early_stop_after: int = 20      # consecutive non-candidate outcomes -> stop the night
    # early_stop_after only fires BETWEEN hypotheses; a single hypothesis whose
    # code keeps failing spends one LLM call per retry. max_llm_calls is the
    # run-wide circuit breaker charged inside forge()'s repair loop AND by
    # generate_ideas. Unchanged: this is the real circuit breaker.
    max_llm_calls: int = 60


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
        # utc=True: the foundry panel/ohlcv index is tz-aware UTC, so regime
        # labels must be too or regime_ic's alignment raises "Cannot compare
        # dtypes datetime64 and datetime64[..., UTC]".
        dates = pd.to_datetime([row["date"] for row in breakdown], utc=True)
        labels = [row["regime"] for row in breakdown]
        return pd.Series(labels, index=dates).sort_index()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.warning("failed to parse regime manifest for %s (%s); falling back to neutral",
                    symbol, e)
        return None


def _preoos_top_features(panel, ohlcv, cfg, top_k: int = 5) -> list:
    """Rank the panel's feature columns by |gross IC| at the gate's own horizon
    and entry lag, and return the top_k names.

    Foundry must NOT pick its derived-hypothesis bases from evidence_<sym>.json:
    stage0a computes that file's IC over the full history, including the reserved
    OOS window, so choosing from it leaks OOS information into the search itself.
    `panel` here is already pre-oos (see the OOS lock in run_foundry).
    """
    feature_cols = [c for c in panel.columns
                    if c not in _OHLCV_COLS and pd.api.types.is_numeric_dtype(panel[c])]
    if not feature_cols:
        return []                                   # nothing to rank; skip the fwd-return compute
    fwd, _ = forward_returns(ohlcv, cfg)            # shared with evaluate(); handles the 1D case
    scored: list = []
    for col in feature_cols:
        ic = gross_ic(panel[col].shift(cfg.entry_lag), fwd)   # same lag the gate applies
        if np.isfinite(ic):
            scored.append((abs(ic), col))
    scored.sort(key=lambda t: (-t[0], t[1]))                  # deterministic tie-break
    return [col for _, col in scored[:top_k]]


def _align_ohlcv(ohlcv, feature_index, min_coverage: float = 0.95):
    """Reindex the price table onto the feature index — features are ground truth.

    agy: features_<sym>.parquet is written by stage0a and deliberately holds no
    OHLCV; candles come from a separate source (lib.okx_data.fetch_candles), so
    the two tables can differ in length, tail, and missing bars. Letting a
    misaligned price table into the engine produces NaN forward returns, which
    turnover_of() then reads as flat->position->flat churn and max_turnover
    wrongly rejects the factor. Align first, and refuse a price table that does
    not actually cover the features.
    """
    if "close" not in ohlcv.columns:
        raise ValueError("injected ohlcv has no 'close' column; cannot evaluate")
    aligned = ohlcv.reindex(feature_index)
    coverage = float(aligned["close"].notna().mean())
    if coverage < min_coverage:
        raise ValueError(
            f"ohlcv coverage {coverage:.1%} of the feature index is below "
            f"{min_coverage:.0%}; the price table does not align with the features"
        )
    return aligned


def run_foundry(symbol, manifests_dir, cfg, llm, sandbox, budget, zoo_dir=None, *,
                oos_start, ohlcv, val_frac=0.2, derived_top_k=5,
                daily_regime=None, run_sandbox=None, forge_budget=None,
                pause_file=None, sources=DEFAULT_SOURCES, n_ideas=25) -> dict:
    """Sweep the hypothesis queue for one symbol under Budget + early stopping.

    zoo_dir is optional: zoo is off by default (see hypothesis_queue.DEFAULT_SOURCES).
    It is required only when 'zoo' is in `sources`, and build_queue checks that.
    oos_start is REQUIRED and keyword-only: the pipeline's
    walk-forward OOS window is reserved, so Foundry must never let forge() or
    evaluate() see index >= oos_start. Passing it explicitly (dependency
    injection) keeps run_foundry a pure function of its inputs; the caller reads
    research_config.yaml.

    ohlcv is REQUIRED and keyword-only: features_<sym>.parquet holds features
    only (stage0a keeps price data in the candle source), so a price table must
    be injected by the caller. It is NOT sliced out of the feature panel.
    """
    features_full = load_features(symbol, manifests_dir=manifests_dir)
    ohlcv_full = _align_ohlcv(ohlcv, features_full.index)

    # OOS lock. foundry_split slices to pre-oos rows and returns (train, val);
    # we hand the gate the whole pre-oos window (the LLM never sees gate metrics,
    # so there is nothing to overfit to train -- see the note below). The second,
    # strict call is defence-in-depth: it raises OOSLeakError if a single row
    # >= oos_start somehow survived the cut.
    train, val = foundry_split(features_full, oos_start, val_frac=val_frac)
    features = pd.concat([train, val])
    foundry_split(features, oos_start, val_frac=val_frac, strict=True)
    ohlcv = ohlcv_full.loc[features.index]        # same cut, already aligned
    log.info("%s: OOS lock -> %d/%d bars kept (< %s); train=%d val=%d",
             symbol, len(features), len(features_full), oos_start, len(train), len(val))

    # forge's panel = features + price. 258/301 zoo alphas require `close` and
    # 171 require `volume`, so hiding OHLCV from the sandbox would kill the zoo
    # source outright. The constitution bans the INTRADAY price-derived class
    # (sub-1H microstructure artefacts, enforced by 1B's theme->dead_class map
    # and by cfg.interval's 1H floor), not price-derived factors as such.
    panel = features.join(ohlcv, how="left")

    # dedup correlates are computed against FEATURES only: price columns are
    # context, not rival factors.
    existing = features

    # agy 3c: include buried factor VALUES so nearest_correlate can dedup vs the graveyard
    gpath = _graveyard_path(symbol, manifests_dir)
    if gpath.exists():
        # reindex onto the pre-oos index: a dedup correlate must not be computed
        # over rows the gate is forbidden to look at.
        existing = existing.join(
            pd.read_parquet(gpath).reindex(features.index), how="outer", rsuffix="_dead")

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

    # rank FEATURES only (never the injected price columns) on the pre-oos window
    derived_bases = _preoos_top_features(features, ohlcv, cfg, top_k=derived_top_k)
    log.info("%s: derived bases ranked on pre-oos data: %s", symbol, derived_bases)

    # One breaker for the whole sweep, charged inside forge()'s repair loop AND by
    # generate_ideas. Must exist BEFORE ideation: an ideation call is real spend.
    # A caller sweeping multiple jobs (reconcile) can pass a shared ForgeBudget so
    # spend is counted across the whole batch, not reset per job.
    forge_budget = forge_budget or ForgeBudget(max_llm_calls=budget.max_llm_calls)

    # ── ideation ─────────────────────────────────────────────────────────────
    # This is what was missing: llm_raw was hardcoded to [], so the LLM never
    # proposed anything and the queue was 100% zoo -- 456 equity alphas that only
    # read close/volume, on a panel whose crypto-native columns (funding/basis/OI/
    # long-short) no hypothesis had ever touched.
    ideation_failed = None
    llm_raw: list = []
    ideas_rejected: list = []
    if SOURCE_LLM in sources:
        # Intersect the hand-written schema with the panel's REAL columns. The test
        # asserts these match exactly; the runtime only warns, because stage0a
        # adding a column must not crash that night's cron (spec §6).
        schema, undocumented, stale = reconcile_schema(load_field_schema(), features.columns)
        if undocumented or stale:
            log.warning("%s: field_schema drift -- undocumented panel columns %s, "
                        "stale schema entries %s; offering the LLM only the %d "
                        "columns that are in both", symbol, undocumented, stale, len(schema))
        # Deaths are fed as CATEGORIES with no numbers attached: handing the LLM
        # "IC 0.029 < 0.03" invites it to bolt on a log() until the bar clears,
        # which is automated p-hacking (spec §4.1).
        deaths = summarize_deaths(load_cards(symbol, manifests_dir))
        llm_raw, ideas_rejected, ideation_failed = generate_ideas(
            llm, schema, deaths, n_ideas=n_ideas, budget=forge_budget)
        if ideation_failed:
            log.warning("%s: ideation produced nothing usable: %s", symbol, ideation_failed)

    queue = build_queue(symbol=symbol, manifests_dir=manifests_dir, zoo_dir=zoo_dir,
                        llm_raw=llm_raw, derived_bases=derived_bases,
                        sources=sources)[: budget.max_factors]
    outcomes: list = []
    budget_exhausted = False
    summary_paused = False
    # `existing` is captured once above and never updated per-iteration: a factor
    # that passes/fails mid-sweep is NOT deduped against by later factors in the
    # SAME run. Deliberate choice (plan Task 4) to keep the loop simple; same-night
    # self-dedup is deferred to the next run, which reloads candidates+graveyard.
    for hyp in queue:
        if pause_file and Path(pause_file).exists():
            log.warning("%s: killswitch pause file present — stopping the sweep", symbol)
            budget_exhausted = False
            summary_paused = True
            break
        try:
            outcomes.append(process_hypothesis(hyp, panel, ohlcv, daily_regime, existing,
                                               symbol, manifests_dir, cfg, llm, run_sb,
                                               forge_budget=forge_budget))
        except BudgetExhausted as exc:
            # not a factor verdict: the run ran out of LLM calls. Stop cleanly and
            # say so in the summary rather than crashing a nightly cron.
            log.warning("%s: %s — stopping the sweep after %d hypotheses",
                        symbol, exc, len(outcomes))
            budget_exhausted = True
            break
        if should_early_stop(outcomes, budget):
            break
    # seed all three outcome literals at 0 so callers/tests can always index
    # summary["candidate"]/["rejected"]/["forge_failed"] without a KeyError,
    # even on a night where one outcome never occurred.
    summary = Counter({OUTCOME_FORGE_FAILED: 0, OUTCOME_CANDIDATE: 0, OUTCOME_REJECTED: 0})
    summary.update(outcomes)
    summary = dict(summary)
    summary["llm_calls_used"] = forge_budget.used
    if budget_exhausted:
        summary["budget_exhausted"] = True
    summary["queue_composition"] = dict(Counter(h.source for h in queue))
    summary["features_range"] = [str(features.index.min()), str(features.index.max())]
    if summary_paused:
        summary["killswitch_paused"] = True
    summary["ideas_accepted"] = len(llm_raw)
    summary["ideas_rejected"] = ideas_rejected
    if ideation_failed:
        summary["ideation_failed"] = ideation_failed
    return summary


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


def run_foundry_job(job_path, manifests_dir, llm, sandbox, zoo_dir, ohlcv, budget=None,
                    forge_budget=None, pause_file=None) -> dict:
    """Foundry runner reconcile: read job.json, run_foundry, mark done.

    On any exception from run_foundry, the job file is rewritten with
    status="failed" + error + finished_at (mirroring pipeline_manager.py's
    convention) BEFORE the exception is re-raised, so a crashed run leaves a
    diagnostic trail instead of sitting at status="queued" forever."""
    job_path = Path(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    p = job["params"]
    cfg = GateConfig(interval=p.get("interval", "1H"), horizon_h=p.get("horizon_h", 24))
    # oos_start has no default: a job that forgets it must fail loudly rather
    # than silently let Foundry evaluate factors on the reserved OOS window.
    if "oos_start" not in p:
        raise KeyError(f"foundry job {job.get('job_id')} params missing 'oos_start' (OOS lock)")
    try:
        summary = run_foundry(job["symbol"], manifests_dir, cfg, llm, sandbox,
                              budget or Budget(), zoo_dir=zoo_dir, ohlcv=ohlcv,
                              oos_start=p["oos_start"], val_frac=p.get("val_frac", 0.2),
                              forge_budget=forge_budget, pause_file=pause_file)
    except Exception as e:
        job["status"] = "failed"; job["error"] = str(e); job["finished_at"] = _now()
        _write_job_json(job_path, job)
        raise
    job["status"] = "done"; job["summary"] = summary; job["finished_at"] = _now()
    _write_job_json(job_path, job)
    return summary
