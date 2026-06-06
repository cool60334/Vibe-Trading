# backtest-selection Specification

## Purpose

Stage 5 strategy selection: scoring, ranking, and promoting strategies to the dashboard. Owns `selection.json` writing, per-strategy `manifest.json` emission, and the `emit_manifest_for_strategy` write helper that both the pipeline and the standalone backfill CLI share.

## Requirements

### Requirement: Stage 5 emits per-strategy manifest.json

After `research/pipeline/stage5_select.py` successfully writes `research/manifests/selection.json`, it SHALL iterate every strategy registered in `strategy_runs.json` and emit a `research/manifests/<strategy_id>/manifest.json` file conforming to `dashboard/server/schemas.py::StrategyManifest`. The emission MUST reuse `research/emit_manifest.py::build_strategy_manifest(...)` for the manifest content (no re-implementation), and MUST persist the result by serialising the returned dict to JSON.

Emission MUST be fail-soft per strategy: a single `build_strategy_manifest` failure (any exception raised during build) MUST be logged with the strategy id and exception summary, the loop MUST continue with the remaining strategies, and the runner exit code MUST NOT change due to per-strategy emission failures alone. The runner exit code is still driven by `selection.json` validity per the existing requirement.

The standalone CLI `python -m research.emit_manifest` MUST continue to function for backfill scenarios. Both code paths MUST share the same write helper (e.g. `emit_manifest_for_strategy(...)`) so behaviour cannot drift.

#### Scenario: every eligible strategy gets a manifest

- **GIVEN** stage 5 runs against a `strategy_runs.json` with 3 strategies (eth_s5_half_size eligible-proceed, btc_s7 eligible-back_to_stage_4, eth_s1 ineligible-back_to_stage_2)
- **AND** each strategy has its required upstream artifacts (diagnosis.json, optimization.json, metrics.csv) present
- **WHEN** `stage5_select.main()` runs to completion
- **THEN** `research/manifests/eth_s5_half_size/manifest.json` MUST exist
- **AND** `research/manifests/btc_s7_stablecoin_funding_gated/manifest.json` MUST exist
- **AND** `research/manifests/eth_s1_multi_factor_consensus/manifest.json` MUST exist
- **AND** each MUST validate against `StrategyManifest.model_validate_json(...)`
- **AND** `selection.json` MUST still exist with the original ranking

#### Scenario: emission is idempotent

- **GIVEN** a `manifest.json` already exists from a previous stage 5 run
- **WHEN** stage 5 runs again
- **THEN** the existing file MUST be overwritten with the latest aggregated content
- **AND** the new file MUST contain the latest `selected` flag and score from the just-written `selection.json`

#### Scenario: per-strategy emit failure does not abort

- **GIVEN** one strategy's `build_strategy_manifest(...)` raises an exception (e.g. corrupt diagnosis.json)
- **AND** other strategies build successfully
- **WHEN** stage 5 runs
- **THEN** the failing strategy MUST log a red warning containing the strategy id and exception summary
- **AND** other strategies' `manifest.json` files MUST still be written
- **AND** the runner exit code MUST be 0 (or whatever `selection.json` validity dictates), MUST NOT be non-zero solely because of one emit failure

#### Scenario: stage 5 summary reports emission counts

- **WHEN** stage 5 completes its emission loop
- **THEN** stdout MUST contain an "Emitted: N/M manifests successfully" line where N is the count of successful per-strategy writes and M is the total number of strategies attempted
- **AND** for each successful write, a line of form `[OK] <strategy_id> → <relative path to manifest.json>` MUST appear

### Requirement: emit_manifest write helper exposed

`research/emit_manifest.py` SHALL expose a per-strategy write helper, e.g. `emit_manifest_for_strategy(strategy_id, entry, runs_root, manifests_dir) -> Path`, that:

1. Calls `build_strategy_manifest(...)` with the given inputs
2. Creates `manifests_dir / strategy_id /` if missing
3. Writes the JSON to `manifests_dir / strategy_id / "manifest.json"`
4. Returns the written path

Both the standalone `main()` and `stage5_select.py` MUST call this helper rather than duplicating the write logic.

#### Scenario: helper returns the written path

- **GIVEN** valid inputs (strategy_id, StrategyRunsEntry, paths)
- **WHEN** `emit_manifest_for_strategy(...)` is called
- **THEN** the returned `Path` MUST equal `manifests_dir / strategy_id / "manifest.json"`
- **AND** the file MUST exist at that path after the call
- **AND** the file content MUST be a JSON object validating against `StrategyManifest`

#### Scenario: standalone CLI still works

- **GIVEN** `research/emit_manifest.py::main()` is invoked via `python -m research.emit_manifest`
- **WHEN** the run completes
- **THEN** behaviour MUST be unchanged from the pre-refactor version: one manifest.json per strategy in `strategy_runs.json`, summary printed, exit code based on verification
