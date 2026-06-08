## 1. Archetype router (lib, TDD)

- [ ] 1.1 Write failing tests for `pick_archetypes`: single-factor → only `single_factor`; trend+gate present → includes `trend_with_gate` with correct role assignment; same-sign-only → no `trend_with_gate`; 2–3 factors → `consensus_all`; >3 candidate archetypes → capped at 3
- [ ] 1.2 Create `research/pipeline/lib/archetype_router.py` with `ArchetypePlan` dataclass (archetype name + role-tagged factor subset) and `pick_archetypes(factors) -> list[ArchetypePlan]`; use measured IC sign (reuse `_factor_is_trend` logic) for trend/gate roles
- [ ] 1.3 Run tests green; confirm router is pure (no network/subprocess)

## 2. Per-archetype spec builders (stage2, TDD)

- [ ] 2.1 Write failing tests: `trend_with_gate` builder reproduces btc_s9-shaped conditions (trend high AND gate low, `logic: all`); `single_factor` → 1 condition; `consensus_all` → `logic: all` for 2 / `any` for 3; every emitted spec has all required YAML keys
- [ ] 2.2 Refactor `build_strategy_spec()` to accept an `ArchetypePlan`; branch per archetype; reuse `_entry_condition` / `_factor_is_trend` and existing exit/sizing/parameter_search defaults; `strategy_id = <coin>_s<seq>_<archetype>`
- [ ] 2.3 Add a test compiling one emitted spec of each archetype through the stage-2b signal engine (no schema/AST error)

## 3. Stage 2 swarm demotion + multi-emit wiring

- [ ] 3.1 Add `--use-swarm` flag + `RESEARCH_STAGE2_USE_SWARM` env (default OFF); mirror Stage 0's argument handling
- [ ] 3.2 Default path: skip `run_swarm()`, set deterministic `generation.rationale` + `source_run=None` + method note; with flag: invoke swarm fail-soft (failure/timeout/parse-miss keeps deterministic rationale, exit 0)
- [ ] 3.3 Remove `STRATEGIES_PER_SYMBOL`; replace `_generate_for_symbol` single-emit with a loop over `pick_archetypes(...)` writing one YAML + generation.json per plan
- [ ] 3.4 Tests: default run emits no swarm subprocess and produces all routed archetypes; swarm-enabled failure still writes specs and exits 0

## 4. Stage 3 fail-fast guard

- [ ] 4.1 Write failing tests for the guard: base sharpe < −2 or > 1000 trades/yr → `archetype_misfit` + regime/oos skipped; within thresholds → not flagged
- [ ] 4.2 Implement guard in `stage3_backtest.py` after the base run metrics are available; skip remaining runs for flagged strategy; surface flag in summary + in a field Stage 4 reads to skip the sweep
- [ ] 4.3 Confirm Stage 4 honors the `archetype_misfit` flag (no sweep attempted)

## 5. strategy_runs.json registration

- [ ] 5.1 Update `strategy_runs.py` to register an entry per emitted strategy id (base/regime/oos/walk_forward run names); additive + idempotent per id (don't clobber existing hand-written entries)
- [ ] 5.2 Test: N-archetype symbol registers N entries with unique run names

## 6. End-to-end + docs

- [ ] 6.1 Regenerate strategies for configured symbols (default swarm-free); run Stage 3 → 4 → 5; confirm new candidates flow and Stage 5 ranks them
- [ ] 6.2 Update `research/PIPELINE.md` Stage 2 section: archetype router, fan-out, swarm now opt-in, fail-fast guard
- [ ] 6.3 Full test suite green; update the stage2 archetype factory backlog memory to "done" with the change reference
