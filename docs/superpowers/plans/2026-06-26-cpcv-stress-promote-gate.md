# CPCV + stress required-to-promote Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require cost-stress (always) and CPCV (when the grid has >1 combo) as present-and-passing validations before a strategy can be auto-selected or promoted, via a distinct NOT_TESTED state — not by polluting FATAL.

**Architecture:** `emit_manifest` computes `gate.not_tested` (missing required validations) from the gate thresholds + the strategy's combo count. `decide_selected` (stage5) gains a `validations_ok` flag; the promote endpoint hard-blocks on `gate.not_tested`. Missing data = NOT_TESTED (hard, run it); present-but-failing = existing override path.

**Tech Stack:** Python, pydantic, pytest, FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-06-26-cpcv-stress-promote-gate-design.md`

---

## File Structure

- **Modify** `dashboard/server/schemas.py` — `GateBlock.not_tested: List[str]`.
- **Modify** `dashboard/server/test_schemas.py`.
- **Modify** `research/emit_manifest.py` — `cpcv_required_for`, `compute_not_tested`, `required_validations_ok` helpers; set `gate.not_tested` when assembling the manifest.
- **Modify** `research/tests/test_emit_manifest.py`.
- **Modify** `research/pipeline/stage5_select.py` — `decide_selected` gains `validations_ok`; call site computes it from the full gate.
- **Modify** `research/tests/test_stage5_select.py`.
- **Modify** `dashboard/server/main.py` — NOT_TESTED hard block in `promote_strategy`.
- **Modify** `dashboard/server/test_main.py`.

**⚠ Two pytest scopes:** research (`cd research && python -m pytest tests/<f> -v`), dashboard (`cd dashboard/server && python -m pytest <f> -v`). Never mix.

Order: 1 (schema) → 2 (research helpers) → 3 (emit wiring) → 4 (decide_selected + stage5) → 5 (promote). Helpers are pure/tested; emit + stage5 + promote wiring reuse them.

---

### Task 1: `GateBlock.not_tested` schema field

**Files:**
- Modify: `dashboard/server/schemas.py` (`GateBlock`, ~line 430)
- Test: `dashboard/server/test_schemas.py`

- [ ] **Step 1: Write the failing test**

Add to `dashboard/server/test_schemas.py`:

```python
def test_gate_block_not_tested_defaults_empty():
    from schemas import GateBlock, GateThreshold
    g = GateBlock(source_run="r", thresholds=[
        GateThreshold(name="min_sharpe", threshold=1.0, actual=1.2, passed=True, fatal=False)
    ], overall_pass=True, fatal_fail=False)
    assert g.not_tested == []
    g2 = GateBlock(source_run="r", thresholds=[], overall_pass=False, fatal_fail=False,
                   not_tested=["cpcv", "cost_stress"])
    assert g2.not_tested == ["cpcv", "cost_stress"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && python -m pytest test_schemas.py::test_gate_block_not_tested_defaults_empty -v`
Expected: FAIL — `GateBlock` has no `not_tested`.

- [ ] **Step 3: Add the field**

In `dashboard/server/schemas.py`, in `GateBlock`, add:

```python
    not_tested: List[str] = Field(
        default_factory=list,
        description="Required validations whose data is absent (e.g. 'cost_stress', 'cpcv'). Empty = all present.",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && python -m pytest test_schemas.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/schemas.py dashboard/server/test_schemas.py
git commit -m "feat(schemas): GateBlock.not_tested for NOT_TESTED promote state"
```

---

### Task 2: Validation helpers (pure)

**Files:**
- Modify: `research/emit_manifest.py` (new module-level helpers)
- Test: `research/tests/test_emit_manifest.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_emit_manifest.py`:

```python
class TestValidationHelpers:
    def _gate(self, names_actual_passed):
        from schemas import GateBlock, GateThreshold
        ts = [GateThreshold(name=n, threshold=0.0, actual=a, passed=p, fatal=False)
              for (n, a, p) in names_actual_passed]
        return GateBlock(source_run="r", thresholds=ts, overall_pass=True, fatal_fail=False)

    def test_cpcv_required_for(self):
        from emit_manifest import cpcv_required_for
        assert cpcv_required_for({"a": [1, 2, 3]}) is True        # 3 combos
        assert cpcv_required_for({"a": [20]}) is False            # single value
        assert cpcv_required_for({}) is False                     # empty
        assert cpcv_required_for({"a": [1, 2], "b": [3, 4]}) is True  # 4 combos

    def test_compute_not_tested(self):
        from emit_manifest import compute_not_tested
        # stress present (actual not None) + cpcv present -> nothing missing
        g = self._gate([("alpha_not_fee_illusion", 0.5, True), ("cpcv_mean_sharpe", 1.2, True)])
        assert compute_not_tested(g, cpcv_required=True) == []
        # stress absent (actual None) -> cost_stress missing
        g = self._gate([("alpha_not_fee_illusion", None, False)])
        assert compute_not_tested(g, cpcv_required=False) == ["cost_stress"]
        # cpcv required but absent -> cpcv missing
        g = self._gate([("alpha_not_fee_illusion", 0.5, True)])
        assert compute_not_tested(g, cpcv_required=True) == ["cpcv"]
        # cpcv NOT required + absent -> not listed
        assert compute_not_tested(g, cpcv_required=False) == []

    def test_required_validations_ok(self):
        from emit_manifest import required_validations_ok, compute_not_tested
        # all present + passing
        g = self._gate([("alpha_not_fee_illusion", 0.5, True),
                        ("cpcv_mean_sharpe", 1.2, True), ("cpcv_p05_sharpe", 0.1, True)])
        g.not_tested = compute_not_tested(g, cpcv_required=True)
        assert required_validations_ok(g) is True
        # cpcv present but failing -> not ok
        g = self._gate([("alpha_not_fee_illusion", 0.5, True),
                        ("cpcv_mean_sharpe", 0.8, False), ("cpcv_p05_sharpe", 0.1, True)])
        g.not_tested = compute_not_tested(g, cpcv_required=True)
        assert required_validations_ok(g) is False
        # missing data -> not ok
        g = self._gate([("alpha_not_fee_illusion", None, False)])
        g.not_tested = ["cost_stress"]
        assert required_validations_ok(g) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_emit_manifest.py::TestValidationHelpers -v`
Expected: FAIL — helpers not defined.

- [ ] **Step 3: Implement the helpers**

Add to `research/emit_manifest.py` (module level; add `from pipeline.stage4_optimize import expand_param_ranges` to the imports):

```python
def cpcv_required_for(parameter_search_ranges: dict) -> bool:
    """True when the sweep grid has >1 combo (CPCV can re-select per path).
    A {} or single-value grid (<=1 combo) is exempt (agy: total_combos <= 1)."""
    expanded = expand_param_ranges(parameter_search_ranges or {})
    total = 1
    for vals in expanded.values():
        total *= max(len(vals), 1)
    return len(expanded) > 0 and total > 1


def compute_not_tested(gate, cpcv_required: bool) -> list[str]:
    """Names of required validations whose DATA is absent. Empty = all present."""
    by = {t.name: t for t in gate.thresholds}
    afi = by.get("alpha_not_fee_illusion")
    stress_present = afi is not None and afi.actual is not None
    cpcv_present = "cpcv_mean_sharpe" in by
    out: list[str] = []
    if not stress_present:
        out.append("cost_stress")
    if cpcv_required and not cpcv_present:
        out.append("cpcv")
    return out


def required_validations_ok(gate) -> bool:
    """True iff no required validation data is missing AND every present
    cost-stress / CPCV threshold passed. Drives selection + is the promote
    NOT_TESTED source of truth (via gate.not_tested for the missing case)."""
    if gate.not_tested:
        return False
    by = {t.name: t for t in gate.thresholds}
    afi = by.get("alpha_not_fee_illusion")
    if afi is None or not afi.passed:
        return False
    for name in ("cpcv_mean_sharpe", "cpcv_p05_sharpe"):
        t = by.get(name)
        if t is not None and not t.passed:
            return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_emit_manifest.py::TestValidationHelpers -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/emit_manifest.py research/tests/test_emit_manifest.py
git commit -m "feat(emit_manifest): cpcv_required / not_tested / required_validations_ok helpers"
```

---

### Task 3: Set `gate.not_tested` when emitting a manifest

**Files:**
- Modify: `research/emit_manifest.py` (the per-strategy assembly path — find `emit_manifest_for_strategy` / where `gate = compute_gate(...)` is set on the manifest)
- Test: `research/tests/test_emit_manifest.py`

- [ ] **Step 1: Write the failing test**

Add to `research/tests/test_emit_manifest.py`:

```python
def test_emitted_gate_carries_not_tested(tmp_path, monkeypatch):
    # A gate with no cpcv + no stress, grid >1 combo -> not_tested = [cost_stress, cpcv]
    from emit_manifest import compute_not_tested, cpcv_required_for
    from schemas import GateBlock, GateThreshold
    g = GateBlock(source_run="r", thresholds=[
        GateThreshold(name="alpha_not_fee_illusion", threshold=0.0, actual=None, passed=False, fatal=False),
    ], overall_pass=False, fatal_fail=False)
    cpcv_req = cpcv_required_for({"lookback_days": [30, 60, 90]})
    g.not_tested = compute_not_tested(g, cpcv_req)
    assert set(g.not_tested) == {"cost_stress", "cpcv"}
```

(This locks the compose logic; the wiring itself is verified by the full-suite run in Step 4.)

- [ ] **Step 2: Run test to verify it passes the helper composition**

Run: `cd research && python -m pytest tests/test_emit_manifest.py::test_emitted_gate_carries_not_tested -v`
Expected: PASS (helpers from Task 2 already exist).

- [ ] **Step 3: Wire into the manifest assembly**

In `research/emit_manifest.py`, in the function that builds the per-strategy manifest (where `gate = compute_gate(backtest, optimization, cpcv)` is produced), immediately after computing `gate`, read the strategy's grid and set `not_tested`:

```python
    # ── NOT_TESTED: required validations whose data is absent ────────────────────
    psr = {}
    yaml_path = _REPO_ROOT / "research" / "strategies" / f"strategy_{strategy_id}.yaml"
    if yaml_path.exists():
        import yaml as _yaml
        spec_doc = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        psr = spec_doc.get("parameter_search_ranges", {}) or {}
    gate.not_tested = compute_not_tested(gate, cpcv_required_for(psr))
```

(`strategy_id` and `_REPO_ROOT` are already in scope in the assembly function.)

- [ ] **Step 4: Run the emit_manifest suite**

Run: `cd research && python -m pytest tests/test_emit_manifest.py -v`
Expected: all PASS (helpers + wiring; existing tests unaffected — `not_tested` defaults `[]`).

- [ ] **Step 5: Commit**

```bash
git add research/emit_manifest.py research/tests/test_emit_manifest.py
git commit -m "feat(emit_manifest): populate gate.not_tested from grid + thresholds"
```

---

### Task 4: `decide_selected` requires validations; stage5 computes it

**Files:**
- Modify: `research/pipeline/stage5_select.py` (`decide_selected` ~line 211; call site ~line 435-439)
- Test: `research/tests/test_stage5_select.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_stage5_select.py` (the existing `decide_selected("proceed", fatal_fail=False)` tests stay green — the new param defaults True):

```python
class TestDecideSelectedValidations:
    def test_validations_ok_default_true_preserves_old(self):
        assert decide_selected("proceed", fatal_fail=False) is True

    def test_validations_not_ok_blocks_selection(self):
        assert decide_selected("proceed", fatal_fail=False, validations_ok=False) is False

    def test_still_needs_proceed_and_not_fatal(self):
        assert decide_selected("back_to_stage_4", fatal_fail=False, validations_ok=True) is False
        assert decide_selected("proceed", fatal_fail=True, validations_ok=True) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_stage5_select.py::TestDecideSelectedValidations -v`
Expected: FAIL — `decide_selected()` has no `validations_ok` kwarg.

- [ ] **Step 3: Add the `validations_ok` param**

In `research/pipeline/stage5_select.py`, change `decide_selected`:

```python
def decide_selected(recommended_action: str, fatal_fail: bool, validations_ok: bool = True) -> bool:
    """selected requires: recommended_action == "proceed", the strategy did not
    hard-fail a FATAL gate, AND the required validations (cost-stress always; CPCV
    when the grid has >1 combo) are present and passing (`validations_ok`)."""
    return recommended_action == _SELECTED_ACTION and not fatal_fail and validations_ok
```

- [ ] **Step 4: Compute `validations_ok` at the call site**

In `stage5_select.py`, replace the selection block (currently `fatal_fail = compute_gate(backtest_block)... ; selected_flag = decide_selected(action, fatal_fail)`) with a full-gate build:

```python
        backtest_block = build_backtest_block(entry, runs_root)
        strategy_dir = manifests_dir / strategy_id
        optimization = _load_optional_block(strategy_dir / "optimization.json", OptimizationBlock)
        cpcv = _load_optional_block(strategy_dir / "cpcv.json", CPCVBlock)
        if backtest_block is not None:
            gate = compute_gate(backtest_block, optimization, cpcv)
            yaml_path = strategies_dir / f"strategy_{strategy_id}.yaml"
            psr = {}
            if yaml_path.exists():
                psr = (_yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}).get(
                    "parameter_search_ranges", {}) or {}
            gate.not_tested = compute_not_tested(gate, cpcv_required_for(psr))
            fatal_fail = gate.fatal_fail
            validations_ok = required_validations_ok(gate)
        else:
            fatal_fail, validations_ok = True, False
        selected_flag = decide_selected(action, fatal_fail, validations_ok)
```

Add the imports at the top of `stage5_select.py`:

```python
import yaml as _yaml
from emit_manifest import (
    compute_not_tested, cpcv_required_for, required_validations_ok,
)
from schemas import CPCVBlock, OptimizationBlock

def _load_optional_block(path, model):
    if not path.exists():
        return None
    try:
        import json as _json
        return model.model_validate(_json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None
```

(`strategies_dir` / `manifests_dir` are already resolved in the surrounding function; reuse them.)

- [ ] **Step 5: Run tests to verify all pass**

Run: `cd research && python -m pytest tests/test_stage5_select.py -v`
Expected: all PASS (old decide_selected tests + new validations tests).

- [ ] **Step 6: Run the broader research suite**

Run: `cd research && python -m pytest tests/test_stage5_select.py tests/test_emit_manifest.py -q`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add research/pipeline/stage5_select.py research/tests/test_stage5_select.py
git commit -m "feat(stage5): require CPCV+stress validations for selected=True"
```

---

### Task 5: promote endpoint NOT_TESTED hard block

**Files:**
- Modify: `dashboard/server/main.py` (`promote_strategy`, between line 360 and 362)
- Test: `dashboard/server/test_main.py`

- [ ] **Step 1: Write the failing test**

Add to `dashboard/server/test_main.py` (reuse the existing repo_root/manifest fixture pattern; write a manifest whose gate has `not_tested`):

```python
def test_promote_blocks_on_not_tested(repo_root, tmp_path):
    """A manifest with gate.not_tested is hard-blocked with a NOT_TESTED message."""
    _write_manifest_with_gate(  # helper that writes research/manifests/<id>/manifest.json
        repo_root, "strat_nt_001",
        gate={"source_run": "r", "thresholds": [], "overall_pass": False,
              "fatal_fail": False, "not_tested": ["cost_stress", "cpcv"]},
    )
    with TestClient(app) as c:
        r = c.post("/api/strategies/strat_nt_001/promote", json={})
        assert r.status_code == 422
        assert "validation" in r.json()["detail"].lower()
        assert "cost_stress" in r.json()["detail"] or "cpcv" in r.json()["detail"]
```

If no `_write_manifest_with_gate` helper exists, add one mirroring the existing manifest-writing fixture used by `test_promote_status_roundtrip` (write a minimal valid `StrategyManifest` JSON with the given `gate` into `repo_root/research/manifests/strat_nt_001/manifest.json`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && python -m pytest test_main.py::test_promote_blocks_on_not_tested -v`
Expected: FAIL — currently returns 201 or a different 422 (no NOT_TESTED block).

- [ ] **Step 3: Add the NOT_TESTED block**

In `dashboard/server/main.py`, in `promote_strategy`, immediately after the fatal-gate block (ends line 360) and before the non-fatal override check (line 362), insert:

```python
    # Required validations not run yet — hard block (distinct from FATAL, not overridable)
    if manifest.gate and manifest.gate.not_tested:
        missing = manifest.gate.not_tested
        raise HTTPException(
            status_code=422,
            detail=(
                f"Not testable yet — missing required validation(s): {missing}. "
                "Run cost-stress" + (" and CPCV" if "cpcv" in missing else "")
                + " before promoting."
            ),
        )
```

- [ ] **Step 4: Run the main suite**

Run: `cd dashboard/server && python -m pytest test_main.py -v`
Expected: all PASS (new NOT_TESTED test + existing promote tests — existing fixtures have `not_tested` defaulting `[]`, so they are unaffected).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/main.py dashboard/server/test_main.py
git commit -m "feat(promote): NOT_TESTED hard block when CPCV/stress data missing"
```

---

## Verification checklist (after all tasks)

- [ ] `cd research && python -m pytest tests/test_emit_manifest.py tests/test_stage5_select.py -v` — green.
- [ ] `cd dashboard/server && python -m pytest test_schemas.py test_main.py -v` — green.
- [ ] `≤1`-combo grid (eth_s5) → `cpcv` never in `not_tested`; cost-stress still required.
- [ ] Missing data → NOT_TESTED hard block (distinct message, not the FATAL one); present-but-failing → existing override path unchanged.
- [ ] Old manifests (`not_tested` defaulting `[]`) promote exactly as before.
- [ ] `decide_selected("proceed", fatal_fail=False)` (2-arg) still returns True — no existing caller broken.
