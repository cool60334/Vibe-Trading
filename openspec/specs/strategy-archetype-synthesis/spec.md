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

#### Scenario: Two archetypes selected
- **WHEN** the router selects `single_factor` and `trend_with_gate` for a symbol
- **THEN** two distinct strategy YAML files and two generation.json files are written, with distinct ids

#### Scenario: Emitted spec validates against the strategy schema
- **WHEN** any archetype spec is emitted
- **THEN** the YAML contains all required strategy keys and compiles through the stage-2b signal engine without schema or AST error

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

