# strategy-archetype-synthesis Specification

## Purpose
TBD - created by archiving change add-stage2-archetype-factory. Update Purpose after archive.
## Requirements
### Requirement: Deterministic archetype routing from stage-1 factors

The system SHALL select a bounded set of strategy archetypes for a symbol deterministically
from that symbol's stage-1 usable (non-`reject`) factors, using each factor's measured IC
sign, without invoking any LLM. The set SHALL contain at most 3 archetypes per symbol.

The router SHALL apply these rules:
- `single_factor` SHALL be selected whenever ≥1 usable factor exists, bound to the top-1
  |IC| factor.
- `trend_with_gate` SHALL be selected only when both a positive-IC factor and a negative-IC
  factor exist, pairing the top-|IC| positive (trend) factor with the top-|IC| negative
  (gate) factor.
- `consensus_all` SHALL be selected only when 2 ≤ (usable factor count) ≤ 3.

#### Scenario: Single usable factor
- **WHEN** a symbol has exactly one usable stage-1 factor
- **THEN** the router returns only `single_factor` bound to that factor

#### Scenario: Trend and gate factors both present
- **WHEN** a symbol has at least one positive-IC and one negative-IC usable factor
- **THEN** the router includes `trend_with_gate` pairing the top-|IC| trend factor with the top-|IC| gate factor

#### Scenario: No sign diversity omits trend_with_gate
- **WHEN** all usable factors share the same IC sign
- **THEN** the router does NOT include `trend_with_gate`

#### Scenario: Bounded fan-out
- **WHEN** the rules would select more than 3 archetypes
- **THEN** the router returns at most 3 archetype plans

### Requirement: Multiple strategy specs emitted per symbol

The system SHALL emit one strategy YAML spec and one `generation.json` per archetype the
router selects, producing potentially multiple strategies per symbol. Each strategy id SHALL
be unique and encode its archetype as `<coin>_s<seq>_<archetype>`.

In addition, for every archetype spec emitted, the system SHALL emit one **regime variant**:
an otherwise-identical spec with `regime_filter: true`, whose strategy id carries the suffix
`_regime` (i.e. `<coin>_s<seq>_<archetype>_regime`). The variant's `generation.json` MUST note
the regime overlay and its runtime dependency on `research/manifests/regime_<sym>.json`. Both
the base and the regime variant MUST be registered in `strategy_runs.json` so downstream
stages (3 fail-fast, 3-diag, 4 walk-forward, 5 selection) evaluate them independently and
decide survival by data.

#### Scenario: Two archetypes selected
- **WHEN** the router selects `single_factor` and `trend_with_gate` for a symbol
- **THEN** four distinct strategy YAML files and four generation.json files are written
  (each archetype's base + `_regime` variant), with distinct ids

#### Scenario: Emitted spec validates against the strategy schema
- **WHEN** any archetype spec is emitted (base or regime variant)
- **THEN** the YAML contains all required strategy keys and compiles through the stage-2b
  signal engine without schema or AST error

#### Scenario: Regime variant differs only in regime_filter and id
- **WHEN** a base spec and its `_regime` variant are emitted for the same archetype
- **THEN** the variant's `regime_filter` MUST be `true` and the base's `false`
- **AND** entry/exit/indicators/parameter_search_ranges MUST be otherwise identical

### Requirement: Scaffold exposes size_mult as a sweepable parameter

`_default_spec_scaffold` SHALL include `size_mult` in every emitted spec's
`parameter_search_ranges`, with a coarse default range of `[0.4, 1.0, 0.2]`
(yielding candidates 0.4 / 0.6 / 0.8 / 1.0). The spec's top-level `size_mult`
default remains `1.0`. `research/pipeline/stage4_optimize.py::apply_overrides_to_spec`
SHALL support a `size_mult` override key that sets the spec's top-level `size_mult`
field, so the stage-4 grid sweep recompiles and evaluates each sizing level — replacing
the manual sizing calibration previously done by hand for `eth_s5_half_size`.

#### Scenario: scaffold includes size_mult range
- **WHEN** any archetype spec is emitted by stage 2
- **THEN** its `parameter_search_ranges` MUST contain `size_mult: [0.4, 1.0, 0.2]`

#### Scenario: stage 4 sweeps size_mult
- **GIVEN** a spec whose `parameter_search_ranges` includes `size_mult`
- **WHEN** `apply_overrides_to_spec(base_spec, {"size_mult": 0.6, ...})` is applied
- **THEN** the resulting spec dict's top-level `size_mult` MUST equal `0.6`
- **AND** the recompiled engine MUST output signal magnitude `±0.6`

#### Scenario: override absent leaves size_mult untouched
- **GIVEN** an overrides dict without `size_mult`
- **WHEN** `apply_overrides_to_spec` is applied
- **THEN** the spec's `size_mult` MUST remain its original value

### Requirement: Entry conditions follow measured IC sign per archetype

The system SHALL build entry conditions from each factor's measured IC sign: a positive-IC
factor enters long at its high percentile extreme and short at its low extreme; a negative-IC
factor does the reverse. `trend_with_gate` SHALL require both the trend factor at its
favourable extreme AND the gate factor at its favourable extreme (`logic: all`).

#### Scenario: trend_with_gate mirrors the validated structure
- **WHEN** a `trend_with_gate` spec is built from a positive-IC trend factor and a negative-IC gate factor
- **THEN** the long entry requires the trend factor high AND the gate factor low, combined with `logic: all`

#### Scenario: Three-factor consensus avoids over-sparsity
- **WHEN** a `consensus_all` archetype has 3 factors
- **THEN** the entry uses `logic: any` (per the existing sparsity rule), and 2-factor consensus uses `logic: all`

### Requirement: Swarm is optional enrichment, never a failure point

The system SHALL produce all strategy specs deterministically without the trading-desk swarm
by default. A swarm invocation SHALL occur only when explicitly enabled (`--use-swarm` or the
equivalent environment flag), and SHALL be fail-soft: any swarm failure, timeout, or
unparseable result SHALL retain the deterministic rationale and allow Stage 2 to exit 0.

#### Scenario: Default run is swarm-free
- **WHEN** Stage 2 runs without `--use-swarm`
- **THEN** no swarm subprocess is invoked and every routed archetype yields a valid strategy spec

#### Scenario: Swarm failure does not fail the symbol
- **WHEN** `--use-swarm` is set and the swarm subprocess exits non-zero or returns no parseable result
- **THEN** the strategy specs are still written with the deterministic rationale and Stage 2 exits 0

### Requirement: Stage 3 fail-fast guard for misfit archetypes

The system SHALL evaluate each strategy's base backtest and flag it `archetype_misfit` when
the base sharpe is below −2 or the annualised trade count exceeds 1000. A flagged strategy's
remaining runs (regime, oos, sweep) SHALL be skipped, and the flag SHALL be surfaced so that
Stage 4 does not run a parameter sweep on it.

#### Scenario: Hopeless archetype is skipped before the sweep
- **WHEN** a strategy's base run has sharpe < −2 or > 1000 trades/yr
- **THEN** it is marked `archetype_misfit`, its regime/oos runs are skipped, and Stage 4 does not sweep it

#### Scenario: Viable archetype proceeds normally
- **WHEN** a strategy's base run is within the guard thresholds
- **THEN** it is not flagged and proceeds through the normal Stage 3 → 4 → 5 flow

