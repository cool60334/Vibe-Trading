# Strategy-level lag-stress (#4) — Design

**Date:** 2026-06-26
**Status:** Approved (agy-reviewed; pending spec review)
**Scope:** Add an opt-in execution-delay stress: re-run each strategy's backtest
with all signals shifted 1-2 bars later; if the edge flips negative or decays
too far, the edge lives in the first bar (racing HFT) and is fragile. This is the
perturbation test for non-price factors (agy).

## Problem

The pipeline has no strategy-level execution-delay check. A strategy whose edge
is real on paper can vanish once you enter 1-2 bars late (infra lag: on-chain /
funding data delay, order latency). Factor-level entry-lag exists
(`phase0_lag_gate`), and cost-stress (`stage3 --stress`) exists, but nothing
stresses the *compiled strategy's* delay tolerance.

## Goal

Mirror cost-stress: `stage3 --lag-stress` re-runs each strategy on train+OOS with
signals delayed by config-driven lags (default 1, 2 bars), emits a
`lag_stress` block, and gates on a **dual gate** (agy): the edge must clear an
absolute Sharpe floor AND retain enough of its base Sharpe under delay.

## Non-goals (explicit)

- **No change to `agent/backtest`** (upstream open-source). The lag is injected
  research-side, at compile time.
- **Non-fatal gate first** — surface `edge_survives_lag`, observe, like DSR/CPCV
  started. Promoting it to fatal / a required NOT_TESTED validation is a
  follow-up (the NOT_TESTED machinery already exists).
- **Symmetric shift** (entry AND exit both delayed) — agy: this is the faithful
  systemic-lag model; delaying only entry would artificially shorten the holding
  period (unrealistically optimistic).

## Design

### 1. Compile-time signal shift (`research/lib/signal_compiler.py`)

`compile_strategy(spec, yaml_hash="", lag_bars=0)`. When `lag_bars > 0`, after the
signal state-machine loop (the `signal.iloc[bar_i] = float(position) * size_mult`
loop, ~line 372), append:

```python
        f"{i8}signal = signal.shift({lag_bars}).fillna(0.0)",
```

- Shifts **only the position Series** — the compiled signal is a plain float
  position (no embedded limit price, no dynamic vol sizing; `size_mult` is a
  constant scalar), so entries fill at market on the delayed bar. This avoids
  agy's price-reference-mismatch and vol-sizing-mismatch pitfalls (both verified
  N/A for this compiler).
- Holding length is preserved: `[0,1,1,1,0]` shifted by 1 → `[0,0,1,1,1]`, still
  3 bars held; only the whole position moves later. `fillna(0.0)` flattens the
  first `lag_bars` bars (<0.05% of a multi-year window — no training pollution).

### 2. Config-driven lags (`research/research_config.yaml`)

```yaml
lag_stress_bars: [1, 2]   # entry-delay stress in bars (mirrors fee mults; tunable)
```

Loaded onto `ResearchConfig.lag_stress_bars: list[int]` (default `[1, 2]` if
absent).

### 3. Runner (`research/pipeline/stage3_backtest.py`)

Mirror cost-stress:

- `lag_stress_run_plan(strategy_id, cfg, today) -> list[(run_name, label, window, lag_bars)]`
  — the cross product of {train, OOS windows} × `cfg.lag_stress_bars`.
  `run_name = f"{strategy_id}_lagstress_{window}_lag{lag}"`.
- `_run_lag_stress_for_strategy(...)` — for each plan item: **recompile** the
  strategy's spec with `lag_bars=lag` (`compile_strategy(spec, lag_bars=lag)`),
  scaffold a run over that window, backtest, collect metrics. Register via a new
  `update_lag_stress_runs(strategy_id, run_names)` in `strategy_runs.py` (mirror
  `update_stress_runs`).
- New CLI flag `--lag-stress` (separate from `--stress`); gathers lag-stress-
  eligible strategies the same way `--stress` does.

### 4. Schema (`dashboard/server/schemas.py`)

Mirror `CostStressBlock` / `CostStressLevel`:

```python
class LagStressLevel(_Manifest):
    label: str                 # e.g. "lag1_train"
    source_run: str
    lag_bars: int
    window: str                # "train" | "oos"
    sharpe: Optional[float] = None

class LagStressBlock(_Manifest):
    source_run: Optional[str] = None
    levels: List[LagStressLevel] = Field(default_factory=list)
```

Add `lag_stress: Optional[LagStressBlock] = None` to `BacktestBlock` (next to
`cost_stress`). New gate constants: `GATE_MIN_LAG_SHARPE = 0.5`,
`GATE_MIN_LAG_RETENTION = 0.6`.

### 5. Aggregation + dual gate (`research/emit_manifest.py`)

- `emit_manifest` reads the strategy's `lag_stress_runs`, builds a
  `LagStressBlock` (one `LagStressLevel` per run: parse `lag_bars`/`window` from
  the label, read `sharpe` from metrics.csv). Attach to `BacktestBlock`.
- `compute_gate` gains an `edge_survives_lag` threshold, **only when lag_stress
  data exists** (like `alpha_not_fee_illusion`):

  - `worst_lag_sharpe = min(level.sharpe for all levels)`.
  - **Retention** per level = `level.sharpe / base_sharpe(window)` where
    `base_sharpe("train") = in_sample.sharpe`, `base_sharpe("oos") = oos.sharpe`.
    Retention is only evaluated for levels whose window base `> 0` (a non-positive
    base means the strategy has no edge to retain — other gates catch that; do not
    divide). `worst_retention = min` over the evaluated levels (None if none
    evaluable).
  - `passed = (worst_lag_sharpe >= GATE_MIN_LAG_SHARPE) AND
    (worst_retention is None OR worst_retention >= GATE_MIN_LAG_RETENTION)`.
  - **Non-fatal** (`fatal=False`) initially. `actual = worst_lag_sharpe`.

## Data flow

Same as cost-stress: stage3 `--lag-stress` writes lag-stress runs + registers
them; emit_manifest aggregates them into `backtest.lag_stress` and adds the gate.
Only the compile step differs (a shifted signal). No `agent/backtest` change.

## Edge cases

- No `--lag-stress` run → no `lag_stress` block → no `edge_survives_lag` gate
  (like cost-stress absent). Non-fatal, absent — nothing changes.
- Base window Sharpe ≤ 0 → retention not evaluated for that window; the absolute
  floor still applies.
- `lag_bars` ≥ hold length would flatten most of the signal → the run simply
  shows a low/negative Sharpe (correctly flagged); config keeps lags small.
- A low-frequency strategy (holds days) is barely affected by a 1-2 bar lag →
  it passes easily. That is correct: its edge is not in the first bar. The test's
  value is catching first-bar-dependent (fast) edges.

## Testing (TDD)

**signal_compiler (`research/tests/test_signal_compiler.py`):**
1. `compile_strategy(spec, lag_bars=2)` output contains `signal = signal.shift(2).fillna(0.0)`; `lag_bars=0` (default) output does not contain `.shift(`.
2. Compiled lagged engine still validates / imports (AST-safe).

**stage3 (`research/tests/test_stage3_backtest.py`):**
3. `lag_stress_run_plan` returns `len(windows) * len(cfg.lag_stress_bars)` items with correct run_name / window / lag_bars.

**schema (`dashboard/server/test_schemas.py`):** `LagStressBlock`/`LagStressLevel` construct; `BacktestBlock.lag_stress` optional; `GATE_MIN_LAG_SHARPE`/`GATE_MIN_LAG_RETENTION` values.

**emit_manifest (`research/tests/test_emit_manifest.py`):**
4. lag_stress present, worst 0.6 & base 1.2 (retention 0.5 < 0.6) → gate fails (retention).
5. worst 0.4 (< 0.5 floor) even with fine retention → fails (floor).
6. worst 0.8, retention 0.8 → passes; `fatal=False`.
7. base ≤ 0 window → retention skipped, only floor decides.
8. no lag_stress → no `edge_survives_lag` threshold.

(Research and dashboard pytest suites run separately.)

## Follow-ups (out of scope, recorded)

- **Require-to-promote:** add `"lag_stress"` to the NOT_TESTED required
  validations + make `edge_survives_lag` fatal, once observed reliable (the
  NOT_TESTED machinery from the promote-gate change already supports this).
- **Config larger lags** for higher-frequency strategies if any are added.
- **Dashboard:** surface the lag_stress block / `edge_survives_lag` in the gate view.
