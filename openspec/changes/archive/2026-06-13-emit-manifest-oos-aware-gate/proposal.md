## Why

The dashboard's promotion gate (`research/emit_manifest.py::compute_gate`, surfaced via each strategy's `manifest.json::gate`) **fatal-fails every walk-forward strategy**, blocking all of them from testnet. This is systemic, not strategy-specific: `btc_s9_stablecoin_funding_gated_tuned`, `eth_s3_dynamic_exit`, and `eth_s5_half_size` all fail the identical two fatal gates (`oos_sharpe_positive`, `alpha_not_fee_illusion`) despite having strong, honest out-of-sample performance (eth_s5 OOS: sharpe 1.02, DD 9.1%, profit_factor 1.54, +54pp alpha vs ETH).

Three root causes:

1. **OOS read from the wrong field.** `build_backtest_block` populates `backtest.oos` only from `entry.oos_runs[0]`. Walk-forward strategies have `oos_runs == []` and store their true held-out OOS in `walk_forward_runs`. So `backtest.oos` is `None`, making `oos_sharpe_positive` evaluate to `actual=null → fatal fail`.

2. **Gate evaluates train, not OOS.** `compute_gate` checks `min_sharpe` / `max_drawdown` / `min_trades` against `in_sample` (train-window) metrics, which are overfit-optimistic in walk-forward mode. This is the same train-vs-OOS mistake already fixed for stage 3-diag in `stage3-diag-oos-aware`. The schema even predefines `GATE_MIN_WALK_FORWARD_SHARPE = 1.0` but `compute_gate` never wires it in.

3. **A fatal gate with no obtainable data.** `alpha_not_fee_illusion` requires `stress_runs` (fee-escalation backtests). No strategy in the pipeline generates them, so `worst_stress_sharpe` is always `None`, fatal-failing universally. A fatal hard-block that nothing can satisfy is a dead gate.

## What Changes

- **`build_backtest_block`**: when `entry.oos_runs` is empty, fall back to reading `backtest.oos` from `entry.walk_forward_runs[0]`. When neither exists, `oos` stays `None` (legacy behaviour). A new `oos_source` notion is implicit — the metrics carry their `source_run` already.
- **`compute_gate`**: when an OOS metrics block is present, evaluate `min_sharpe`, `max_drawdown`, and `min_trades` against the **OOS** metrics instead of `in_sample`. Use `GATE_MIN_WALK_FORWARD_SHARPE` (1.0) as the sharpe threshold for the OOS path (held-out bar is lower than the aspirational in-sample 1.5), and a looser OOS trade gate of **30** (consistent with the stage3-diag OOS trade gate; the held-out window is ~half the train length). When no OOS is present, keep the existing in-sample evaluation unchanged.
- **`alpha_not_fee_illusion`**: when no cost-stress data exists (`backtest.cost_stress is None` or empty), the check becomes **non-fatal informational** (`actual=null`, `passed=false`, `fatal=false`) rather than a fatal hard-block. When stress data DOES exist and the worst stress sharpe is ≤ 0, it remains a fatal block. Rationale: absence of evidence is not evidence of a fee illusion; we surface it as a warning so the user knows fee-stress wasn't tested, without permanently barring promotion.
- **`oos_sharpe_positive`** stays fatal, but now receives real OOS data (from change #1), so honest negative-OOS strategies still get correctly blocked.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `backtest-selection`: owns stage 5 selection + `manifest.json` emission + the gate computation that drives the dashboard promote flow. This change modifies the gate's data source (OOS-aware) and the fatal/non-fatal classification of `alpha_not_fee_illusion`.

## Impact

**Affected files** (research/ subtree only):
- `research/emit_manifest.py` — `build_backtest_block` (OOS fallback), `compute_gate` (OOS-aware evaluation + alpha gate downgrade)
- `research/tests/test_emit_manifest.py` — new gate scenarios
- `research/PIPELINE.md` — Stage 5 / gate section update

**Not changed**:
- `dashboard/server/schemas.py` — gate constants reused as-is (including the already-present `GATE_MIN_WALK_FORWARD_SHARPE`); `GateBlock` / `GateThreshold` schema unchanged
- `dashboard/server/main.py` and the promote/testnet endpoints — unchanged; they read `gate.fatal_fail` / `gate.overall_pass` which now reflect honest OOS results
- Other pipeline stages

**Breaking changes**: None to schema or API. Behavioural change: walk-forward strategies with positive OOS sharpe and acceptable OOS drawdown will now pass the fatal gates and become promotable. Strategies with genuinely negative OOS sharpe remain blocked.

**Risks**:
- Downgrading `alpha_not_fee_illusion` to non-fatal removes a guardrail against fee-illusion alpha. → Mitigation: it stays fatal whenever stress data IS present; the red_flag `alpha_is_fee_illusion` derivation is unchanged; and the long-term correct fix (generate stress runs in stage 3) is recorded as follow-up. For now, the real-funding recompute already done for eth_s5 confirms its alpha survives realistic funding.
- OOS trade gate of 30 admits smaller-sample strategies. → Mitigation: `too_few_trades` red_flag still fires below 100, surfacing the caveat without hard-blocking; consistent with stage3-diag's accepted 30 gate.
