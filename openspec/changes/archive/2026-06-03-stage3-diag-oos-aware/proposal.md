## Why

Stage 3 diagnosis (`research/pipeline/stage3_diagnose.py`) decides per-strategy routing (`proceed` / `back_to_stage_4` / `back_to_stage_2`) by feeding `base_run` metrics + the stage-4 best-sweep run's metrics into an LLM. Both come from the **in-sample TRAIN window** when `oos_start` is set in `research_config.yaml`.

This biases the verdict toward train metrics, which are by construction overfit-optimistic. Concrete failure: `eth_s5_half_size` walk-forward OOS sharpe 1.02 / DD 9.1% / alpha +54pp (vs ETH benchmark -40%) — production-ready by any metric — was tagged `back_to_stage_4` because its **train** DD 14.9% looked "above 10% target" to the LLM, leaving its stage 5 `selected` flag false. Meanwhile `eth_s3_dynamic_exit` (OOS sharpe 0.25 / DD 26%) gets `proceed` because at the time *its* diag was run, the train-only rule happened to look favourable.

The walk-forward holdout (`runs/<wf>/artifacts/metrics.csv`) is the **only true OOS** in the pipeline (per `research/PIPELINE.md` "Walk-Forward train/OOS" section). It's already written by stage 4 but never read by diag.

## What Changes

- **stage3_diagnose.py** MUST, when a strategy's `walk_forward_runs` is non-empty, also load metrics from the first walk-forward run.
- The LLM prompt MUST explicitly label which metrics block is `train`, which is `stage4_best (in-sample)`, and which is `walk_forward (true OOS)`, and MUST instruct the LLM that **OOS is the authoritative signal** for `proceed` / `back_to_stage_4`.
- The rule-based fallback heuristic (used when LLM call fails) MUST evaluate `recommended_action` against the walk-forward metrics when available, falling back to train metrics when not.
- `back_to_stage_2` (concept-broken) gate MUST stay anchored on walk-forward sharpe < 0 (not train) so a strategy with train sharpe 1.5 / OOS sharpe -2 gets correctly killed.
- **No schema changes**. `diagnosis.json` keeps its existing fields; an optional `walk_forward_metrics` block is added under `findings_context` for audit.

## Capabilities

### New Capabilities

- `backtest-diagnosis`: stage 3 routing logic between backtest results and downstream stages (stage 4 sweep / stage 5 selection). Owns the rules for `proceed` / `back_to_stage_4` / `back_to_stage_2`, the LLM prompt schema, and the rule-based fallback heuristic. New capability because no spec currently covers stage 3-diag.

### Modified Capabilities

(none)

## Impact

**Affected files** (research/ subtree only):
- `research/pipeline/stage3_diagnose.py` — load walk-forward metrics, update prompt, update rule-based fallback
- `research/tests/test_stage3_diagnose.py` (new or updated) — scenarios for OOS-aware routing
- `research/PIPELINE.md` — stage 3-diag section updated to describe new behaviour

**Not changed**:
- Schema (`diagnosis.json` keeps same Pydantic structure; only adds optional audit field)
- Stage 4 / 5 / strategy_runs.json
- LLM provider / `vibe-trading run` CLI
- Other pipeline stages

**Breaking changes**: None. Strategies without `walk_forward_runs` keep current behaviour (train-only diag).

**Risks**:
- The OOS window is short (1.4y) — verdict may flip per individual quarter's regime. Mitigation: keep train metrics in the prompt as secondary context.
- The LLM may still misweight OOS vs train. Mitigation: prompt phrasing is explicit; rule-based fallback also OOS-anchored.
