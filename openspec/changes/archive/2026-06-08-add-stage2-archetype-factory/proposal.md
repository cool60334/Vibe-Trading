## Why

Stage 2 today emits exactly **one** strategy per symbol and labels it with a generic
archetype (`multi_factor_consensus` or `<factor>_mean_reversion`). It never produces the
`trend_with_gate` archetype — yet that is the only archetype that has ever yielded a
`selected=True` strategy (btc_s9, eth_s5 were all **hand-written** clones, outside the
pipeline). Worse, the runner shells out to the `crypto_trading_desk` swarm on the critical
path: a non-zero swarm exit raises and marks the whole symbol FAILED, so a flaky LLM call
can produce zero strategies. To reach "no-Claude, fully automatic factors → strategies",
Stage 2 must deterministically fan out the archetypes that actually win and stop depending
on the swarm to succeed.

## What Changes

- Add a deterministic **archetype router**: given a symbol's stage-1 usable factors, pick a
  bounded set (≤3) of archetypes by IC-sign rules — `single_factor` (top-1 |IC|),
  `trend_with_gate` (top positive-IC trend factor gated by top negative-IC factor),
  `consensus_all` (2–3 factors AND).
- Emit **one strategy YAML per selected archetype** (multiple per symbol) instead of the
  current fixed one. Each builder reuses the already-validated `_factor_is_trend` /
  `_entry_condition` / measured-IC-sign logic — no Jinja, extend the existing Python
  dict-builder.
- **BREAKING (workflow)**: `STRATEGIES_PER_SYMBOL` is removed; the count is now derived from
  the router. Downstream `strategy_runs.json` registers N strategies per symbol.
- Demote the swarm to an **optional `--use-swarm` rationale enrichment** (mirrors the Stage 0
  deterministic flip). Default path is swarm-free and never FAILs a symbol on swarm error.
- Add a Stage 3 **fail-fast guard**: a base run with sharpe < −2 or > 1000 trades/yr is
  flagged `archetype_misfit` and skipped before Stage 4, so grid sweeps are not burned on
  hopeless archetypes.
- **Out of scope (explicit non-goals)**: no C(n,k) exhaustive factor-combination enumeration
  (multiple-testing inflation); no LLM-authored structured YAML.

## Capabilities

### New Capabilities
- `strategy-archetype-synthesis`: Deterministic, swarm-independent synthesis of multiple
  strategy specs per symbol via a bounded archetype router driven by stage-1 factor IC
  signs, plus a fail-fast guard that prevents misfit archetypes from reaching parameter
  optimization.

### Modified Capabilities
<!-- None: Stage 2 has no existing spec under openspec/specs/; this introduces the first one.
     backtest-selection / backtest-diagnosis behavior is unchanged (they naturally rank the
     extra candidates). -->

## Impact

- **Code**: `research/pipeline/stage2_strategies.py` (router + per-archetype builders,
  swarm demotion, multi-emit loop); new `research/pipeline/lib/archetype_router.py`;
  `research/pipeline/stage3_backtest.py` (fail-fast guard); `research/pipeline/strategy_runs.py`
  (register N strategies/symbol).
- **Artifacts**: more `strategy_<id>.yaml` + `generation.json` per symbol; `strategy_runs.json`
  grows. Stage 5 already weight-ranks all candidates — no change needed there.
- **CLI/flags**: `--use-swarm` becomes opt-in (was implicit); pipeline default no longer
  requires LLM/API keys for Stage 2.
- **Dependencies**: none added (no Jinja). Existing `_KNOWN_SOURCES`, schema unchanged.
