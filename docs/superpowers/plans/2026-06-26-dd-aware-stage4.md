# DD-aware Stage 4 combo selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Stage 4 grid sweep pick `best` under the same drawdown budget the deploy gate enforces, so a DD-busting combo can never become `best` and the operator never re-tunes against the OOS holdout.

**Architecture:** Add a hard `max_drawdown ≤ GATE_MAX_DRAWDOWN` gate to `rank_combos`, layered *after* the existing `trade_count` gate (so DD never weakens trade_count). When no combo in the pool meets the budget, `rank_combos` returns empty and `_optimize_strategy` fail-soft skips the strategy (no `optimization.json`, no OOS holdout) — the contamination firewall. All selection logic; no backtest-engine change.

**Tech Stack:** Python, pytest. Single file `research/pipeline/stage4_optimize.py` + its test `research/tests/test_stage4_optimize.py`.

**Spec:** `docs/superpowers/specs/2026-06-26-dd-aware-stage4-design.md`

---

## File Structure

- **Modify** `research/pipeline/stage4_optimize.py`
  - add `import math`
  - extend the `from schemas import …` line with `GATE_MAX_DRAWDOWN`; add module constant `DD_CEILING`
  - add `ComboResult.max_drawdown` property
  - rewrite `rank_combos` (hierarchical trade_count → DD gate)
  - add pure helper `is_all_bust_dd(results, ranked) -> bool`
  - add DD-pass count line to `_summarise`
  - wire the skip into `_optimize_strategy`
- **Modify** `research/tests/test_stage4_optimize.py`
  - extend the `_combo` helper with a `max_dd` kwarg (default passing)
  - add DD-gate ranking tests, `max_drawdown` property tests, `is_all_bust_dd` tests, `_summarise` test

Testing boundary follows the file's established convention: **only pure functions are unit-tested** (no subprocess / backtest / filesystem). `_optimize_strategy` is verified by the full suite staying green; its skip decision is extracted into the pure, tested `is_all_bust_dd`.

**Run tests with:** `cd research && python -m pytest tests/test_stage4_optimize.py -v`
(Research and dashboard pytest suites run separately — do not run them together.)

---

### Task 1: `ComboResult.max_drawdown` property

**Files:**
- Modify: `research/pipeline/stage4_optimize.py` (imports; `ComboResult` dataclass, after the `trade_count` property ~line 113-121)
- Test: `research/tests/test_stage4_optimize.py` (`TestComboResultProperties`)

- [ ] **Step 1: Write the failing tests**

Add to `class TestComboResultProperties` in `research/tests/test_stage4_optimize.py`:

```python
    def test_max_drawdown_parses_and_abs(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"sharpe": "1.0", "trade_count": "30",
                                 "max_drawdown": "-0.12"})
        assert c.max_drawdown == 0.12  # abs() applied

    def test_max_drawdown_missing_field_is_inf(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"sharpe": "1.0", "trade_count": "30"})
        assert c.max_drawdown == float("inf")

    def test_max_drawdown_none_metrics_is_inf(self):
        c = ComboResult(idx=0, overrides={}, run_name="x", metrics=None)
        assert c.max_drawdown == float("inf")

    def test_max_drawdown_unparseable_is_inf(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"max_drawdown": "n/a"})
        assert c.max_drawdown == float("inf")

    def test_max_drawdown_nan_string_is_inf(self):
        # float("NaN") does not raise; abs(nan) is nan. Must be normalised to inf.
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"max_drawdown": "NaN"})
        assert c.max_drawdown == float("inf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestComboResultProperties -v`
Expected: the 5 new tests FAIL with `AttributeError: 'ComboResult' object has no attribute 'max_drawdown'`.

- [ ] **Step 3: Add `import math` and the property**

In `research/pipeline/stage4_optimize.py`, add `import math` with the other stdlib imports (the block starting `import argparse` near line 41):

```python
import argparse
import csv
import dataclasses
import json
import math
import random
import re
import subprocess
import shutil
```

Add the property to the `ComboResult` dataclass, immediately after the `trade_count` property:

```python
    @property
    def max_drawdown(self) -> float:
        """Max drawdown as a positive fraction. inf when absent/unparseable/NaN
        so a combo with no usable DD conservatively fails the DD gate."""
        if not self.metrics:
            return float("inf")
        raw = self.metrics.get("max_drawdown")
        try:
            val = abs(float(raw))
        except (TypeError, ValueError):
            return float("inf")
        return float("inf") if math.isnan(val) else val
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestComboResultProperties -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage4_optimize.py research/tests/test_stage4_optimize.py
git commit -m "feat(stage4): ComboResult.max_drawdown property with NaN guard"
```

---

### Task 2: DD-aware `rank_combos` (hierarchical gate)

**Files:**
- Modify: `research/pipeline/stage4_optimize.py` (`from schemas import …` line ~73; add `DD_CEILING`; rewrite `rank_combos` ~line 401-411)
- Test: `research/tests/test_stage4_optimize.py` (`_combo` helper ~line 263; `TestRankCombos`)

- [ ] **Step 1: Extend the `_combo` test helper with a `max_dd` kwarg**

Replace the existing `_combo` helper (just above `class TestRankCombos`) with:

```python
def _combo(idx: int, sharpe, trade_count, *, metrics: bool = True,
           max_dd: float = 0.05) -> ComboResult:
    m = None
    if metrics:
        m = {"sharpe": str(sharpe), "trade_count": str(trade_count),
             "max_drawdown": str(max_dd)}
    return ComboResult(idx=idx, overrides={}, run_name=f"s_{idx:03d}", metrics=m)
```

(Default `max_dd=0.05` passes the 0.10 ceiling, so the existing `TestRankCombos` assertions are unchanged.)

- [ ] **Step 2: Write the failing DD-gate tests**

Add to `class TestRankCombos`:

```python
    def test_low_dd_preferred_over_higher_sharpe_high_dd(self):
        # A: higher sharpe but busts DD; B: lower sharpe, passes DD.
        combos = [
            _combo(0, 2.0, 50, max_dd=0.15),  # busts DD → excluded
            _combo(1, 1.0, 50, max_dd=0.05),  # passes
        ]
        ranked = rank_combos(combos)
        assert [c.idx for c in ranked] == [1]

    def test_sub_min_trade_combo_not_selected_when_others_pass(self):
        # trade_count gate must not be weakened by the DD layer: a 2-trade
        # high-sharpe combo never wins when a real combo passes trade_count.
        combos = [
            _combo(0, 3.0, 2, max_dd=0.05),   # below trade gate
            _combo(1, 1.0, 50, max_dd=0.05),  # passes both
        ]
        ranked = rank_combos(combos)
        assert [c.idx for c in ranked] == [1]

    def test_all_bust_dd_returns_empty(self):
        combos = [
            _combo(0, 2.0, 50, max_dd=0.20),
            _combo(1, 1.0, 50, max_dd=0.15),
        ]
        assert rank_combos(combos) == []

    def test_dd_boundary_inclusive(self):
        # exactly 0.10 passes; 0.1001 fails.
        combos = [
            _combo(0, 1.0, 50, max_dd=0.10),
            _combo(1, 2.0, 50, max_dd=0.1001),
        ]
        ranked = rank_combos(combos)
        assert [c.idx for c in ranked] == [0]
```

- [ ] **Step 3: Run tests to verify the new ones fail (and old ones still pass)**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestRankCombos -v`
Expected: the 4 new tests FAIL (current `rank_combos` ignores DD, so e.g. `test_all_bust_dd_returns_empty` returns 2 combos, not `[]`); the 4 pre-existing tests PASS.

- [ ] **Step 4: Add the import, constant, and rewrite `rank_combos`**

In `research/pipeline/stage4_optimize.py`, extend the schemas import (~line 73):

```python
from schemas import GATE_MAX_DRAWDOWN, OptimizationBlock, StrategySpec  # noqa: E402
```

Add a module constant next to the other gate constants (near `MIN_TRADE_COUNT_GATE`, ~line 81):

```python
DD_CEILING = GATE_MAX_DRAWDOWN  # 0.10 — deploy single source of truth (schemas.py)
```

Replace `rank_combos` with:

```python
def rank_combos(
    combos: list[ComboResult],
    min_trades: int = MIN_TRADE_COUNT_GATE,
    dd_ceiling: float = DD_CEILING,
) -> list[ComboResult]:
    """Return combos sorted best→worst under two hard gates.

    Gate 1 (trade_count): combos with trade_count >= min_trades. Falls back to
    all valid combos when none qualify (unchanged legacy behaviour).
    Gate 2 (max_drawdown): of that pool, keep combos with train max_drawdown
    <= dd_ceiling. This gate has NO fallback — if none qualify, the result is
    empty and the caller fail-soft skips the strategy (no DD-busting `best`).
    Survivors are ranked by sharpe desc.
    """
    valid = [c for c in combos if c.metrics is not None]
    trade_gated = [c for c in valid if c.trade_count >= min_trades]
    pool = trade_gated if trade_gated else valid
    dd_gated = [c for c in pool if c.max_drawdown <= dd_ceiling]
    return sorted(dd_gated, key=lambda c: c.sharpe, reverse=True)
```

- [ ] **Step 5: Run tests to verify all pass**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestRankCombos tests/test_stage4_optimize.py::TestComboResultProperties -v`
Expected: all PASS (4 old + 4 new ranking tests, plus Task 1 property tests).

- [ ] **Step 6: Commit**

```bash
git add research/pipeline/stage4_optimize.py research/tests/test_stage4_optimize.py
git commit -m "feat(stage4): DD ceiling gate in rank_combos (layered after trade_count)"
```

---

### Task 3: `is_all_bust_dd` skip-decision helper

**Files:**
- Modify: `research/pipeline/stage4_optimize.py` (new pure function near `rank_combos`)
- Test: `research/tests/test_stage4_optimize.py` (new `TestIsAllBustDd` class)

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_stage4_optimize.py` (and add `is_all_bust_dd` to the `from pipeline.stage4_optimize import (...)` block at the top):

```python
class TestIsAllBustDd:
    def test_true_when_metrics_exist_but_none_survive_dd(self):
        results = [_combo(0, 2.0, 50, max_dd=0.20), _combo(1, 1.0, 50, max_dd=0.15)]
        ranked = rank_combos(results)  # empty
        assert is_all_bust_dd(results, ranked) is True

    def test_false_when_a_combo_survives(self):
        results = [_combo(0, 1.0, 50, max_dd=0.05)]
        ranked = rank_combos(results)
        assert is_all_bust_dd(results, ranked) is False

    def test_false_when_no_combo_has_metrics(self):
        # all-errored case: do NOT skip via the DD branch (keeps best=None path).
        results = [_combo(0, 0.0, 0, metrics=False), _combo(1, 0.0, 0, metrics=False)]
        ranked = rank_combos(results)  # empty
        assert is_all_bust_dd(results, ranked) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestIsAllBustDd -v`
Expected: FAIL — `ImportError` / `is_all_bust_dd` not defined.

- [ ] **Step 3: Implement the helper**

Add to `research/pipeline/stage4_optimize.py`, directly below `rank_combos`:

```python
def is_all_bust_dd(
    results: list[ComboResult], ranked: list[ComboResult]
) -> bool:
    """True when combos produced metrics but none survived the DD gate.

    Distinguishes "every combo busts the train DD budget" (→ fail-soft skip,
    no OOS) from "no combo produced metrics at all" (→ keep the best=None
    error path). ``ranked`` is the output of ``rank_combos(results)``.
    """
    have_metrics = any(r.metrics is not None for r in results)
    return have_metrics and not ranked
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestIsAllBustDd -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage4_optimize.py research/tests/test_stage4_optimize.py
git commit -m "feat(stage4): is_all_bust_dd skip-decision helper"
```

---

### Task 4: `_summarise` reports the DD gate

**Files:**
- Modify: `research/pipeline/stage4_optimize.py` (`_summarise` ~line 414-436)
- Test: `research/tests/test_stage4_optimize.py` (new `TestSummarise` class)

- [ ] **Step 1: Write the failing test**

Add to `research/tests/test_stage4_optimize.py` (add `_summarise` and `DD_CEILING` to the import block):

```python
class TestSummarise:
    def test_reports_dd_pass_count(self):
        combos = [
            _combo(0, 1.5, 50, max_dd=0.05),  # passes DD
            _combo(1, 1.0, 50, max_dd=0.20),  # busts DD
            _combo(2, 0.0, 0, metrics=False),  # no metrics
        ]
        ranked = rank_combos(combos)
        out = _summarise(combos, ranked)
        assert f"combos passing max_drawdown <= {DD_CEILING}: 1" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestSummarise -v`
Expected: FAIL — the DD line is not in the summary yet.

- [ ] **Step 3: Add the DD-pass line to `_summarise`**

In `_summarise`, after the `n_gated = …` line, add the DD count, and add the report line after the existing `trade_count` bullet:

```python
    n_gated = sum(1 for c in ranked if c.trade_count >= MIN_TRADE_COUNT_GATE)
    n_dd_pass = sum(
        1 for c in combos
        if c.metrics is not None and c.max_drawdown <= DD_CEILING
    )

    lines = [
        f"# Stage 4 grid sweep summary",
        f"",
        f"- combos attempted: {n_total}",
        f"- combos with metrics: {n_with_metrics}",
        f"- combos with errors: {n_errors}",
        f"- combos passing trade_count >= {MIN_TRADE_COUNT_GATE}: {n_gated}",
        f"- combos passing max_drawdown <= {DD_CEILING}: {n_dd_pass}",
        f"",
        f"## Top {min(top_n, len(ranked))} (by sharpe)",
        f"",
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestSummarise -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage4_optimize.py research/tests/test_stage4_optimize.py
git commit -m "feat(stage4): report DD-gate pass count in sweep summary"
```

---

### Task 5: Wire the fail-soft skip into `_optimize_strategy`

**Files:**
- Modify: `research/pipeline/stage4_optimize.py` (`_optimize_strategy`, at the `ranked = rank_combos(results)` site ~line 623-624)

No new unit test — `_optimize_strategy` runs real backtests via subprocess and is outside the file's pure-function test boundary. Its skip decision is already covered by `TestIsAllBustDd` (Task 3). Verification is the full suite staying green plus a read-through of the wiring.

- [ ] **Step 1: Insert the skip branch**

In `_optimize_strategy`, locate:

```python
    ranked = rank_combos(results)
    best = ranked[0] if ranked else None
```

Replace with:

```python
    ranked = rank_combos(results)
    if is_all_bust_dd(results, ranked):
        msg = (
            f"all {len(results)} combos exceed train DD ceiling {DD_CEILING} — "
            "skipping (redesign for DD; do not re-tune against OOS holdout)"
        )
        print(f"  [SKIP] {msg}")
        return OptimizationCheckResult(
            strategy_id=strategy_id, ok=False, skipped=True, error=msg
        )
    best = ranked[0] if ranked else None
```

This returns before `optimization.json` is written and before the OOS-holdout block runs, so a strategy whose every combo busts the train DD budget produces no OOS numbers. `compute_exit_code` already treats `skipped=True` as non-fatal.

- [ ] **Step 2: Run the full stage4 test file**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py -v`
Expected: all PASS (every class, old and new).

- [ ] **Step 3: Run the broader research suite to catch regressions**

Run: `cd research && python -m pytest -q`
Expected: PASS / no new failures attributable to this change. (If pre-existing unrelated failures exist, confirm they are unchanged from before the task.)

- [ ] **Step 4: Commit**

```bash
git add research/pipeline/stage4_optimize.py
git commit -m "feat(stage4): fail-soft skip when all combos bust train DD budget"
```

---

## Verification checklist (after all tasks)

- [ ] `cd research && python -m pytest tests/test_stage4_optimize.py -v` — all green.
- [ ] Grep confirms `DD_CEILING` is imported from `GATE_MAX_DRAWDOWN`, not redefined as a literal.
- [ ] `rank_combos` returns `[]` for an all-bust-DD set; `_optimize_strategy` skips on it.
- [ ] Existing `TestRankCombos` assertions unchanged (only the `_combo` helper gained a defaulted kwarg).
- [ ] No change to `backtest/` or the OOS-holdout code path other than the early skip return.
