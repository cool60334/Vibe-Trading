## ADDED Requirements

### Requirement: Gate OOS source falls back to walk-forward

`research/emit_manifest.py::build_backtest_block` SHALL populate `BacktestBlock.oos` from `entry.oos_runs[0]` when `oos_runs` is non-empty, and otherwise SHALL fall back to `entry.walk_forward_runs[0]`. When both are empty, `oos` MUST remain `None`. The chosen run's metrics MUST be read via the existing `metrics_csv_to_backtest_metrics(...)` helper, and the resulting `BacktestMetrics.source_run` MUST identify which run supplied the OOS data.

#### Scenario: walk-forward used when oos_runs empty

- **GIVEN** a StrategyRunsEntry with `oos_runs == []` and `walk_forward_runs == ["eth_s5_half_size_oos"]`
- **AND** `runs/eth_s5_half_size_oos/artifacts/metrics.csv` exists with sharpe 1.016
- **WHEN** `build_backtest_block(entry, runs_root)` is called
- **THEN** the returned `BacktestBlock.oos` MUST NOT be `None`
- **AND** `oos.sharpe` MUST equal the walk-forward run's sharpe
- **AND** `oos.source_run` MUST equal `"eth_s5_half_size_oos"`

#### Scenario: explicit oos_runs takes precedence

- **GIVEN** a StrategyRunsEntry with `oos_runs == ["foo_oos_2024"]` and `walk_forward_runs == ["foo_wf"]`
- **WHEN** `build_backtest_block(entry, runs_root)` is called
- **THEN** `oos.source_run` MUST equal `"foo_oos_2024"` (oos_runs wins)

#### Scenario: neither present leaves oos None

- **GIVEN** a StrategyRunsEntry with `oos_runs == []` and `walk_forward_runs == ()`
- **WHEN** `build_backtest_block(entry, runs_root)` is called
- **THEN** `BacktestBlock.oos` MUST be `None`

### Requirement: Gate thresholds evaluate OOS when available

`research/emit_manifest.py::compute_gate` SHALL evaluate `min_sharpe`, `max_drawdown`, and `min_trades` against the OOS metrics when `backtest.oos` is present, and against `in_sample` metrics when it is not. In OOS mode the sharpe threshold MUST be `GATE_MIN_WALK_FORWARD_SHARPE` (1.0) and the trade-count threshold MUST be a looser OOS gate of 30 (defined as a module constant, e.g. `GATE_OOS_MIN_TRADES`). In legacy (no-OOS) mode the thresholds MUST remain `GATE_MIN_SHARPE` (1.5) and `GATE_MIN_TRADES` (100) exactly as before. The `max_drawdown` threshold (`GATE_MAX_DRAWDOWN`, 0.10) and `min_profit_factor` threshold (`GATE_MIN_PROFIT_FACTOR`, 1.5) are unchanged in both modes. Each emitted `GateThreshold.threshold` MUST report the value actually applied so the dashboard shows the real bar.

#### Scenario: OOS-mode sharpe uses walk-forward bar

- **GIVEN** a BacktestBlock with `oos.sharpe == 1.016` and `in_sample.sharpe == 1.19`
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `min_sharpe` threshold's `actual` MUST equal `1.016` (the OOS value)
- **AND** its `threshold` MUST equal `1.0` (GATE_MIN_WALK_FORWARD_SHARPE)
- **AND** its `passed` MUST be `true`

#### Scenario: OOS-mode trade gate is 30

- **GIVEN** a BacktestBlock with `oos.trades == 49`
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `min_trades` threshold's `actual` MUST equal `49`
- **AND** its `threshold` MUST equal `30`
- **AND** its `passed` MUST be `true`

#### Scenario: OOS-mode drawdown evaluated against OOS

- **GIVEN** a BacktestBlock with `oos.max_drawdown == 0.091` and `in_sample.max_drawdown == 0.149`
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `max_drawdown` threshold's `actual` MUST equal `0.091`
- **AND** its `passed` MUST be `true` (0.091 <= 0.10)

#### Scenario: legacy mode unchanged when no OOS

- **GIVEN** a BacktestBlock with `oos is None` and `in_sample.sharpe == 1.6`, `in_sample.trades == 120`
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `min_sharpe` threshold's `actual` MUST equal `1.6` and `threshold` MUST equal `1.5`
- **AND** the `min_trades` threshold's `threshold` MUST equal `100`

### Requirement: alpha_not_fee_illusion fatal only when evaluable

`compute_gate` SHALL classify the `alpha_not_fee_illusion` check as fatal **only when cost-stress data is available** to evaluate it. When `backtest.cost_stress` is `None` or contains no usable stress-level sharpes, the check MUST be emitted with `actual=null`, `passed=false`, and `fatal=false` (informational, not a hard block). When cost-stress data IS present, the check MUST remain fatal: `passed` is true iff the worst stress-level sharpe is greater than 0, and `fatal` is true.

#### Scenario: no stress data → non-fatal informational

- **GIVEN** a BacktestBlock with `cost_stress is None`
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `alpha_not_fee_illusion` threshold MUST have `actual` null, `passed` false, `fatal` **false**
- **AND** `gate.fatal_fail` MUST NOT be set to true by this check alone

#### Scenario: stress data present with negative worst sharpe → fatal block

- **GIVEN** a BacktestBlock with cost_stress levels whose minimum sharpe is -0.5
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `alpha_not_fee_illusion` threshold MUST have `actual == -0.5`, `passed` false, `fatal` **true**
- **AND** `gate.fatal_fail` MUST be true

#### Scenario: stress data present with positive worst sharpe → pass

- **GIVEN** a BacktestBlock with cost_stress levels whose minimum sharpe is 0.4
- **WHEN** `compute_gate(backtest)` is called
- **THEN** the `alpha_not_fee_illusion` threshold MUST have `passed` true, `fatal` true

### Requirement: walk-forward strategy with healthy OOS is promotable

For a walk-forward strategy whose held-out OOS has positive sharpe, drawdown within the gate, and at least the OOS trade gate count, `compute_gate` MUST NOT set `fatal_fail` when no cost-stress data exists. The strategy's `manifest.json` MUST therefore be promotable (the dashboard hard-blocks only on `fatal_fail`).

#### Scenario: eth_s5-shaped strategy passes fatal gates

- **GIVEN** a BacktestBlock with `oos` = {sharpe 1.016, max_drawdown 0.091, trades 49, profit_factor 1.539} and `cost_stress is None`
- **WHEN** `compute_gate(backtest)` is called
- **THEN** `oos_sharpe_positive` MUST pass (1.016 > 0)
- **AND** `alpha_not_fee_illusion` MUST be non-fatal (no stress data)
- **AND** `gate.fatal_fail` MUST be `false`
