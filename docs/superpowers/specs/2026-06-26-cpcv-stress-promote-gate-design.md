# CPCV + cost-stress required for promotion — Design

**Date:** 2026-06-26
**Status:** Approved (agy-reviewed; pending spec review)
**Scope:** Make CPCV and cost-stress **required** validations for auto-selection
and promotion — close the hole that let an un-validated strategy deploy.

## Problem

CPCV and cost-stress gates are **non-fatal** and only exist in the manifest when
the data was produced. So a strategy that never ran CPCV/stress has no gate for
them and sails through promotion — exactly how `xrp_s2` reached live paper
without any CPCV (its later CPCV was 0.68 / p05 −0.55, a clear fail). "Not run"
currently reads as "not blocked."

## Goal

Promotion and auto-selection require the validation data to **exist and pass**:

- **cost-stress**: required for every strategy.
- **CPCV**: required when the strategy has a real sweep grid (>1 combo). A
  hand-finalized fixed-param strategy (≤1 combo, e.g. `eth_s5_half_size` whose
  `parameter_search_ranges` is `{}`) is **exempt from CPCV but still requires
  cost-stress** — CPCV Fork A has no param surface to re-tune there.

## Key design decision (agy review)

**Enforce with an explicit "required data present" check, NOT by making missing
data `fatal`.** Marking un-run validations `fatal` would paint every freshly
backtested strategy red FATAL while it queues for CPCV → "cry-wolf" numbing →
real OOS-collapse FATALs get ignored (semantic pollution). Instead introduce a
distinct **NOT_TESTED** state, separate from FATAL:

```
promotable = (overall gates handled as today) AND (not fatal_fail) AND (required validations present)
```

## Non-goals (explicit)

- **Not** fixed-param CPCV for empty-grid strategies (agy option B) — a valuable
  follow-up that would give `eth_s5` a real p05, but it must not delay this
  fix. Recorded below.
- **Not** changing the existing non-fatal override path: a present-but-**failing**
  CPCV/stress stays override-able with a reason (a conscious human call on a
  number they can see). Only **missing** data is a hard NOT_TESTED block.
- **Not** touching the CPCV/stress thresholds or their non-fatal-ness.

## Design

### 1. Empty-grid / cpcv-required detection

`cpcv_required = total_combos(parameter_search_ranges) > 1`, where
`total_combos` = product of the expanded dimension lengths (reuse
`stage4_optimize.expand_param_ranges`; a `{}` grid or a single-value grid like
`{"length": [20]}` both yield ≤1 → **exempt**, per agy's "combos ≤ 1" rule, not
a naive `== {}` check).

### 2. `GateBlock.not_tested` (schema)

Add `not_tested: List[str] = Field(default_factory=list)` to `GateBlock`
(dashboard/server/schemas.py). Empty = all required validation data present.
Populated with the names of **missing** required validations, e.g.
`["cost_stress"]`, `["cpcv"]`, or both. Optional-with-default → archived
manifests parse unchanged and read as "nothing missing" (preserves old
behaviour until re-emitted).

### 3. `emit_manifest` computes `not_tested`

`compute_gate` is **unchanged** (stays `(backtest, optimization, cpcv)`). A new
pure helper computes `not_tested` from the finished gate + the combo count, and
the manifest-assembly function (`emit_manifest_for_strategy`, which already knows
`strategy_id` and can read the yaml) sets it on the gate before writing:

```python
def compute_not_tested(gate: GateBlock, cpcv_required: bool) -> list[str]:
    by = {t.name: t for t in gate.thresholds}
    afi = by.get("alpha_not_fee_illusion")
    stress_present = afi is not None and afi.actual is not None
    cpcv_present = "cpcv_mean_sharpe" in by
    out = []
    if not stress_present:
        out.append("cost_stress")
    if cpcv_required and not cpcv_present:
        out.append("cpcv")
    return out
```

`cpcv_required = total_combos(expand_param_ranges(psr)) > 1`, where `psr` is the
strategy yaml's `parameter_search_ranges` and `total_combos` = product of the
expanded dimension lengths. Computed on the research side (reuses
`stage4_optimize.expand_param_ranges`) so the dashboard never imports research
code — it only reads the stored `gate.not_tested`.

### 4. `decide_selected` (stage5) — require validations to pass

Extend the selection rule. `selected` requires, in addition to today's
`proceed AND not fatal_fail`:

- **all required validations satisfied** = present **and passed**:
  - cost-stress satisfied = `alpha_not_fee_illusion` present, `actual not None`, `passed`.
  - cpcv satisfied = `not cpcv_required` OR (`cpcv_mean_sharpe` and
    `cpcv_p05_sharpe` present and passed).

So `selected = (action == "proceed") AND (not fatal_fail) AND required_validations_ok(gate, cpcv_required)`.
`decide_selected` gains the gate (thresholds) and `cpcv_required` as inputs.

### 5. `promote_strategy` (dashboard) — NOT_TESTED hard block

Between the fatal-gate block (main.py:352-360) and the non-fatal-override check
(362-367), insert:

```python
if manifest.gate and manifest.gate.not_tested:
    raise HTTPException(
        status_code=422,
        detail=(
            f"Not testable yet — missing required validation(s): "
            f"{manifest.gate.not_tested}. Run cost-stress"
            + (" and CPCV" if "cpcv" in manifest.gate.not_tested else "")
            + " before promoting."
        ),
    )
```

This is a hard block (like fatal) but with a distinct NOT_TESTED message —
not overridable, because you must actually run the validation. A present-but-
**failing** CPCV/stress leaves `not_tested` empty (data exists) and falls through
to the existing `overall_pass`/override path (a conscious, reasoned override).

## Data flow

`emit_manifest` (research) reads the spec's grid, counts combos, inspects the
gate thresholds, and writes `gate.not_tested`. Both consumers read it:
`decide_selected` (stage5, research) and `promote_strategy` (dashboard) — neither
recomputes the combo count. No cross-package import.

## Edge cases

- Empty / single-value grid → `cpcv_required = False` → `"cpcv"` never in
  `not_tested`; cost-stress still required (eth_s5 promotable once stressed).
- Old manifest without `not_tested` → defaults `[]` → behaves as before until
  re-emitted (no surprise hard-blocks on stale data).
- cost-stress present but **failing** → `not_tested` empty; `alpha_not_fee_illusion`
  fatal (when stress exists) already hard-blocks — unchanged.
- CPCV present but **failing** (e.g. `xrp_s2` 0.68/−0.55) → `not_tested` empty,
  `overall_pass` False → override-with-reason path (conscious human call).

## Testing (TDD)

**schema (`dashboard/server/test_schemas.py`):** `GateBlock.not_tested` defaults `[]`; accepts a list.

**emit_manifest (`research/tests/test_emit_manifest.py`):**
1. grid >1 combo, no CPCV data → `not_tested` contains `"cpcv"`.
2. any strategy, no cost-stress data → `not_tested` contains `"cost_stress"`.
3. ≤1 combo grid, no CPCV → `not_tested` does **not** contain `"cpcv"` (exempt).
4. both present → `not_tested` empty.

**decide_selected (`research/tests/test_stage5_select.py`):**
5. proceed + not fatal + validations satisfied → selected True.
6. proceed + not fatal + cpcv missing (required) → selected False.
7. proceed + not fatal + ≤1 combo + stress satisfied (cpcv exempt) → selected True.
8. proceed + not fatal + cpcv present but failing → selected False.

**promote (`dashboard/server/test_main.py`):**
9. `not_tested` non-empty → 422 with a NOT_TESTED message; distinct from the fatal message.
10. `not_tested` empty + fatal_fail → still the fatal 422 (unchanged).
11. `not_tested` empty + non-fatal fail + no override → existing override-needed 422 (unchanged).

(Research and dashboard pytest suites run separately.)

## Follow-ups (out of scope, recorded)

- **Fixed-param CPCV distribution (agy option B):** treat a fixed-param
  strategy's own params as a 1-combo grid, evaluate across the 120 combinatorial
  test paths → a robustness / p05 distribution for hand-finalized strategies.
  This closes agy's acknowledged blind spot: exempting CPCV hides `eth_s5`'s
  tail risk (it survives stress, but its worst-path drawdown is unmeasured).
- **`NOT_TESTED` surfaced in the dashboard** (badge distinct from GO / NO-GO /
  FATAL) so the UI shows "run validation" rather than a scary red FATAL.
