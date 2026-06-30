# Deflated Sharpe Ratio gate (#2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a non-fatal Deflated Sharpe Ratio gate that haircuts the best train Sharpe by the breadth of the Stage 4 parameter search, so brute-force survivorship bias can no longer pass as edge.

**Architecture:** A pure lib computes DSR (Bailey & López de Prado 2014, normal-returns form) from all valid trial Sharpes. Stage 4 calls it and emits `deflated_sharpe` + `n_trials` into `optimization.json`. `emit_manifest.compute_gate` reads that and adds a non-fatal `deflated_sharpe >= 0.95` threshold.

**Tech Stack:** Python, numpy, scipy.stats, pytest. Pydantic schema (`dashboard/server/schemas.py`).

**Spec:** `docs/superpowers/specs/2026-06-26-deflated-sharpe-gate-design.md`

---

## File Structure

- **Create** `research/lib/deflated_sharpe.py` — `bars_per_year`, `bars_in_window`, `deflated_sharpe` (pure).
- **Create** `research/tests/test_deflated_sharpe.py` — lib unit tests.
- **Modify** `dashboard/server/schemas.py` — `OptimizationBlock` gains `deflated_sharpe`, `n_trials`; new `GATE_MIN_DEFLATED_SHARPE`.
- **Modify** `dashboard/server/test_schemas.py` — assert the new constant + fields.
- **Modify** `research/pipeline/stage4_optimize.py` — `build_optimization_block` gains the two fields; `_optimize_strategy` computes DSR and passes them.
- **Modify** `research/tests/test_stage4_optimize.py` — `build_optimization_block` carries the fields.
- **Modify** `research/emit_manifest.py` — `compute_gate(backtest, optimization=None)` + DSR threshold; thread `optimization` at the call site.
- **Modify** `research/tests/test_emit_manifest.py` — DSR-threshold gate tests.

**⚠ Two pytest scopes — never mix in one call (established convention):**
- Research: `cd research && python -m pytest tests/<file> -v`
- Dashboard: `cd dashboard/server && python -m pytest test_schemas.py -v`

Task order matters: Task 1 (lib) → Task 2 (schema) → Task 3 (stage4 uses both) → Task 4 (gate uses schema). `_optimize_strategy` wiring (Task 3) runs real backtests via subprocess and is outside the pure-function test boundary; it is verified by the full suite plus read-through, with the math covered by Task 1 and the emit by `build_optimization_block` tests.

---

### Task 1: DSR pure lib

**Files:**
- Create: `research/lib/deflated_sharpe.py`
- Test: `research/tests/test_deflated_sharpe.py`

- [ ] **Step 1: Write the failing tests**

Create `research/tests/test_deflated_sharpe.py`:

```python
"""Tests for the Deflated Sharpe Ratio lib (pure, no IO).

Run from repo root:  cd research && python -m pytest tests/test_deflated_sharpe.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.deflated_sharpe import bars_in_window, bars_per_year, deflated_sharpe  # noqa: E402


class TestBarsPerYear:
    def test_known_intervals(self):
        assert bars_per_year("1H") == 8760
        assert bars_per_year("30m") == 17520
        assert bars_per_year("15m") == 35040

    def test_unknown_defaults_to_hourly(self):
        assert bars_per_year("banana") == 8760


class TestBarsInWindow:
    def test_one_year_hourly(self):
        assert bars_in_window("2021-01-01", "2022-01-01", "1H") == 8760

    def test_handles_iso_datetime_prefix(self):
        assert bars_in_window("2021-01-01T00:00:00", "2022-01-01T00:00:00", "1H") == 8760

    def test_reversed_dates_clamp_to_zero(self):
        assert bars_in_window("2022-01-01", "2021-01-01", "1H") == 0


class TestDeflatedSharpe:
    def test_single_trial_returns_one(self):
        assert deflated_sharpe(0.1, [0.1], 1000) == 1.0

    def test_zero_variance_returns_one(self):
        assert deflated_sharpe(0.1, [0.05, 0.05, 0.05], 1000) == 1.0

    def test_short_sample_returns_one(self):
        assert deflated_sharpe(0.1, [0.01, 0.05, 0.2], 1) == 1.0

    def test_best_far_above_tight_cluster_is_high(self):
        dsr = deflated_sharpe(0.02, [0.001, 0.002, 0.0015, 0.0012], 8760)
        assert dsr > 0.95

    def test_best_at_top_of_wide_spread_is_haircut(self):
        trials = [-0.02, 0.02, -0.01, 0.015, 0.01, -0.015]
        dsr = deflated_sharpe(0.02, trials, 8760)
        assert dsr < 0.7

    def test_dsr_decreases_as_trials_grow(self):
        base = [0.005, -0.005, 0.003, -0.003]
        few = deflated_sharpe(0.02, base, 8760)
        many = deflated_sharpe(0.02, base * 25, 8760)  # 100 trials, same dispersion
        assert many < few

    def test_nonfinite_trials_dropped(self):
        clean = deflated_sharpe(0.02, [0.001, 0.002, 0.0015, 0.0012], 8760)
        dirty = deflated_sharpe(
            0.02, [0.001, 0.002, 0.0015, 0.0012, float("nan"), float("inf")], 8760
        )
        assert dirty == clean
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_deflated_sharpe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.deflated_sharpe'`.

- [ ] **Step 3: Create the lib**

Create `research/lib/deflated_sharpe.py`:

```python
"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014), normal-returns form.

Pure functions; no IO. Used by Stage 4 to haircut the best train Sharpe by the
breadth of the parameter search (multiple-testing / survivorship correction).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import scipy.stats as st

# Bars per year per pipeline interval — used to de-annualise Sharpe to per-bar.
_BARS_PER_YEAR = {"15m": 35040, "30m": 17520, "1H": 8760, "4H": 2190, "1D": 365}


def bars_per_year(interval: str) -> int:
    """Bars per year for a pipeline interval; defaults to 1H (8760) if unknown."""
    return _BARS_PER_YEAR.get(interval, 8760)


def bars_in_window(start_date: str, end_date: str, interval: str) -> int:
    """Approx bar count between two ISO dates at *interval* (clamped at 0)."""
    d0 = date.fromisoformat(start_date[:10])
    d1 = date.fromisoformat(end_date[:10])
    days = max((d1 - d0).days, 0)
    return int(days * bars_per_year(interval) / 365)


def deflated_sharpe(
    best_sr_per_bar: float,
    trial_srs_per_bar: "np.ndarray | list[float]",
    T: int,
    expected_sr: float = 0.0,
) -> float:
    """Probability the best trial's true per-bar Sharpe exceeds the expected
    maximum Sharpe of N trials under the null (skew=0, kurt=3).

    All Sharpes MUST be per-bar (de-annualised). ``T`` = train-window bar count.
    Returns 1.0 when deflation is undefined (N<2, T<2, zero trial variance) so
    the gate never spuriously fails. Result is a probability in [0, 1].
    """
    srs = np.asarray(list(trial_srs_per_bar), dtype=float)
    srs = srs[np.isfinite(srs)]
    n = srs.size
    if n < 2 or T < 2:
        return 1.0

    var_srs = float(np.var(srs, ddof=1))
    if var_srs <= 0.0:
        return 1.0

    gamma = 0.5772156649  # Euler-Mascheroni
    max_z = (1 - gamma) * st.norm.ppf(1 - 1.0 / n) + gamma * st.norm.ppf(
        1 - 1.0 / (n * np.e)
    )
    expected_max_sr = expected_sr + np.sqrt(var_srs) * max_z

    sr_std = np.sqrt((1.0 + 0.5 * best_sr_per_bar**2) / (T - 1))
    return float(st.norm.cdf((best_sr_per_bar - expected_max_sr) / sr_std))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_deflated_sharpe.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add research/lib/deflated_sharpe.py research/tests/test_deflated_sharpe.py
git commit -m "feat(research): deflated Sharpe ratio lib (Bailey-Lopez de Prado)"
```

---

### Task 2: Schema fields + gate constant

**Files:**
- Modify: `dashboard/server/schemas.py` (`OptimizationBlock` ~line 382-389; gate constants ~line 41-45)
- Test: `dashboard/server/test_schemas.py` (`test_canonical_gate_constants` ~line 373)

- [ ] **Step 1: Write the failing tests**

In `dashboard/server/test_schemas.py`, add `GATE_MIN_DEFLATED_SHARPE` to the import from `schemas`, then add an assertion line inside `test_canonical_gate_constants`:

```python
    assert GATE_MIN_DEFLATED_SHARPE == 0.95
```

And add a new test:

```python
def test_optimization_block_dsr_fields_default_none():
    from schemas import OptimizationBlock
    blk = OptimizationBlock()
    assert blk.deflated_sharpe is None
    assert blk.n_trials is None
    blk2 = OptimizationBlock(deflated_sharpe=0.97, n_trials=50)
    assert blk2.deflated_sharpe == 0.97
    assert blk2.n_trials == 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard/server && python -m pytest test_schemas.py::test_canonical_gate_constants test_schemas.py::test_optimization_block_dsr_fields_default_none -v`
Expected: FAIL — `ImportError: cannot import name 'GATE_MIN_DEFLATED_SHARPE'` / `AttributeError` on the fields.

- [ ] **Step 3: Add the constant and fields**

In `dashboard/server/schemas.py`, add the constant next to the other gate constants (after `GATE_MIN_WALK_FORWARD_SHARPE`, ~line 45):

```python
GATE_MIN_DEFLATED_SHARPE: float = 0.95  # multiple-testing haircut (Bailey-LdP DSR)
```

Add the two fields to `OptimizationBlock` (after `improvement_summary`):

```python
    deflated_sharpe: Optional[float] = Field(
        default=None,
        description="Deflated Sharpe Ratio in [0,1]; null for runs without a sweep.",
    )
    n_trials: Optional[int] = Field(
        default=None, description="Number of valid combos evaluated in the sweep."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard/server && python -m pytest test_schemas.py -v`
Expected: all PASS (the new tests + every existing schema test).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/schemas.py dashboard/server/test_schemas.py
git commit -m "feat(schemas): OptimizationBlock.deflated_sharpe + GATE_MIN_DEFLATED_SHARPE"
```

---

### Task 3: Stage 4 computes and emits DSR

**Files:**
- Modify: `research/pipeline/stage4_optimize.py` (imports; `build_optimization_block` ~line 439-457; `_optimize_strategy` at the `build_optimization_block(...)` call ~line 628)
- Test: `research/tests/test_stage4_optimize.py` (`TestBuildOptimizationBlock`)

- [ ] **Step 1: Write the failing tests**

Add to `class TestBuildOptimizationBlock` in `research/tests/test_stage4_optimize.py`:

```python
    def test_carries_deflated_sharpe_and_n_trials(self):
        best = ComboResult(idx=0, overrides={"tp_pct": 5.0}, run_name="x_sweep_000",
                           metrics={"sharpe": "1.2", "trade_count": "40"})
        block = build_optimization_block(
            ["tp_pct"], best, "ok", deflated_sharpe=0.97, n_trials=50
        )
        assert block["deflated_sharpe"] == 0.97
        assert block["n_trials"] == 50
        from schemas import OptimizationBlock
        OptimizationBlock.model_validate(block)

    def test_dsr_fields_default_none(self):
        block = build_optimization_block(["a"], None, "")
        assert block["deflated_sharpe"] is None
        assert block["n_trials"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestBuildOptimizationBlock -v`
Expected: FAIL — `build_optimization_block()` got an unexpected keyword `deflated_sharpe`.

- [ ] **Step 3: Extend `build_optimization_block`**

In `research/pipeline/stage4_optimize.py`, change the signature and returned dict:

```python
def build_optimization_block(
    swept_params: list[str],
    best: ComboResult | None,
    summary: str,
    deflated_sharpe: float | None = None,
    n_trials: int | None = None,
) -> dict:
    best_params: dict[str, float] = {}
    if best is not None:
        for k, v in best.overrides.items():
            try:
                best_params[k] = float(v)
            except (TypeError, ValueError):
                continue
    return {
        "source_run": best.run_name if best is not None else None,
        "method": OPTIMIZATION_METHOD,
        "swept_params": sorted(swept_params),
        "best_params": best_params,
        "improvement_summary": summary[:2000] if summary else None,
        "deflated_sharpe": deflated_sharpe,
        "n_trials": n_trials,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py::TestBuildOptimizationBlock -v`
Expected: all PASS.

- [ ] **Step 5: Wire DSR computation into `_optimize_strategy`**

In `research/pipeline/stage4_optimize.py`, add the import near the other `lib` imports (next to `from lib.signal_compiler import compile_strategy`):

```python
from lib.deflated_sharpe import bars_in_window, bars_per_year, deflated_sharpe  # noqa: E402
```

In `_optimize_strategy`, locate the block:

```python
    best = ranked[0] if ranked else None
    summary = _summarise(results, ranked)
    print(f"\n{summary}\n")

    block = build_optimization_block(
        swept_params=list(expanded.keys()),
        best=best,
        summary=summary,
    )
```

Replace with:

```python
    best = ranked[0] if ranked else None
    summary = _summarise(results, ranked)
    print(f"\n{summary}\n")

    # ── Deflated Sharpe Ratio (multiple-testing haircut) ──────────────────────
    # All valid combos are the search breadth (NOT the DD-gated survivors); the
    # selected best's train Sharpe is deflated against that distribution. Sharpes
    # are de-annualised to per-bar; T is the train-window bar count.
    ppy = bars_per_year(cfg.interval)
    trial_srs = [c.sharpe / (ppy ** 0.5) for c in results if c.metrics is not None]
    n_trials = len(trial_srs)
    dsr: float | None = None
    if best is not None and trial_srs:
        T = bars_in_window(base_config["start_date"], base_config["end_date"], cfg.interval)
        dsr = deflated_sharpe(best.sharpe / (ppy ** 0.5), trial_srs, T)
        print(f"  [DSR] deflated_sharpe={dsr:.3f}  n_trials={n_trials}  T={T}")

    block = build_optimization_block(
        swept_params=list(expanded.keys()),
        best=best,
        summary=summary,
        deflated_sharpe=dsr,
        n_trials=n_trials,
    )
```

(`base_config["start_date"]/["end_date"]` are the actual sweep window — set earlier in the function, including the train-window override.)

- [ ] **Step 6: Run the full stage4 suite**

Run: `cd research && python -m pytest tests/test_stage4_optimize.py -v`
Expected: all PASS (no regression; `_optimize_strategy` import + wiring resolve).

- [ ] **Step 7: Commit**

```bash
git add research/pipeline/stage4_optimize.py research/tests/test_stage4_optimize.py
git commit -m "feat(stage4): compute and emit deflated Sharpe + n_trials"
```

---

### Task 4: emit_manifest DSR gate

**Files:**
- Modify: `research/emit_manifest.py` (schemas import line ~59-67; `compute_gate` signature ~line 179 and threshold list; call site ~line 688)
- Test: `research/tests/test_emit_manifest.py` (new `TestDeflatedSharpeGate` class)

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_emit_manifest.py` (the file already imports `compute_gate` and `_make_good_backtest`):

```python
class TestDeflatedSharpeGate:
    def _opt(self, dsr):
        from schemas import OptimizationBlock
        return OptimizationBlock(deflated_sharpe=dsr, n_trials=50)

    def test_threshold_present_and_non_fatal_when_passing(self):
        gate = compute_gate(_make_good_backtest(), self._opt(0.96))
        dsr_t = [t for t in gate.thresholds if t.name == "deflated_sharpe"]
        assert len(dsr_t) == 1
        assert dsr_t[0].fatal is False
        assert dsr_t[0].passed is True

    def test_fails_below_threshold_but_not_fatal(self):
        gate = compute_gate(_make_good_backtest(), self._opt(0.80))
        dsr_t = [t for t in gate.thresholds if t.name == "deflated_sharpe"][0]
        assert dsr_t.passed is False
        assert gate.fatal_fail is False  # non-fatal: does not hard-block

    def test_boundary_inclusive(self):
        gate = compute_gate(_make_good_backtest(), self._opt(0.95))
        dsr_t = [t for t in gate.thresholds if t.name == "deflated_sharpe"][0]
        assert dsr_t.passed is True

    def test_absent_when_optimization_none(self):
        gate = compute_gate(_make_good_backtest())  # one-arg, backward compat
        assert not any(t.name == "deflated_sharpe" for t in gate.thresholds)

    def test_absent_when_dsr_none(self):
        gate = compute_gate(_make_good_backtest(), self._opt(None))
        assert not any(t.name == "deflated_sharpe" for t in gate.thresholds)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_emit_manifest.py::TestDeflatedSharpeGate -v`
Expected: FAIL — `compute_gate()` takes 1 positional arg / no `deflated_sharpe` threshold exists.

- [ ] **Step 3: Add the constant import**

In `research/emit_manifest.py`, add `GATE_MIN_DEFLATED_SHARPE` to the existing `from schemas import (...)` block that already pulls the gate constants (alongside `GATE_MAX_DRAWDOWN`, `GATE_MIN_SHARPE`, ...):

```python
    GATE_MIN_DEFLATED_SHARPE,
```

- [ ] **Step 4: Add the optional arg and threshold**

Change the `compute_gate` signature:

```python
def compute_gate(backtest: BacktestBlock, optimization: "OptimizationBlock | None" = None) -> GateBlock:
```

Immediately before `overall_pass = all(t.passed for t in thresholds)`, append the DSR threshold:

```python
    # ── deflated_sharpe (multiple-testing haircut; non-fatal) ───────────────────
    if optimization is not None and optimization.deflated_sharpe is not None:
        dsr_actual = optimization.deflated_sharpe
        thresholds.append(GateThreshold(
            name="deflated_sharpe",
            threshold=GATE_MIN_DEFLATED_SHARPE,
            actual=dsr_actual,
            passed=dsr_actual >= GATE_MIN_DEFLATED_SHARPE,
            fatal=False,
        ))
```

- [ ] **Step 5: Thread `optimization` at the call site**

In `research/emit_manifest.py`, find:

```python
        gate = compute_gate(backtest)
```

Replace with:

```python
        gate = compute_gate(backtest, optimization)
```

(`optimization` is already loaded a few lines earlier as an `OptimizationBlock | None`.)

- [ ] **Step 6: Run the emit_manifest suite**

Run: `cd research && python -m pytest tests/test_emit_manifest.py -v`
Expected: all PASS — new `TestDeflatedSharpeGate` plus every existing `compute_gate(bt)` test (the new arg defaults to `None`).

- [ ] **Step 7: Commit**

```bash
git add research/emit_manifest.py research/tests/test_emit_manifest.py
git commit -m "feat(emit_manifest): non-fatal deflated-Sharpe gate"
```

---

## Verification checklist (after all tasks)

- [ ] `cd research && python -m pytest tests/test_deflated_sharpe.py tests/test_stage4_optimize.py tests/test_emit_manifest.py -v` — all green.
- [ ] `cd dashboard/server && python -m pytest test_schemas.py -v` — all green (separate scope).
- [ ] `compute_gate(bt)` (one-arg) still works everywhere — no existing call site touched except emit_manifest's own.
- [ ] DSR is computed from **all valid combos**, de-annualised to per-bar, with `T` = train-window bars.
- [ ] DSR gate is **non-fatal**; absent when `deflated_sharpe is None`.
- [ ] No change to `backtest/` or per-combo IO.
