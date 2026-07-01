# CPCV Validation (#3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in CPCV validation command that replaces the single walk-forward OOS path with a 120-path distribution of out-of-sample Sharpes, gated (non-fatal) on the distribution's mean and 5th percentile.

**Architecture:** A pure lib (`cpcv.py`) does the combinatorics, purge/embargo, pooled Sharpe, and per-path re-selection. A standalone stage (`stage4_cpcv.py`) precomputes a combo×block per-bar-returns matrix (reusing Stage 4's combo helpers), runs the distribution, and writes `cpcv.json`. `emit_manifest` reads it and adds two non-fatal gate thresholds.

**Tech Stack:** Python, numpy, pandas, scipy, pytest. Pydantic schema.

**Spec:** `docs/superpowers/specs/2026-06-26-cpcv-validation-design.md`

---

## File Structure

- **Create** `research/lib/cpcv.py` — `make_blocks`, `combinatorial_splits`, `pooled_sharpe`, `purge_boundary_bars`, `cpcv_distribution` (pure).
- **Create** `research/tests/test_cpcv.py`.
- **Create** `research/pipeline/stage4_cpcv.py` — orchestration command.
- **Modify** `dashboard/server/schemas.py` — `CPCVBlock`, `StrategyManifest.cpcv`, gate constants.
- **Modify** `dashboard/server/test_schemas.py`.
- **Modify** `research/emit_manifest.py` — `compute_gate(..., cpcv=None)`, load `cpcv.json`, thread it.
- **Modify** `research/tests/test_emit_manifest.py`.

**⚠ Two pytest scopes — never mix:**
- Research: `cd research && python -m pytest tests/<file> -v`
- Dashboard: `cd dashboard/server && python -m pytest test_schemas.py -v`

Order: Task 1-3 (lib) → 4 (schema) → 5 (gate) → 6 (orchestration, reuses lib + Stage 4 helpers). The orchestration runs real subprocess backtests and is outside the pure-function test boundary; its logic is carried by the tested lib + a light smoke check.

---

### Task 1: Blocks + combinatorial splits

**Files:**
- Create: `research/lib/cpcv.py`
- Test: `research/tests/test_cpcv.py`

- [ ] **Step 1: Write the failing tests**

Create `research/tests/test_cpcv.py`:

```python
"""Tests for the CPCV lib (pure, no IO).
Run: cd research && python -m pytest tests/test_cpcv.py -v
"""
from __future__ import annotations

import sys
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.cpcv import (  # noqa: E402
    combinatorial_splits,
    make_blocks,
)


class TestMakeBlocks:
    def test_contiguous_non_overlapping_cover(self):
        blocks = make_blocks("2022-01-01", "2023-01-01", 4)
        assert len(blocks) == 4
        assert blocks[0][0] == "2022-01-01"
        assert blocks[-1][1] == "2023-01-01"
        # each block's end == next block's start (contiguous, no gaps/overlap)
        for i in range(len(blocks) - 1):
            assert blocks[i][1] == blocks[i + 1][0]

    def test_raises_on_bad_n(self):
        with pytest.raises(ValueError):
            make_blocks("2022-01-01", "2023-01-01", 1)


class TestCombinatorialSplits:
    def test_count_is_c_n_k(self):
        splits = combinatorial_splits(10, 3)
        assert len(splits) == comb(10, 3) == 120

    def test_train_test_disjoint_and_cover(self):
        for train, test in combinatorial_splits(6, 2):
            assert set(train).isdisjoint(test)
            assert set(train) | set(test) == set(range(6))
            assert len(test) == 2

    def test_raises_when_k_ge_n(self):
        with pytest.raises(ValueError):
            combinatorial_splits(3, 3)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_cpcv.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.cpcv'`.

- [ ] **Step 3: Create the lib with these two functions**

Create `research/lib/cpcv.py`:

```python
"""Combinatorial Purged Cross-Validation (Bailey & López de Prado).

Pure functions; no IO. See docs/superpowers/specs/2026-06-26-cpcv-validation-design.md.
"""
from __future__ import annotations

from datetime import date, timedelta
from itertools import combinations

import numpy as np
import pandas as pd


def make_blocks(start_date: str, end_date: str, n_blocks: int) -> list[tuple[str, str]]:
    """Split [start_date, end_date] into n_blocks contiguous equal-day (start, end)
    ISO ranges. Adjacent blocks share a boundary date (block[i].end == block[i+1].start)."""
    if n_blocks < 2:
        raise ValueError(f"n_blocks must be >= 2, got {n_blocks}")
    d0 = date.fromisoformat(start_date[:10])
    d1 = date.fromisoformat(end_date[:10])
    total = (d1 - d0).days
    if total < n_blocks:
        raise ValueError("date range too short for n_blocks")
    edges = [d0 + timedelta(days=round(total * i / n_blocks)) for i in range(n_blocks + 1)]
    return [(edges[i].isoformat(), edges[i + 1].isoformat()) for i in range(n_blocks)]


def combinatorial_splits(n_blocks: int, k_test: int) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """All C(n_blocks, k_test) (train_block_ids, test_block_ids) index tuples."""
    if not 1 <= k_test < n_blocks:
        raise ValueError(f"require 1 <= k_test < n_blocks, got k={k_test}, n={n_blocks}")
    out: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    all_ids = set(range(n_blocks))
    for test in combinations(range(n_blocks), k_test):
        train = tuple(sorted(all_ids - set(test)))
        out.append((train, tuple(test)))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_cpcv.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/lib/cpcv.py research/tests/test_cpcv.py
git commit -m "feat(cpcv): block splitter + combinatorial splits"
```

---

### Task 2: Pooled Sharpe + boundary purge/embargo

**Files:**
- Modify: `research/lib/cpcv.py`
- Test: `research/tests/test_cpcv.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_cpcv.py` (extend the import to include `pooled_sharpe, purge_boundary_bars`):

```python
from lib.cpcv import pooled_sharpe, purge_boundary_bars  # noqa: E402


def _const_returns(n, val, start_id):
    idx = pd.RangeIndex(start_id, start_id + n)
    return pd.Series([val] * n, index=idx, dtype=float)


class TestPooledSharpe:
    def test_annualised_mean_over_std(self):
        r = pd.Series([0.01, -0.01, 0.02, -0.02, 0.03], dtype=float)
        br = {0: r}
        got = pooled_sharpe(br, [0], bars_per_year=8760)
        exp = r.mean() / r.std() * (8760 ** 0.5)
        assert abs(got - exp) < 1e-9

    def test_pools_multiple_blocks(self):
        br = {0: pd.Series([0.01, 0.01]), 1: pd.Series([0.02, 0.02])}
        got = pooled_sharpe(br, [0, 1], bars_per_year=8760)
        pooled = pd.concat([br[0], br[1]])
        assert abs(got - pooled.mean() / pooled.std() * (8760 ** 0.5)) < 1e-9

    def test_nan_on_too_few_or_zero_std(self):
        assert np.isnan(pooled_sharpe({0: pd.Series([0.01])}, [0], 8760))       # <2
        assert np.isnan(pooled_sharpe({0: _const_returns(5, 0.0, 0)}, [0], 8760))  # zero std


class TestPurgeBoundaryBars:
    def _matrix(self):
        # 4 blocks, 10 bars each, ids 0..3
        return {b: _const_returns(10, 0.01, b * 10) for b in range(4)}

    def test_train_precedes_test_drops_tail(self):
        # train {0,1}, test {2}: block 1 precedes test 2 -> drop tail of block 1
        purged = purge_boundary_bars(self._matrix(), (0, 1), (2,), purge_bars=3, embargo_bars=3)
        assert len(purged[1]) == 7        # tail 3 dropped
        assert len(purged[0]) == 10       # block 0 not adjacent to a test block
        assert list(purged[1].index) == list(range(10, 17))  # kept the HEAD

    def test_train_follows_test_drops_head(self):
        # train {2,3}, test {1}: block 2 follows test 1 -> drop head of block 2
        purged = purge_boundary_bars(self._matrix(), (2, 3), (1,), purge_bars=3, embargo_bars=4)
        assert len(purged[2]) == 6        # head 4 (embargo) dropped
        assert list(purged[2].index) == list(range(24, 30))  # kept the TAIL
        assert len(purged[3]) == 10

    def test_sandwiched_drops_both(self):
        # train {1}, test {0,2}: block 1 follows test 0 AND precedes test 2
        purged = purge_boundary_bars(self._matrix(), (1,), (0, 2), purge_bars=2, embargo_bars=2)
        assert len(purged[1]) == 6        # head 2 + tail 2 dropped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_cpcv.py::TestPooledSharpe tests/test_cpcv.py::TestPurgeBoundaryBars -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement both functions**

Append to `research/lib/cpcv.py`:

```python
def pooled_sharpe(block_returns: dict[int, pd.Series], block_ids, bars_per_year: float) -> float:
    """Annualised Sharpe of the concatenated per-bar returns of `block_ids`.
    nan when <2 pooled bars or zero std (undefined / flat)."""
    parts = [block_returns[i] for i in block_ids if i in block_returns and len(block_returns[i])]
    if not parts:
        return float("nan")
    pooled = pd.concat(parts)
    if len(pooled) < 2:
        return float("nan")
    std = float(pooled.std())
    if std == 0.0:
        return float("nan")
    return float(pooled.mean() / std * (bars_per_year ** 0.5))


def purge_boundary_bars(
    block_returns: dict[int, pd.Series], train_ids, test_ids, purge_bars: int, embargo_bars: int
) -> dict[int, pd.Series]:
    """Copy of the TRAIN blocks' returns with boundary rows dropped where a train
    block is adjacent to a test block (ids consecutive => adjacency = id±1):
      - train id precedes a test id (id+1 in test) -> drop TAIL purge_bars (label leak)
      - train id follows a test id  (id-1 in test) -> drop HEAD embargo_bars (serial corr)
      - sandwiched -> both. Non-adjacent train blocks untouched."""
    test_set = set(test_ids)
    out: dict[int, pd.Series] = {}
    for t in train_ids:
        s = block_returns.get(t)
        if s is None:
            continue
        head = embargo_bars if (t - 1) in test_set else 0
        tail = purge_bars if (t + 1) in test_set else 0
        out[t] = s.iloc[head: len(s) - tail] if tail else s.iloc[head:]
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_cpcv.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/lib/cpcv.py research/tests/test_cpcv.py
git commit -m "feat(cpcv): pooled Sharpe + directional purge/embargo"
```

---

### Task 3: `cpcv_distribution` (per-path re-selection)

**Files:**
- Modify: `research/lib/cpcv.py`
- Test: `research/tests/test_cpcv.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_cpcv.py` (extend import with `cpcv_distribution`):

```python
from lib.cpcv import cpcv_distribution  # noqa: E402


class TestCpcvDistribution:
    def _matrix(self, per_combo_block_mean):
        # per_combo_block_mean: {combo_id: {block_id: mean_return}}
        # build 20-bar constant-ish returns with tiny noise so std>0
        rng = np.random.default_rng(0)
        out = {}
        for cid, blocks in per_combo_block_mean.items():
            out[cid] = {}
            for bid, mu in blocks.items():
                out[cid][bid] = pd.Series(mu + rng.normal(0, 1e-4, 20),
                                          index=pd.RangeIndex(bid * 20, bid * 20 + 20))
        return out

    def test_best_combo_selected_per_split_and_summary(self):
        # combo 0 strong on blocks 0,1; combo 1 strong on blocks 2,3
        m = self._matrix({0: {0: 0.02, 1: 0.02, 2: -0.01, 3: -0.01},
                          1: {0: -0.01, 1: -0.01, 2: 0.02, 3: 0.02}})
        splits = combinatorial_splits(4, 1)  # 4 paths
        d = cpcv_distribution(m, splits, purge_bars=0, embargo_bars=0, bars_per_year=8760)
        assert d["n_paths"] == 4
        assert "cpcv_mean_sharpe" in d and "cpcv_p05_sharpe" in d
        assert 0.0 <= d["pct_paths_positive"] <= 1.0

    def test_flat_test_scores_zero_not_dropped(self):
        # combo 0 best on train everywhere but FLAT (zero) on the test block -> 0.0, kept
        m = self._matrix({0: {0: 0.02, 1: 0.02, 2: 0.02, 3: 0.02}})
        # overwrite block 3 with exact zeros (flat -> nan sharpe -> 0.0)
        m[0][3] = pd.Series([0.0] * 20, index=pd.RangeIndex(60, 80))
        splits = [((0, 1, 2), (3,))]
        d = cpcv_distribution(m, splits, purge_bars=0, embargo_bars=0, bars_per_year=8760)
        assert d["n_paths"] == 1
        assert d["path_sharpes"] == [0.0]

    def test_tie_break_prefers_lower_combo_id(self):
        # combos 0 and 1 identical on train -> tie -> combo 0 wins; make combo 1 better on test
        m = self._matrix({0: {0: 0.02, 1: 0.02}, 1: {0: 0.02, 1: 0.02}})
        # identical train means; give combo 1 a distinct (better) test block 1
        m[1][1] = pd.Series(0.05 + np.random.default_rng(1).normal(0, 1e-4, 20),
                            index=pd.RangeIndex(20, 40))
        m[0][1] = pd.Series(0.02 + np.random.default_rng(2).normal(0, 1e-4, 20),
                            index=pd.RangeIndex(20, 40))
        splits = [((0,), (1,))]  # train block 0 (tie), test block 1
        d = cpcv_distribution(m, splits, purge_bars=0, embargo_bars=0, bars_per_year=8760)
        # combo 0 chosen on the tie -> test sharpe reflects combo 0's block 1 (~0.02), not combo 1's 0.05
        # (asserting selection determinism: same result across runs)
        d2 = cpcv_distribution(m, splits, 0, 0, 8760)
        assert d["path_sharpes"] == d2["path_sharpes"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_cpcv.py::TestCpcvDistribution -v`
Expected: FAIL — `cpcv_distribution` not defined.

- [ ] **Step 3: Implement `cpcv_distribution`**

Append to `research/lib/cpcv.py`:

```python
def cpcv_distribution(combo_block_returns, splits, purge_bars, embargo_bars, bars_per_year) -> dict:
    """For each split: pick best combo by purge+embargo pooled TRAIN Sharpe
    (tie-break: lower combo_id, deterministic), then score its pooled TEST Sharpe.
    Flat/undefined TEST -> 0.0 (kept, no survivorship). Split skipped only when no
    combo has a defined TRAIN Sharpe. Returns the distribution summary dict."""
    combo_ids = sorted(combo_block_returns.keys())
    path_sharpes: list[float] = []
    for train_ids, test_ids in splits:
        best_cid = None
        best_key = None  # (train_sharpe, -combo_id); max wins => lower id breaks ties
        for cid in combo_ids:
            purged = purge_boundary_bars(
                combo_block_returns[cid], train_ids, test_ids, purge_bars, embargo_bars
            )
            tr = pooled_sharpe(purged, train_ids, bars_per_year)
            if np.isnan(tr):
                continue
            key = (tr, -cid)
            if best_key is None or key > best_key:
                best_key, best_cid = key, cid
        if best_cid is None:
            continue  # empty train pool -> skip path
        te = pooled_sharpe(combo_block_returns[best_cid], test_ids, bars_per_year)
        path_sharpes.append(0.0 if np.isnan(te) else te)

    arr = np.array(path_sharpes, dtype=float)
    return {
        "n_paths": int(arr.size),
        "path_sharpes": [float(x) for x in path_sharpes],
        "cpcv_mean_sharpe": float(np.mean(arr)) if arr.size else None,
        "cpcv_p05_sharpe": float(np.percentile(arr, 5)) if arr.size else None,
        "pct_paths_positive": float(np.mean(arr > 0)) if arr.size else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_cpcv.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/lib/cpcv.py research/tests/test_cpcv.py
git commit -m "feat(cpcv): per-path re-selection distribution (tie-break, no-survivorship)"
```

---

### Task 4: Schema — `CPCVBlock` + gate constants

**Files:**
- Modify: `dashboard/server/schemas.py` (gate constants ~line 45; new block class near `OptimizationBlock`; `StrategyManifest`)
- Test: `dashboard/server/test_schemas.py`

- [ ] **Step 1: Write the failing tests**

In `dashboard/server/test_schemas.py`, add `GATE_MIN_CPCV_MEAN_SHARPE`, `GATE_MIN_CPCV_P05_SHARPE` to the schemas import, add assertions to `test_canonical_gate_constants`:

```python
    assert GATE_MIN_CPCV_MEAN_SHARPE == 1.0
    assert GATE_MIN_CPCV_P05_SHARPE == 0.0
```

And a new test:

```python
def test_cpcv_block_and_manifest_field():
    from schemas import CPCVBlock, StrategyManifest
    blk = CPCVBlock(n_paths=120, cpcv_mean_sharpe=1.3, cpcv_p05_sharpe=0.2,
                    pct_paths_positive=0.9, n_blocks=10, k_test=3)
    assert blk.n_paths == 120 and blk.cpcv_mean_sharpe == 1.3
    assert "cpcv" in StrategyManifest.model_fields  # optional field exists
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard/server && python -m pytest test_schemas.py::test_canonical_gate_constants test_schemas.py::test_cpcv_block_and_manifest_field -v`
Expected: FAIL — import error / `CPCVBlock` missing.

- [ ] **Step 3: Add constants, block, and manifest field**

In `dashboard/server/schemas.py`, after the other gate constants (~line 45):

```python
GATE_MIN_CPCV_MEAN_SHARPE: float = 1.0
GATE_MIN_CPCV_P05_SHARPE: float = 0.0
```

Add the block class (near `OptimizationBlock`):

```python
class CPCVBlock(_Manifest):
    """Combinatorial Purged CV distribution summary (opt-in validation)."""

    n_paths: int
    cpcv_mean_sharpe: Optional[float] = None
    cpcv_p05_sharpe: Optional[float] = None
    pct_paths_positive: Optional[float] = None
    n_blocks: Optional[int] = None
    k_test: Optional[int] = None
```

Add the optional field to `StrategyManifest` (next to its `optimization` field):

```python
    cpcv: Optional[CPCVBlock] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard/server && python -m pytest test_schemas.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/schemas.py dashboard/server/test_schemas.py
git commit -m "feat(schemas): CPCVBlock + StrategyManifest.cpcv + CPCV gate constants"
```

---

### Task 5: emit_manifest CPCV gate

**Files:**
- Modify: `research/emit_manifest.py` (schemas import; `compute_gate`; the manifest-assembly site that loads `optimization.json`)
- Test: `research/tests/test_emit_manifest.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_emit_manifest.py`:

```python
class TestCPCVGate:
    def _cpcv(self, mean, p05):
        from schemas import CPCVBlock
        return CPCVBlock(n_paths=120, cpcv_mean_sharpe=mean, cpcv_p05_sharpe=p05,
                         pct_paths_positive=0.9, n_blocks=10, k_test=3)

    def test_two_non_fatal_thresholds_when_present(self):
        gate = compute_gate(_make_good_backtest(), cpcv=self._cpcv(1.3, 0.2))
        names = {t.name for t in gate.thresholds}
        assert {"cpcv_mean_sharpe", "cpcv_p05_sharpe"} <= names
        for t in gate.thresholds:
            if t.name.startswith("cpcv_"):
                assert t.fatal is False

    def test_pass_fail_at_thresholds(self):
        gate = compute_gate(_make_good_backtest(), cpcv=self._cpcv(0.8, -0.1))
        by = {t.name: t for t in gate.thresholds}
        assert by["cpcv_mean_sharpe"].passed is False   # 0.8 < 1.0
        assert by["cpcv_p05_sharpe"].passed is False     # -0.1 <= 0.0
        assert gate.fatal_fail is False                  # non-fatal

    def test_absent_when_no_cpcv(self):
        gate = compute_gate(_make_good_backtest())
        assert not any(t.name.startswith("cpcv_") for t in gate.thresholds)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_emit_manifest.py::TestCPCVGate -v`
Expected: FAIL — `compute_gate` has no `cpcv` kwarg.

- [ ] **Step 3: Add constant import + gate + threading**

In `research/emit_manifest.py`, add to the `from schemas import (...)` block:

```python
    GATE_MIN_CPCV_MEAN_SHARPE,
    GATE_MIN_CPCV_P05_SHARPE,
```

Change `compute_gate` signature to:

```python
def compute_gate(backtest, optimization=None, cpcv=None) -> GateBlock:
```

Immediately before `overall_pass = all(...)`, append:

```python
    # ── CPCV distribution gates (non-fatal) ─────────────────────────────────────
    if cpcv is not None:
        if cpcv.cpcv_mean_sharpe is not None:
            thresholds.append(GateThreshold(
                name="cpcv_mean_sharpe", threshold=GATE_MIN_CPCV_MEAN_SHARPE,
                actual=cpcv.cpcv_mean_sharpe,
                passed=cpcv.cpcv_mean_sharpe >= GATE_MIN_CPCV_MEAN_SHARPE, fatal=False))
        if cpcv.cpcv_p05_sharpe is not None:
            thresholds.append(GateThreshold(
                name="cpcv_p05_sharpe", threshold=GATE_MIN_CPCV_P05_SHARPE,
                actual=cpcv.cpcv_p05_sharpe,
                passed=cpcv.cpcv_p05_sharpe > GATE_MIN_CPCV_P05_SHARPE, fatal=False))
```

At the manifest-assembly site that loads `optimization.json` (search for `optimization.json` in the file), load `cpcv.json` the same way and pass it to `compute_gate`:

```python
    cpcv_raw = _load_json_block(strategy_dir / "cpcv.json")
    cpcv = None
    if cpcv_raw is not None:
        try:
            cpcv = CPCVBlock.model_validate(cpcv_raw)
        except Exception:  # noqa: BLE001
            cpcv = None
    ...
    gate = compute_gate(backtest, optimization, cpcv)
```

(Add `CPCVBlock` to the `from schemas import (...)` block; set `cpcv=cpcv` on the `StrategyManifest(...)` construction next to `optimization=optimization`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_emit_manifest.py -v`
Expected: all PASS (new CPCV tests + every existing `compute_gate` test — the two new args default to None).

- [ ] **Step 5: Commit**

```bash
git add research/emit_manifest.py research/tests/test_emit_manifest.py
git commit -m "feat(emit_manifest): non-fatal CPCV mean + p05 gates"
```

---

### Task 6: `stage4_cpcv.py` orchestration command

**Files:**
- Create: `research/pipeline/stage4_cpcv.py`

Reuses Stage 4 helpers (`research/pipeline/stage4_optimize.py`): `expand_param_ranges`, `sample_combos`, `apply_overrides_to_spec`, `_compile_signal_code`, `_scaffold_combo_run`, `_invoke_backtest`; `build_run_config` from `stage3_backtest`; `bars_per_year` from `lib.deflated_sharpe`. No new unit test — it runs subprocess backtests (outside the pure-function boundary); the math is covered by Tasks 1-3 and the gate by Task 5. Verified by a live smoke run.

- [ ] **Step 1: Write the module**

Create `research/pipeline/stage4_cpcv.py`:

```python
"""Stage 4b — CPCV validation (opt-in).

python -m research.pipeline.stage4_cpcv --strategy <id> [--blocks 10 --k 3 --max 40]

Precomputes a combo x block per-bar-returns matrix (each block backtested
independently over [block_start - WARMUP_HOURS, block_end] at fixed initial_cash,
warm-up dropped), filters combos to a complete NxK matrix, then writes the CPCV
distribution to research/manifests/<id>/cpcv.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml as _yaml

_THIS = Path(__file__).resolve()
_RESEARCH = _THIS.parent.parent
for _p in (str(_RESEARCH), str(_RESEARCH.parent / "dashboard" / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline.config import _REPO_ROOT, load_config
from pipeline.stage3_backtest import build_run_config
from pipeline.stage4_optimize import (
    apply_overrides_to_spec,
    expand_param_ranges,
    sample_combos,
    _compile_signal_code,
    _invoke_backtest,
    _scaffold_combo_run,
)
from lib.cpcv import cpcv_distribution, combinatorial_splits, make_blocks
from lib.deflated_sharpe import bars_per_year

DEFAULT_BLOCKS, DEFAULT_K, DEFAULT_MAX = 10, 3, 40


def _max_lookback_hours(expanded: dict, horizons_h: list[int]) -> int:
    lbs = [float(x) for x in expanded.get("lookback_days", [])]
    days = max(lbs) if lbs else 0.0
    return max(int(days * 24), max(horizons_h))


def _hold_hours(base_spec: dict, horizons_h: list[int]) -> int:
    for r in base_spec.get("exit_rules", []):
        if r.get("condition") == "time_based":
            return int(r.get("max_hold_hours", max(horizons_h)))
    return max(horizons_h)


def _block_returns(run_dir: Path, block_start: str) -> pd.Series | None:
    """Read artifacts/equity.csv -> per-bar returns; drop the warm-up prefix
    (rows before block_start)."""
    eq_path = run_dir / "artifacts" / "equity.csv"
    if not eq_path.exists():
        return None
    df = pd.read_csv(eq_path, index_col=0, parse_dates=True)
    if "equity" not in df.columns or df.empty:
        return None
    ret = df["equity"].pct_change().dropna()
    return ret[ret.index >= pd.Timestamp(block_start)]


def run(strategy_id: str, n_blocks: int, k_test: int, max_combos: int) -> int:
    cfg = load_config()
    ppy = bars_per_year(cfg.interval)
    strategies_dir = _REPO_ROOT / "research" / "strategies"
    manifests_dir = _REPO_ROOT / "research" / "manifests"
    runs_root = _REPO_ROOT / "runs"

    yaml_path = strategies_dir / f"strategy_{strategy_id}.yaml"
    base_spec = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    expanded = expand_param_ranges(base_spec.get("parameter_search_ranges", {}))
    if not expanded:
        print(f"[cpcv] {strategy_id}: no parameter_search_ranges — nothing to validate")
        return 1
    combos = sample_combos(expanded, max_n=max_combos, seed=42)

    # full history window
    base_cfg = build_run_config(symbol=_symbol_of(base_spec), cfg=cfg)
    hist_start = base_cfg["start_date"]
    # full history incl. OOS (CPCV re-splits everything)
    hist_end = date.today().isoformat() if cfg.oos_start else base_cfg["end_date"]
    blocks = make_blocks(hist_start, hist_end, n_blocks)

    warmup_h = max(_hold_hours(base_spec, cfg.horizons_h),
                   _max_lookback_hours(expanded, cfg.horizons_h))
    warmup_days = warmup_h // 24 + 1  # blocks are day-granular; round warm-up up to whole days
    purge_bars = embargo_bars = max(cfg.horizons_h)

    # ── precompute combo x block matrix ─────────────────────────────────────────
    matrix: dict[int, dict[int, pd.Series]] = {}
    for cid, overrides in enumerate(combos):
        spec_dict = apply_overrides_to_spec(base_spec, overrides)
        code = _compile_signal_code(spec_dict)
        matrix[cid] = {}
        for bid, (b_start, b_end) in enumerate(blocks):
            warm_start = (date.fromisoformat(b_start) - timedelta(days=warmup_days)).isoformat()
            run_cfg = build_run_config(symbol=_symbol_of(base_spec), cfg=cfg)
            run_cfg["start_date"], run_cfg["end_date"] = warm_start, b_end
            run_dir = runs_root / f"{strategy_id}_cpcv_{cid:03d}_b{bid}"
            _scaffold_combo_run(run_dir, run_cfg, code)
            proc = _invoke_backtest(run_dir)
            if proc.returncode == 0:
                r = _block_returns(run_dir, b_start)
                if r is not None and len(r):
                    matrix[cid][bid] = r

    # ── completeness filter: keep only combos with all N blocks (agy P5) ────────
    complete = {cid: bl for cid, bl in matrix.items() if len(bl) == n_blocks}
    if not complete:
        print(f"[cpcv] {strategy_id}: no combo produced all {n_blocks} blocks — abort")
        return 1
    print(f"[cpcv] {strategy_id}: {len(complete)}/{len(combos)} combos complete")

    splits = combinatorial_splits(n_blocks, k_test)
    dist = cpcv_distribution(complete, splits, purge_bars, embargo_bars, ppy)
    dist.pop("path_sharpes", None)  # keep cpcv.json small
    dist.update({"n_blocks": n_blocks, "k_test": k_test})

    out = manifests_dir / strategy_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "cpcv.json").write_text(json.dumps(dist, indent=2), encoding="utf-8")
    print(f"[cpcv] {strategy_id}: mean={dist['cpcv_mean_sharpe']} "
          f"p05={dist['cpcv_p05_sharpe']} paths={dist['n_paths']} -> cpcv.json")
    return 0


def _symbol_of(base_spec: dict) -> str:
    return str(base_spec.get("symbol", "BTC"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--blocks", type=int, default=DEFAULT_BLOCKS)
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--max", type=int, default=DEFAULT_MAX)
    args = ap.parse_args()
    sys.exit(run(args.strategy, args.blocks, args.k, args.max))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Import smoke — module loads and CLI parses**

Run: `cd research && python -m research.pipeline.stage4_cpcv --help`
Expected: argparse usage prints, exit 0 (no import errors).

- [ ] **Step 3: Live smoke on one strategy (small grid for speed)**

Run: `cd research && python -m research.pipeline.stage4_cpcv --strategy sol_s2_trend_with_gate_regime --blocks 6 --k 2 --max 8`
Expected: prints `N/8 combos complete` then `mean=... p05=... paths=15 -> cpcv.json`; `research/manifests/sol_s2_trend_with_gate_regime/cpcv.json` exists and validates as `CPCVBlock`.

- [ ] **Step 4: Confirm the gate wires through emit_manifest**

Run:
```bash
cd research && python -c "import json,sys; sys.path.insert(0,'../dashboard/server'); from schemas import CPCVBlock; CPCVBlock.model_validate(json.load(open('manifests/sol_s2_trend_with_gate_regime/cpcv.json'))); print('cpcv.json valid')"
```
Expected: `cpcv.json valid`.

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage4_cpcv.py
git commit -m "feat(stage4_cpcv): opt-in CPCV validation command"
```

---

## Verification checklist (after all tasks)

- [ ] `cd research && python -m pytest tests/test_cpcv.py tests/test_emit_manifest.py -v` — green.
- [ ] `cd dashboard/server && python -m pytest test_schemas.py -v` — green.
- [ ] Purge direction: train-before-test drops TAIL, train-after-test drops HEAD (Task 2 tests).
- [ ] Flat test pool scores 0.0, not dropped; tie-break picks lower combo_id (Task 3 tests).
- [ ] Warm-up covers `max(hold, lookback×24)`; each block backtest is independent at fixed initial_cash.
- [ ] Completeness filter yields a perfect NxK matrix before distribution.
- [ ] `compute_gate(backtest)` / `(backtest, optimization)` still work (both new args default None).
- [ ] CPCV gates are non-fatal; absent when `cpcv.json` missing.
