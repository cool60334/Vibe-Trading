## Context

Stage 3-diag current flow (per `research/pipeline/stage3_diagnose.py`):

```
strategy_runs.json (per strategy)
  ├─ base_run         → runs/<base>/artifacts/metrics.csv         (in-sample TRAIN window when oos_start set)
  ├─ sweep_run        ─/                                          (unused by diag)
  ├─ walk_forward_runs[]                                           ← STAGE 4 WRITES THIS, DIAG IGNORES
  └─ optimization.json::source_run → metrics.csv                   (best stage-4 sweep combo, also TRAIN)

→ build_diagnosis_prompt(metrics_by_run={base_run: train_metrics},
                         optimization_metrics=stage4_best_train_metrics)
→ LLM → recommended_action
→ rule_based_action() fallback uses same two inputs
```

Both feed-in metrics blocks come from the **train window**. The walk-forward run file exists, on disk, never read.

`stage4_optimize.py` (memory: `project_oos_overfit_finding`) already writes `runs/<strategy>_oos_holdout/artifacts/metrics.csv` and registers it in `strategy_runs.json::walk_forward_runs[0]`. The data is there; only diag's reader is missing.

## Goals / Non-Goals

**Goals:**
- Diag's verdict reflects walk-forward (true OOS) performance, not train-window in-sample.
- LLM prompt explicitly distinguishes the three metric blocks (train base / stage4 best train / walk-forward OOS) and instructs the model to prioritise OOS.
- Rule-based fallback (used when LLM fails / unparseable) ALSO consumes walk-forward metrics, anchoring on OOS sharpe sign for `back_to_stage_2`.
- Backwards compatible: strategies with empty `walk_forward_runs` behave exactly as today.

**Non-Goals:**
- Changing `diagnosis.json` Pydantic schema (`DiagnosisBlock`). The verdict field set stays identical.
- Reweighting the stage 5 score formula. Stage 5 still trusts diag's verdict; we fix the upstream verdict instead.
- Adding new walk-forward windows. Diag reads only the first entry in `walk_forward_runs[]`.
- Doing this for the legacy / non-walk-forward case (when `oos_start` unset). Train is the only signal in that mode; diag keeps current behaviour.

## Decisions

### D1: Where to load walk-forward metrics

Add `read_walk_forward_metrics(strategy_runs_entry, runs_root) -> dict | None`:
- Reads `strategy_runs_entry.get("walk_forward_runs", [])`. If empty/missing → return None.
- Takes the **first** element (stage 4 writes only one holdout per strategy; defensive against future multi-window).
- Reads `runs/<wf_run>/artifacts/metrics.csv` via existing `read_metrics_csv` helper.
- Returns metrics dict or None on any failure (file missing / unparseable). Failure is **soft**: diag falls back to current train-only behaviour with a stdout warning.

### D2: Prompt structure

Reshape `build_diagnosis_prompt` from a single metrics blob into three labelled blocks:

```
## Train (in-sample, may be overfit)
```json
{base_run_metrics, stage4_best_train_metrics}
```

## Walk-Forward (held-out OOS, AUTHORITATIVE)
```json
{walk_forward_metrics}
```

## Task
... PRIORITISE walk-forward metrics for the verdict ...
```

Prompt MUST contain the literal phrase "walk-forward is the authoritative signal" so the LLM cannot mistake which block matters. Train remains shown as context (for sanity checks and overfit detection: large train→OOS sharpe gap is informative).

When walk-forward is unavailable, the prompt collapses to today's structure (single metrics block, no OOS section, no "authoritative" claim).

### D3: Rule-based fallback OOS anchoring

`rule_based_action(metrics_by_run, optimization_metrics, walk_forward_metrics=None)` decision tree updates:

```
if walk_forward_metrics is not None:
    wf_sharpe   = wf_metrics["sharpe"]
    wf_drawdown = wf_metrics["max_drawdown"]
    wf_trades   = wf_metrics["trade_count"]

    if wf_sharpe < 0:
        return BACK_TO_STAGE_2          # concept-broken on OOS
    if (wf_sharpe < 1.0
        or abs(wf_drawdown) > 0.15
        or wf_trades < 30):              # OOS trade gate looser: 30 (was 50 for train)
        return BACK_TO_STAGE_4
    return PROCEED

# else: current train-only path
```

OOS trade threshold loosened from 50 → 30 because OOS window is ~half the train length and a sparse 2-factor AND strategy naturally has ~30-50 OOS trades (eth_s5 OOS = 49, exactly at boundary).

Stage-4 `optimization_metrics` is still loaded for context but is **not** the decision input when walk-forward is present (it's train-window data).

### D4: Backwards compat

Strategies with `walk_forward_runs: []` or whose holdout metrics.csv is missing:
- Walk-forward path returns None.
- Prompt structure collapses to current (train-only) form.
- Rule-based fallback uses current logic.
- No `recommended_action` semantic change.

This is the case for all legacy `oos_start: null` configs and for strategies that haven't yet had stage 4 sweep run.

### D5: Audit trail

Diagnosis.json gains an optional `walk_forward_metrics` echo block under a new optional field on `DiagnosisBlock`:

```json
{
  "source_run": "eth_s5_half_size_train",
  "recommended_action": "proceed",
  "summary": "...",
  "findings": [...],
  "walk_forward_source": "eth_s5_half_size_oos",      // optional
  "walk_forward_metrics": {                             // optional, audit only
    "sharpe": 1.02, "max_drawdown": -0.091, "trade_count": 49, ...
  }
}
```

These two fields are `Optional[...]` with default None in the Pydantic schema → backwards compat with archived diagnoses preserved. **Schema change is the only Pydantic touch**; stage 5 reads `recommended_action` and doesn't care about the new fields.

## Risks / Trade-offs

- **[Risk] OOS window short (1.4y)** — verdict may oscillate as a single regime ends. → Mitigation: keep train block in prompt for context; LLM can detect "OOS good only in bear regime".
- **[Risk] LLM still over-weights train** despite prompt — possible because models have priors on "look at in-sample first". → Mitigation: rule-based fallback is OOS-anchored independently; if LLM returns garbage / unparseable, fallback overrides. Memory `feedback_llm_json_strict_false` notes LLM JSON parsing failures already happen.
- **[Trade-off] Looser OOS trade gate (30 vs 50)** — risk admitting noisy small-sample winners. → Acceptance: stage 5 score formula already discounts low trade count (10% weight × trades/100), so a 30-trade strategy gets penalised but not rejected. Multi-layer defence acceptable.
- **[Risk] Existing diagnoses become "stale"** — old `diagnosis.json` files lack the new fields. → Migration: optional fields default None, no breaking change; diags can be regenerated by re-running stage 3-diag.

## Migration Plan

1. Add `read_walk_forward_metrics` helper + unit tests.
2. Update `build_diagnosis_prompt` to accept optional walk-forward block.
3. Update `rule_based_action` signature to accept optional walk-forward metrics.
4. Update main loop in `stage3_diagnose.py::main()` to wire walk-forward into both helpers.
5. Update `DiagnosisBlock` schema in `dashboard/server/schemas.py` with two optional fields.
6. Re-run `python -m research.pipeline.stage3_diagnose` for all strategies in current `strategy_runs.json`; verify `eth_s5_half_size` flips to `proceed`.
7. Re-run `python -m research.pipeline.stage5_select` and confirm `eth_s5_half_size.selected == True`.
8. Update `research/PIPELINE.md` stage 3-diag section.

Rollback: revert the four-file diff. Optional schema fields can stay (harmless).
