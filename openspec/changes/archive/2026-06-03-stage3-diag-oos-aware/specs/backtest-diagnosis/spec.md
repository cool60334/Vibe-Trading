## ADDED Requirements

### Requirement: Stage 3-diag walk-forward metrics ingestion

When a strategy's `strategy_runs.json` entry has a non-empty `walk_forward_runs` array, the diagnosis runner SHALL load the metrics from the first walk-forward run's `runs/<wf>/artifacts/metrics.csv` and pass them to both the LLM prompt builder and the rule-based fallback. When `walk_forward_runs` is empty or the metrics file is missing/unreadable, the runner MUST fall back gracefully to train-only behaviour (current pre-change semantics) with a stdout warning.

A pure helper `read_walk_forward_metrics(strategy_runs_entry, runs_root) -> dict | None` MUST be provided in `research/pipeline/stage3_diagnose.py`, returning the parsed metrics dict (same shape as `read_metrics_csv`) on success and `None` on any failure mode.

#### Scenario: walk-forward run exists and is read

- **GIVEN** `strategy_runs_entry["walk_forward_runs"] == ["eth_s5_half_size_oos"]`
- **AND** `runs/eth_s5_half_size_oos/artifacts/metrics.csv` exists and parses
- **WHEN** `read_walk_forward_metrics(strategy_runs_entry, runs_root)` is called
- **THEN** it MUST return a dict containing at least the keys `sharpe`, `max_drawdown`, `trade_count`
- **AND** the values MUST match those in the file

#### Scenario: walk_forward_runs is empty

- **GIVEN** `strategy_runs_entry["walk_forward_runs"] == []`
- **WHEN** `read_walk_forward_metrics(...)` is called
- **THEN** it MUST return `None`
- **AND** it MUST NOT raise

#### Scenario: walk-forward metrics file missing

- **GIVEN** `strategy_runs_entry["walk_forward_runs"] == ["nonexistent_run"]`
- **AND** `runs/nonexistent_run/artifacts/metrics.csv` does not exist
- **WHEN** `read_walk_forward_metrics(...)` is called
- **THEN** it MUST return `None`
- **AND** it MUST NOT raise

### Requirement: OOS-prioritising LLM prompt

`build_diagnosis_prompt` MUST accept an optional `walk_forward_metrics` argument. When this argument is non-None, the prompt MUST:

1. Render the train and stage-4-best metrics under a section explicitly titled with the word "Train" or "in-sample"
2. Render the walk-forward metrics under a section explicitly titled with the words "Walk-Forward" and "OOS"
3. Include the literal phrase "walk-forward is the authoritative signal" (or close synonym) in the task instructions so the LLM is unambiguous about which block to weight
4. Instruct the LLM to use the train block only for overfit detection (train→OOS gap), not as the primary verdict source

When `walk_forward_metrics is None`, the prompt MUST keep the existing single-block layout (no OOS section, no "authoritative" claim).

#### Scenario: prompt with walk-forward metrics

- **GIVEN** walk_forward_metrics = {"sharpe": 1.02, "max_drawdown": -0.091, "trade_count": 49}
- **WHEN** `build_diagnosis_prompt(...)` is called
- **THEN** the returned prompt string MUST contain the substring "Walk-Forward"
- **AND** the prompt MUST contain the substring "OOS"
- **AND** the prompt MUST contain the phrase "authoritative" (case-insensitive)
- **AND** the JSON of walk_forward_metrics MUST appear in the prompt

#### Scenario: prompt without walk-forward metrics

- **GIVEN** `walk_forward_metrics is None`
- **WHEN** `build_diagnosis_prompt(...)` is called
- **THEN** the prompt MUST NOT contain "Walk-Forward" or "authoritative"
- **AND** structure MUST match the pre-change format (single metrics block + optional stage-4 best section)

### Requirement: OOS-anchored rule-based fallback

`rule_based_action` MUST accept an optional `walk_forward_metrics` argument. When non-None, the decision tree MUST anchor on walk-forward metrics, with thresholds:

- `wf_sharpe < 0` → `back_to_stage_2` (concept-broken on true OOS)
- `wf_sharpe < 1.0` OR `|wf_max_drawdown| > 0.15` OR `wf_trade_count < 30` → `back_to_stage_4`
- else → `proceed`

The walk-forward trade-count gate is intentionally looser (30, vs the 50 used for train) because the held-out OOS window is typically half the train length. When `walk_forward_metrics is None`, the fallback MUST keep the existing train-anchored decision tree unchanged.

#### Scenario: walk-forward positive sharpe and acceptable DD → proceed

- **GIVEN** walk_forward_metrics = {"sharpe": 1.02, "max_drawdown": -0.091, "trade_count": 49}
- **WHEN** `rule_based_action(metrics_by_run={...}, walk_forward_metrics=wf)` is called
- **THEN** it MUST return `RecommendedAction.PROCEED`

#### Scenario: walk-forward sharpe under 1.0 → back_to_stage_4

- **GIVEN** walk_forward_metrics = {"sharpe": 0.25, "max_drawdown": -0.26, "trade_count": 89}
- **WHEN** `rule_based_action(metrics_by_run={...}, walk_forward_metrics=wf)` is called
- **THEN** it MUST return `RecommendedAction.BACK_TO_STAGE_4`

#### Scenario: walk-forward negative sharpe → back_to_stage_2

- **GIVEN** walk_forward_metrics = {"sharpe": -4.16, "max_drawdown": -0.94, "trade_count": 1721}
- **WHEN** `rule_based_action(metrics_by_run={...}, walk_forward_metrics=wf)` is called
- **THEN** it MUST return `RecommendedAction.BACK_TO_STAGE_2`

#### Scenario: walk-forward unavailable → fall back to train

- **GIVEN** walk_forward_metrics = None
- **AND** metrics_by_run["base"]["sharpe"] = 1.5, trades = 120
- **WHEN** `rule_based_action(metrics_by_run, walk_forward_metrics=None)` is called
- **THEN** decision MUST be computed exclusively from `metrics_by_run` and `optimization_metrics` (existing behaviour)

#### Scenario: walk-forward low trade count above 30 → proceed

- **GIVEN** walk_forward_metrics = {"sharpe": 1.10, "max_drawdown": -0.08, "trade_count": 35}
- **WHEN** `rule_based_action(metrics_by_run={...}, walk_forward_metrics=wf)` is called
- **THEN** it MUST return `RecommendedAction.PROCEED`
- **AND** trade_count 35 MUST NOT trigger back_to_stage_4 (loosened OOS gate ≥ 30)

### Requirement: DiagnosisBlock schema audit fields

`dashboard/server/schemas.py::DiagnosisBlock` SHALL gain two optional fields:

- `walk_forward_source: Optional[str] = None` — the run name read for walk-forward metrics (e.g. `"eth_s5_half_size_oos"`)
- `walk_forward_metrics: Optional[dict] = None` — the metrics dict that drove the verdict, echoed for audit

Both fields MUST default to `None` and MUST NOT be required. Existing archived `diagnosis.json` files (without these fields) MUST continue to validate against the schema. Stage 5 (`stage5_select.py`) MUST continue reading only `recommended_action` and MUST NOT depend on these new fields.

#### Scenario: archived diagnosis without walk-forward fields still validates

- **GIVEN** a `diagnosis.json` written before this change (no `walk_forward_*` keys)
- **WHEN** validated via `DiagnosisBlock.model_validate_json(...)`
- **THEN** validation MUST pass
- **AND** the loaded object's `walk_forward_source` MUST be `None`
- **AND** the loaded object's `walk_forward_metrics` MUST be `None`

#### Scenario: new diagnosis with walk-forward fields validates and round-trips

- **GIVEN** a `diagnosis.json` with `walk_forward_source = "eth_s5_half_size_oos"` and `walk_forward_metrics = {...}`
- **WHEN** loaded then re-serialised via Pydantic
- **THEN** the round-tripped payload MUST preserve both fields verbatim
