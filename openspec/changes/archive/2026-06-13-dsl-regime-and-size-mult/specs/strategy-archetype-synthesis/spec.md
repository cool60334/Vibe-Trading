## MODIFIED Requirements

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

## ADDED Requirements

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
