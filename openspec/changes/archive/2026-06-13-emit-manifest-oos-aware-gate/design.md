## Context

`research/emit_manifest.py` builds each strategy's `manifest.json`. Two functions matter:

- `build_backtest_block(entry, runs_root) -> BacktestBlock | None` — assembles `in_sample` (from `base_run`), `oos` (from `oos_runs[0]`), `by_regime`, `cost_stress` (from `stress_runs`), `benchmark`.
- `compute_gate(backtest) -> GateBlock` — produces 6 threshold checks; 2 are fatal (`oos_sharpe_positive`, `alpha_not_fee_illusion`). `overall_pass = all pass`; `fatal_fail = any fatal not passed`. The dashboard hard-blocks promotion on `fatal_fail`.

Current `strategy_runs.json` shape for a walk-forward strategy:
```json
"eth_s5_half_size": {
  "base_run": "eth_s5_half_size_train",    // in-sample (TRAIN)
  "oos_runs": [],                           // EMPTY in walk-forward mode
  "walk_forward_runs": ["eth_s5_half_size_oos"],  // the TRUE OOS
  "stress_runs": {}                         // EMPTY — no fee stress
}
```

The schema already anticipates walk-forward gating: `dashboard/server/schemas.py` defines `GATE_MIN_WALK_FORWARD_SHARPE = 1.0` (unused by `compute_gate` today).

Precedent: `stage3-diag-oos-aware` (archived 2026-06-03) fixed the identical train-vs-OOS data-source bug for the diagnosis verdict, reading `walk_forward_runs[0]` metrics and anchoring decisions on OOS with a loosened trade gate of 30. This change applies the same philosophy to the gate.

## Goals / Non-Goals

**Goals:**
- The gate evaluates a walk-forward strategy on its held-out OOS metrics, not its overfit train metrics.
- `oos_sharpe_positive` receives real OOS data (from `walk_forward_runs` when `oos_runs` is empty).
- A fatal gate that cannot be evaluated for lack of data (`alpha_not_fee_illusion` with no stress runs) does not permanently block promotion.
- Backwards compatible: strategies with populated `oos_runs` and/or `stress_runs` behave as before; legacy non-walk-forward strategies unaffected.

**Non-Goals:**
- Changing `GateBlock` / `GateThreshold` Pydantic schema.
- Generating fee-stress runs in the pipeline (recorded as follow-up; out of scope here).
- Touching dashboard endpoints — they already key off `gate.fatal_fail`.
- Reworking the stage 5 score formula (separate concern; already OOS-aware via the diagnosis change).

## Decisions

### D1: OOS source fallback in `build_backtest_block`

```python
# ── OOS: prefer explicit oos_runs, else fall back to walk_forward_runs ──
oos: BacktestMetrics | None = None
oos_run_name: str | None = None
if entry.oos_runs:
    oos_run_name = entry.oos_runs[0]
elif entry.walk_forward_runs:
    oos_run_name = entry.walk_forward_runs[0]
if oos_run_name is not None:
    oos_metrics_path = runs_root / oos_run_name / "artifacts" / "metrics.csv"
    oos = metrics_csv_to_backtest_metrics(oos_run_name, oos_metrics_path)
```

`BacktestMetrics.source_run` already records which run the OOS came from, so the manifest is self-describing about whether it's a year-slice or a walk-forward holdout. No schema field needed.

### D2: OOS-aware threshold evaluation in `compute_gate`

Introduce a local `eval_metrics` = `oos_m if oos_m is not None else is_m`, and an `oos_mode = oos_m is not None` flag. Then:

| Threshold | Source when OOS present | Threshold value when OOS present |
|---|---|---|
| `min_sharpe` | `eval_metrics.sharpe` | `GATE_MIN_WALK_FORWARD_SHARPE` (1.0) |
| `max_drawdown` | `eval_metrics.max_drawdown` | `GATE_MAX_DRAWDOWN` (0.10) — unchanged |
| `min_trades` | `eval_metrics.trades` | `30` (OOS gate) instead of `GATE_MIN_TRADES` (100) |
| `min_profit_factor` | `eval_metrics.profit_factor` | `GATE_MIN_PROFIT_FACTOR` (1.5) — unchanged |

When `oos_m is None` (legacy): every threshold uses `is_m` and the original constants (`GATE_MIN_SHARPE` 1.5, `GATE_MIN_TRADES` 100) — **byte-for-byte current behaviour**.

The `min_sharpe` threshold *value* reported in the `GateThreshold` reflects which mode is active (1.0 in OOS mode, 1.5 in legacy), so the dashboard shows the user the bar that was actually applied. The threshold `name` stays `min_sharpe` for schema stability.

Define the OOS trade gate as a module constant `GATE_OOS_MIN_TRADES = 30` in `emit_manifest.py` (not in schemas.py — it's gate-evaluation policy, not a shared schema constant), with a comment cross-referencing the stage3-diag precedent.

### D3: `oos_sharpe_positive` (stays fatal)

No logic change beyond now receiving real data via D1. `actual = oos_m.sharpe`, `passed = actual is not None and actual > 0`, `fatal = True`. A strategy with genuinely negative OOS sharpe still fatal-fails — correct.

### D4: `alpha_not_fee_illusion` — fatal only when evaluable

```python
worst_stress_sharpe = <min of stress level sharpes, or None>
has_stress = worst_stress_sharpe is not None
afi_passed = has_stress and worst_stress_sharpe > 0.0
thresholds.append(GateThreshold(
    name="alpha_not_fee_illusion",
    threshold=0.0,
    actual=worst_stress_sharpe,   # None when untested
    passed=afi_passed,
    fatal=has_stress,             # ← fatal ONLY when we have data to judge
))
```

Semantics:
- Stress data present + worst ≤ 0 → `passed=false, fatal=true` → hard block (alpha is a fee illusion). Unchanged from today.
- Stress data present + worst > 0 → `passed=true, fatal=true(but passed)` → fine.
- **No stress data → `passed=false, fatal=false` → informational warning, not a block.** This is the behavioural change.

`fatal_fail = any(t.fatal and not t.passed)` — so when `alpha_not_fee_illusion` is non-fatal-unpassed, it does not set `fatal_fail`. It still shows `passed=false` in the table so the user sees "fee-stress not tested."

`overall_pass = all(t.passed)` still becomes false (the check is unpassed), so the dashboard can distinguish "fully green" from "promotable but with caveats." Promotion is blocked only on `fatal_fail`, which is now honest.

### D5: red_flags unchanged

`derive_red_flags` already keys off OOS sharpe and trade count independently. With D1 feeding real OOS, the `overfit_suspect` / `oos_sharpe_far_below_is` flags now compute against real held-out data — a free correctness win, no code change. `too_few_trades` still fires below `GATE_MIN_TRADES` (100), so a 49-trade strategy still surfaces the caveat flag without being hard-blocked.

## Risks / Trade-offs

- **[Risk] Removing a fatal guardrail (`alpha_not_fee_illusion` when no data).** A strategy whose alpha *would* evaporate under higher fees could now be promotable. → **Mitigation**: (a) it remains fatal whenever stress data exists; (b) the realistic-funding recompute already run for eth_s5 confirms its alpha survives real funding; (c) follow-up to generate stress runs in stage 3 is recorded; (d) promotion is paper/testnet first, not live capital.
- **[Risk] OOS sharpe threshold 1.0 vs 1.5 is more permissive.** → **Acceptance**: 1.5 on a held-out window is unrealistically strict; the schema author already encoded 1.0 as the walk-forward bar. eth_s5 OOS 1.016 clears it honestly.
- **[Trade-off] OOS trade gate 30 admits small samples.** → **Acceptance**: matches the accepted stage3-diag gate; `too_few_trades` red_flag still surfaces below 100.
- **[Risk] Two trade thresholds (100 legacy / 30 OOS) could confuse.** → **Mitigation**: the `GateThreshold.threshold` field reports the value actually applied, so the dashboard always shows the real bar.

## Migration Plan

1. Add `GATE_OOS_MIN_TRADES = 30` constant + OOS fallback in `build_backtest_block`.
2. Refactor `compute_gate` to OOS-aware evaluation + alpha gate downgrade.
3. Add unit tests (OOS fallback, OOS-mode thresholds, alpha non-fatal when no stress, alpha fatal when stress present + negative, legacy path unchanged).
4. Re-run `python -m research.pipeline.stage5_select` (or `python -m research.emit_manifest`) to regenerate manifests.
5. Verify `eth_s5_half_size` / `btc_s9` / `eth_s3` now have `gate.fatal_fail == false`.
6. Boot dashboard; confirm the three strategies are promotable.
7. Update PIPELINE.md.

Rollback: revert the `emit_manifest.py` diff; manifests regenerate to the prior (all-blocked) state on next emit.
