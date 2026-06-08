## Context

Stage 2 (`research/pipeline/stage2_strategies.py`) currently:

- Emits exactly `STRATEGIES_PER_SYMBOL = 1` strategy per symbol.
- Picks a single archetype via `_archetype_for()` → only `multi_factor_consensus` (≥2
  factors) or `<factor>_mean_reversion` (1 factor). The winning **`trend_with_gate`**
  archetype (btc_s9 / eth_s5) is never produced by the pipeline — those specs were
  hand-authored.
- Calls `run_swarm()` on the critical path inside `_generate_for_symbol()`. A non-zero CLI
  exit raises `CalledProcessError`, caught by `main()` → symbol marked FAILED, no YAML
  emitted. Per [[feedback_swarm_json_fence_fail]] the swarm frequently fails to emit a
  parseable result, so this is a real availability hole.

Crucial observation that shrinks the work: the existing `build_strategy_spec()` already
produces the **exact** btc_s9 structure for the 2-factor `logic: all` case — `_entry_condition`
uses the **measured IC sign** (`_factor_is_trend`) to place each factor at its favourable
percentile extreme. btc_s9 = `stablecoin_supply_z` (positive IC → long high `>=75`) AND
`funding_z` (negative IC → long low `<=20`). So `trend_with_gate` is **not new entry logic** —
it is the existing 2-factor consensus with explicit role labelling and a proper archetype
name. This is why a Python dict-builder extension beats introducing Jinja: the validated
condition logic is reused verbatim.

Stage 0 already performed this exact "deterministic primary, swarm optional enrichment" flip
(`openspec/changes/archive/2026-06-03-stage0-deterministic-discovery`) — this change mirrors
that proven pattern at Stage 2.

## Goals / Non-Goals

**Goals:**
- Deterministically produce the archetypes that actually win, including `trend_with_gate`.
- Bounded fan-out (≤3 archetypes/symbol) — no combinatorial explosion.
- Stage 2 default path needs no LLM / API keys and never FAILs a symbol on swarm error.
- Reuse the already-validated IC-sign entry logic; add no new templating mechanism.
- Don't waste Stage 4 sweeps on hopeless archetypes (fail-fast guard).

**Non-Goals:**
- C(n,k) exhaustive factor enumeration (multiple-testing inflation, compute blow-up).
- LLM-authored structured YAML (non-deterministic; the Stage 0 lesson).
- Auto-"inventing" new archetypes — the fixed 3 cover single / gated / consensus.
- Changing Stage 5 scoring (it already weight-ranks all candidates).
- Changing the strategy YAML schema or the stage2b compiler DSL.

## Decisions

### D1 — Archetype router as a pure function in a new lib module
New `research/pipeline/lib/archetype_router.py` exposing
`pick_archetypes(factors) -> list[ArchetypePlan]`, where each `ArchetypePlan` names the
archetype and the **role-assigned factor subset** (e.g. trend factor + gate factor). Pure,
network-free, unit-testable — same placement as `lib/factor_selector.py` (Stage 0).

Rules (bounded, deterministic):
- `single_factor`: always, for the top-1 |IC| usable factor.
- `trend_with_gate`: only when both a positive-IC ("trend") and a negative-IC ("gate")
  factor exist; pairs top-|IC| trend with top-|IC| gate.
- `consensus_all`: only when 2 ≤ n ≤ 3 usable factors; AND of all.

A symbol with one factor → just `single_factor`. With a trend+gate pair → `single_factor` +
`trend_with_gate` (+ `consensus_all` if 2–3). Capped at 3.

**Alternative considered**: Jinja `.j2` templates per archetype (the original backlog
sketch). Rejected — adds a second spec-generation mechanism alongside the Python
dict-builder, more files to keep in sync with the schema, and the readability win is small
because `yaml.safe_dump` already emits block style. Reusing `build_strategy_spec` keeps the
IC-sign logic single-sourced.

### D2 — Per-archetype builders extend `build_strategy_spec`
Refactor `build_strategy_spec()` to accept an `ArchetypePlan` (archetype name + role-tagged
factors) and branch:
- `single_factor` → 1 condition, `logic: all`.
- `trend_with_gate` → 2 conditions (trend extreme AND gate extreme), `logic: all`,
  description mirrors btc_s9 ("capital inflowing AND longs not crowded").
- `consensus_all` → n conditions, `logic: all` for 2 / `any` for 3 (keep the existing
  sparsity rule from `_entry_logic_note`).
`strategy_id` becomes `<coin>_s<seq>_<archetype>` with `seq` incrementing across the fanned
archetypes for that symbol. `_entry_condition`, `_factor_is_trend`, exit rules, sizing,
`parameter_search_ranges` reuse the current defaults.

### D3 — Swarm demoted to optional enrichment (mirror Stage 0)
- Add `--use-swarm` / `RESEARCH_STAGE2_USE_SWARM` (default OFF).
- Default: skip `run_swarm()` entirely; `generation.rationale` gets a deterministic
  placeholder; `source_run = None`; `method` notes "deterministic scaffold".
- With the flag: run the swarm **fail-soft** — any failure/timeout/parse-miss keeps the
  deterministic rationale and continues (exit 0), exactly like Stage 0's enrichment.
- Remove `STRATEGIES_PER_SYMBOL`; the emit loop iterates the router's plans.

### D4 — Stage 3 fail-fast guard via `archetype_misfit`
In `stage3_backtest.py`, after the **base** run's metrics are available, evaluate a guard:
base sharpe < −2 OR annualised trades > 1000 → mark the strategy `archetype_misfit` and skip
its regime/oos/sweep runs. Surface it in the run summary and in a field Stage 4 reads so the
sweep is not attempted. This keeps the bounded fan-out cheap even when one archetype is a
dud (avoids the eth_s1 "200 combos all negative" burn).

**Alternative**: guard inside Stage 4. Rejected — by Stage 4 the base backtest already ran;
catching it at Stage 3 (where the base metric first exists) skips the most expensive step
(the sweep) and keeps Stage 4 simple.

### D5 — strategy_runs.json registration
`strategy_runs.py` registers an entry per emitted strategy (base/regime/oos/walk_forward run
names), so N strategies/symbol flow through Stage 3/4/5 unchanged. Existing entries are
preserved (the generator is additive / idempotent per strategy_id).

## Risks / Trade-offs

- **[Fan-out inflates Stage 3/4 compute]** → bounded ≤3/symbol + D4 fail-fast guard skips
  duds before the expensive sweep.
- **[More candidates = more multiple-testing surface]** → fan-out is a fixed small set of
  economically-motivated archetypes, not data-mined combinations; Stage 1 IC gate + Stage 5
  OOS gate still arbitrate. Documented as a caveat in emitted specs.
- **[`trend_with_gate` needs both an IC+ and an IC− factor]** → when absent, the router
  simply omits it (no synthetic gate). Symbols like a pure-trend universe degrade to
  `single_factor` + `consensus_all`.
- **[Removing `STRATEGIES_PER_SYMBOL` changes downstream counts]** → workflow-breaking but
  contained; `strategy_runs.json` regenerates. Existing hand-written strategies are untouched
  (additive registration by id).
- **[Swarm default-off changes generation.rationale provenance]** → acceptable; rationale was
  already Tier-3 post-hoc audit prose, not evidence. `--use-swarm` restores it.

## Migration Plan

1. Land `archetype_router.py` + builders + tests (no behavior change until wired).
2. Wire Stage 2 emit loop + `--use-swarm` default-off; regenerate strategies for configured
   symbols.
3. Add Stage 3 fail-fast guard.
4. Regenerate `strategy_runs.json`; run Stage 3→5 to confirm new candidates flow and rank.
Rollback: revert the wiring commit; `archetype_router.py` is dead code until called.

## Open Questions

- Should `trend_with_gate` also emit when there are two same-sign factors (no natural gate),
  using the weaker factor as a pseudo-gate? Current decision: **no** — only emit with a real
  IC-sign gate; revisit if a target universe lacks sign diversity.
- Exact `archetype_misfit` thresholds (sharpe < −2, >1000 trades/yr) are starting heuristics
  from the eth_s1 burn; may need per-symbol tuning after first multi-symbol run.
