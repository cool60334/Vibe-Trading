## 1. Archetype router (lib, TDD)

- [x] 1.1 Write failing tests for `pick_archetypes`: single-factor → only `single_factor`; trend+gate present → includes `trend_with_gate` with correct role assignment; same-sign-only → no `trend_with_gate`; 2–3 factors → `consensus_all`; >3 candidate archetypes → capped at 3
- [x] 1.2 Create `research/pipeline/lib/archetype_router.py` with `ArchetypePlan` dataclass (archetype name + role-tagged factor subset) and `pick_archetypes(factors) -> list[ArchetypePlan]`; use measured IC sign (reuse `_factor_is_trend` logic) for trend/gate roles
- [x] 1.3 Run tests green; confirm router is pure (no network/subprocess)

## 2. Per-archetype spec builders (stage2, TDD)

- [x] 2.1 Write failing tests: `trend_with_gate` builder reproduces btc_s9-shaped conditions (trend high AND gate low, `logic: all`); `single_factor` → 1 condition; `consensus_all` → `logic: all` for 2 / `any` for 3; every emitted spec has all required YAML keys
- [x] 2.2 Refactor `build_strategy_spec()` to accept an `ArchetypePlan`; branch per archetype; reuse `_entry_condition` / `_factor_is_trend` and existing exit/sizing/parameter_search defaults; `strategy_id = <coin>_s<seq>_<archetype>`
- [x] 2.3 Add a test compiling one emitted spec of each archetype through the stage-2b signal engine (no schema/AST error)

## 3. Stage 2 swarm demotion + multi-emit wiring

- [x] 3.1 Add `--use-swarm` flag + `RESEARCH_STAGE2_USE_SWARM` env (default OFF); mirror Stage 0's argument handling
- [x] 3.2 Default path: skip `run_swarm()`, set deterministic `generation.rationale` + `source_run=None` + method note; with flag: invoke swarm fail-soft (failure/timeout/parse-miss keeps deterministic rationale, exit 0)
- [x] 3.3 Remove `STRATEGIES_PER_SYMBOL`; replace `_generate_for_symbol` single-emit with a loop over `pick_archetypes(...)` writing one YAML + generation.json per plan
- [x] 3.4 Tests: default run emits no swarm subprocess and produces all routed archetypes; swarm-enabled failure still writes specs and exits 0
- [x] 3.5 (closeout) Thread `runs_path` into `_generate_for_symbol_multi` → `register_strategy(path=)` so tests stop polluting the real `strategy_runs.json`; autouse isolation fixture + regression test (commit 6d095da)

## 4. Stage 3 fail-fast guard

- [x] 4.1 Write failing tests for the guard: base sharpe < −2 or > 1000 trades/yr → `archetype_misfit` + regime/oos skipped; within thresholds → not flagged
- [x] 4.2 Implement guard in `stage3_backtest.py` after the base run metrics are available; skip remaining runs for flagged strategy; surface flag in summary + in a field Stage 4 reads to skip the sweep
- [x] 4.3 Confirm Stage 4 honors the `archetype_misfit` flag (no sweep attempted) — verified live: sol_s1 skipped as `archetype_misfit`

## 5. strategy_runs.json registration

- [x] 5.1 Update `strategy_runs.py` to register an entry per emitted strategy id (base/regime/oos/walk_forward run names); additive + idempotent per id (don't clobber existing hand-written entries)
- [x] 5.2 Test: N-archetype symbol registers N entries with unique run names

## 6. End-to-end + docs

- [x] 6.1 Ran stage 2 fan-out (default swarm-free) for btc+eth: 5 specs emitted (btc → single/trend_with_gate/consensus_all, eth → single/trend_with_gate), all compiled via stage-2b, all backtested OK in stage 3; fail-fast guard fired on sol_s1. NOTE: full stage-4 walk-forward sweep + stage-5 OOS ranking deferred to a manual research run (heavy compute, gitignored output) per owner decision.
- [x] 6.2 Update `research/PIPELINE.md` Stage 2 section: archetype router, fan-out, swarm now opt-in, fail-fast guard
- [x] 6.3 Full suite green for B2 scope (stage2/3/4-misfit/5/strategy_runs/archetype). Follow-ups spawned for pre-existing rot: deterministic stage4 helper tests, stale stage0→1 integration tests, and a degenerate trend_with_gate≡consensus_all (n=2) dedup. Backlog memory updated.
