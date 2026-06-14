# 15m/30m Intraday Timeframe Pipeline — Phase 2 (Factor Library + Discovery) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a small library of intraday OHLCV-derived factors and run the full discovery pipeline at 15m/30m for ETH + BTC, so the deterministic selector can surface real intraday alpha.

**Architecture:** Each factor is a causal `_name(candles) -> pd.Series` function registered in `_INDICATOR_DISPATCH` (computation) and `_INDICATOR_CATEGORY` (evidence category). The new factors are added unconditionally to those tables (harmless — only computed if in the active pool). Pool membership and the short forward-return horizons are switched on **only for sub-hour runs**, inside `_apply_interval_override` (the existing "sub-hour profile" applier from Phase 1). This keeps 1H discovery byte-for-byte unchanged. Factor windows are defined in **bars** (intraday factors are bar-native); the Phase-1 forward-return scaling makes prediction horizons correct at any candle size.

**Tech Stack:** Python 3, pandas, the `ta` library (already a dependency), pytest. Research code under `research/` (pytest from `research/`).

**Scope:** Builds on Phase 1 (must be merged first — provides `bars_per_hour`, interval-scaled forward returns/IC windows, `RESEARCH_INTERVAL` override + feature-store namespacing, sub-hour funding). Tasks 1-4 are TDD code. Task 5 is the operational discovery run (network-bound, not CI) plus fixing any stage 2-5 sub-hour wiring that surfaces. See spec: `docs/superpowers/specs/2026-06-14-intraday-15m-30m-timeframe-pipeline-design.md` §6.

**Factor set (6, OHLCV-only, causal, single-series IC-screenable):**

| name | category | definition (bars) | rationale |
|------|----------|-------------------|-----------|
| `mom_4` | momentum | `close.pct_change(4)` | very-short momentum/reversal (IC sign decides which) |
| `mom_8` | momentum | `close.pct_change(8)` | short momentum |
| `mom_16` | momentum | `close.pct_change(16)` | medium-short momentum |
| `rvol_ratio_8_32` | volatility | `std(ret,8) / std(ret,32)` | realised-vol breakout (>1 = expanding) |
| `range_expansion_16` | volatility | `(high-low) / mean(high-low,16)` | bar-range expansion |
| `volume_zscore_8` | volume | `(v - mean(v,8)) / std(v,8)` | short volume surge |

> **Deferred:** hour-of-day seasonality. It does not fit single-series Spearman-IC screening (needs grouped/conditional analysis). Revisit separately if the clean factors above underperform.

---

## File Structure

**Modified files**
- `research/lib/indicators.py` — 6 new factor functions + `_INDICATOR_DISPATCH` entries.
- `research/pipeline/stage0a_features.py` — 6 new `_INDICATOR_CATEGORY` entries.
- `research/pipeline/config.py` — `_INTRADAY_FACTORS` + `_INTRADAY_HORIZONS_H` constants; extend `_apply_interval_override` to apply the sub-hour profile (pool + horizons).

**New files**
- `research/tests/test_intraday_factors.py` — factor correctness + causality.
- `research/tests/test_intraday_profile.py` — sub-hour profile (pool/horizons) + name/dispatch consistency.

**Design note — zero 1H regression:** factor functions, dispatch, and category entries are always present but inert unless the factor is in the active pool. Only `_apply_interval_override` (which fires only when `RESEARCH_INTERVAL` is a sub-hour value) adds them to the pool and swaps horizons. With `RESEARCH_INTERVAL` unset or `1H`, nothing changes.

---

## Task 1: Intraday factor functions + dispatch

**Files:**
- Modify: `research/lib/indicators.py` (add functions before `_INDICATOR_DISPATCH` at line 234; add dispatch entries inside it)
- Test: `research/tests/test_intraday_factors.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_intraday_factors.py
"""Intraday OHLCV factor correctness + causality — run from research/ (pytest tests/)."""
import numpy as np
import pandas as pd
import pytest

from lib.indicators import (
    _mom_4,
    _mom_8,
    _mom_16,
    _range_expansion_16,
    _rvol_ratio_8_32,
    _volume_zscore_8,
)


def _candles(close, high=None, low=None, volume=None):
    n = len(close)
    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    close = pd.Series([float(x) for x in close], index=idx)
    return pd.DataFrame(
        {
            "open": close,
            "high": pd.Series(high, index=idx, dtype=float) if high is not None else close,
            "low": pd.Series(low, index=idx, dtype=float) if low is not None else close,
            "close": close,
            "volume": pd.Series(volume, index=idx, dtype=float)
            if volume is not None
            else pd.Series(1.0, index=idx),
        }
    )


def test_mom_8_is_8_bar_return():
    c = _candles(close=list(range(1, 30)))  # 1..29
    out = _mom_8(c)
    assert out.iloc[8] == pytest.approx(9.0 / 1.0 - 1)  # close[8]=9, close[0]=1


def test_mom_4_and_16_shapes_and_values():
    c = _candles(close=[100.0 * (1.01 ** i) for i in range(40)])
    assert _mom_4(c).iloc[4] == pytest.approx(1.01 ** 4 - 1)
    assert _mom_16(c).iloc[16] == pytest.approx(1.01 ** 16 - 1)


def test_range_expansion_constant_range_is_one():
    n = 40
    c = _candles(close=[9.5] * n, high=[10.0] * n, low=[9.0] * n)
    out = _range_expansion_16(c)
    assert out.iloc[-1] == pytest.approx(1.0)


def test_volume_zscore_8_known_value():
    c = _candles(close=[1.0] * 9, volume=[1, 1, 1, 1, 1, 1, 1, 1, 2])
    out = _volume_zscore_8(c)
    # window idx1..8 == [1,1,1,1,1,1,1,2]: mean 1.125, std(ddof=1) 0.353553
    assert out.iloc[8] == pytest.approx((2 - 1.125) / 0.3535534, rel=1e-4)


def test_rvol_ratio_constant_vol_is_about_one():
    rets = [0.01 if i % 2 == 0 else -0.01 for i in range(80)]
    close = [100.0]
    for r in rets:
        close.append(close[-1] * (1 + r))
    out = _rvol_ratio_8_32(_candles(close=close))
    assert out.iloc[-1] == pytest.approx(1.0, abs=0.15)


def test_mom_8_is_causal():
    base = _candles(close=[100.0 + i for i in range(40)])
    out_before = _mom_8(base).iloc[:30].copy()
    perturbed = base.copy()
    perturbed.iloc[35, perturbed.columns.get_loc("close")] = 9999.0  # change the future
    out_after = _mom_8(perturbed).iloc[:30]
    pd.testing.assert_series_equal(out_before, out_after)
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_intraday_factors.py -v`
Expected: FAIL with `ImportError: cannot import name '_mom_4'`.

- [ ] **Step 3: Write minimal implementation**

Add these functions to `research/lib/indicators.py` immediately before the `# ─── Dispatch table ───` comment (line 232):

```python
# ─── Intraday OHLCV factors (bar-native windows) ──────────────────────────────

def _mom_4(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].pct_change(4)


def _mom_8(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].pct_change(8)


def _mom_16(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].pct_change(16)


def _rvol_ratio_8_32(candles: pd.DataFrame) -> pd.Series:
    """Realised-vol breakout: 8-bar return stdev over 32-bar return stdev."""
    ret = candles["close"].pct_change()
    short = ret.rolling(8).std()
    long = ret.rolling(32).std()
    return short / long.replace(0, np.nan)


def _range_expansion_16(candles: pd.DataFrame) -> pd.Series:
    """Current bar range relative to its 16-bar average."""
    rng = candles["high"] - candles["low"]
    return rng / rng.rolling(16).mean().replace(0, np.nan)


def _volume_zscore_8(candles: pd.DataFrame) -> pd.Series:
    v = candles["volume"]
    return (v - v.rolling(8).mean()) / v.rolling(8).std()
```

Then add their entries to `_INDICATOR_DISPATCH` (after the `"volume_zscore_20": _volume_zscore_20,` line):

```python
    # intraday OHLCV
    "mom_4": _mom_4,
    "mom_8": _mom_8,
    "mom_16": _mom_16,
    "rvol_ratio_8_32": _rvol_ratio_8_32,
    "range_expansion_16": _range_expansion_16,
    "volume_zscore_8": _volume_zscore_8,
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_intraday_factors.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/indicators.py research/tests/test_intraday_factors.py
git commit -m "feat(research): add intraday OHLCV factors (mom/rvol/range/vol-z)"
```

---

## Task 2: Evidence category mappings

**Files:**
- Modify: `research/pipeline/stage0a_features.py` (`_INDICATOR_CATEGORY` dict, after line 97 `"volume_zscore_20": "volume",`)
- Test: `research/tests/test_intraday_profile.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_intraday_profile.py
"""Sub-hour profile + intraday factor wiring — run from research/ (pytest tests/)."""
import pytest

from pipeline.stage0a_features import _INDICATOR_CATEGORY


def test_intraday_factors_have_categories():
    assert _INDICATOR_CATEGORY["mom_4"] == "momentum"
    assert _INDICATOR_CATEGORY["mom_8"] == "momentum"
    assert _INDICATOR_CATEGORY["mom_16"] == "momentum"
    assert _INDICATOR_CATEGORY["rvol_ratio_8_32"] == "volatility"
    assert _INDICATOR_CATEGORY["range_expansion_16"] == "volatility"
    assert _INDICATOR_CATEGORY["volume_zscore_8"] == "volume"
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_intraday_profile.py::test_intraday_factors_have_categories -v`
Expected: FAIL with `KeyError: 'mom_4'`.

- [ ] **Step 3: Write minimal implementation**

Add to `_INDICATOR_CATEGORY` in `research/pipeline/stage0a_features.py`, right after the `"volume_zscore_20": "volume",` line (97):

```python
    # intraday OHLCV
    "mom_4": "momentum",
    "mom_8": "momentum",
    "mom_16": "momentum",
    "rvol_ratio_8_32": "volatility",
    "range_expansion_16": "volatility",
    "volume_zscore_8": "volume",
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_intraday_profile.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_intraday_profile.py
git commit -m "feat(research): categorize intraday factors for evidence"
```

---

## Task 3: Sub-hour profile in `_apply_interval_override`

**Files:**
- Modify: `research/pipeline/config.py` (add `_INTRADAY_FACTORS` + `_INTRADAY_HORIZONS_H`; extend `_apply_interval_override`)
- Test: `research/tests/test_intraday_profile.py` (append) + `research/tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_intraday_profile.py`:

```python
from lib.indicators import _INDICATOR_DISPATCH
from pipeline.config import _INTRADAY_FACTORS


def test_intraday_factor_names_match_dispatch():
    # Every name the sub-hour profile injects must be computable.
    for name in _INTRADAY_FACTORS:
        assert name in _INDICATOR_DISPATCH, f"{name} missing from _INDICATOR_DISPATCH"
```

Append to `research/tests/test_config.py`:

```python
def test_subhour_profile_adds_intraday_factors_and_horizons(monkeypatch):
    from pipeline.config import _INTRADAY_FACTORS, _INTRADAY_HORIZONS_H

    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")
    cfg = load_config(_REAL_CONFIG)
    assert cfg.horizons_h == _INTRADAY_HORIZONS_H
    for name in _INTRADAY_FACTORS:
        assert name in cfg.indicator_pool


def test_1H_profile_leaves_pool_and_horizons_untouched(monkeypatch):
    from pipeline.config import _INTRADAY_FACTORS

    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    cfg = load_config(_REAL_CONFIG)
    # 1H keeps the YAML horizons and base pool — no intraday factors.
    assert "mom_8" not in cfg.indicator_pool
    assert all(f not in cfg.indicator_pool for f in _INTRADAY_FACTORS)
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`):
`python -m pytest tests/test_intraday_profile.py::test_intraday_factor_names_match_dispatch tests/test_config.py -k "subhour or 1H_profile" -v`
Expected: FAIL with `ImportError: cannot import name '_INTRADAY_FACTORS'`.

- [ ] **Step 3: Write minimal implementation**

Add module-level constants near the top of `research/pipeline/config.py` (after the imports, before the dataclasses):

```python
# Intraday sub-hour profile: factors appended to the pool and the short
# forward-return horizons used when RESEARCH_INTERVAL is a sub-hour value.
# Names must exist in lib.indicators._INDICATOR_DISPATCH (asserted in tests).
_INTRADAY_FACTORS: tuple[str, ...] = (
    "mom_4", "mom_8", "mom_16",
    "rvol_ratio_8_32", "range_expansion_16", "volume_zscore_8",
)
_INTRADAY_HORIZONS_H: tuple[int, ...] = (1, 2, 4, 8, 24)
```

Replace the Phase-1 `_apply_interval_override` with the extended version:

```python
def _apply_interval_override(cfg: ResearchConfig) -> ResearchConfig:
    """If RESEARCH_INTERVAL is set, return a copy reflecting that candle interval.

    For "1H" only the interval label changes (zero regression). For a sub-hour
    interval the full intraday profile applies: feature store namespaced by
    interval, short intraday forward-return horizons, and the intraday OHLCV
    factors appended to the pool. Unset -> unchanged. Unknown -> ValueError.
    """
    iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
    if not iv:
        return cfg
    from lib.timeframe import SUPPORTED_INTERVALS

    if iv not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"RESEARCH_INTERVAL={iv!r} is not supported; "
            f"valid: {sorted(SUPPORTED_INTERVALS)}"
        )
    changes: dict = {"interval": iv}
    if iv != "1H":
        changes["feature_store_path"] = f"{cfg.feature_store_path.rstrip('/')}/{iv}"
        changes["horizons_h"] = _INTRADAY_HORIZONS_H
        changes["indicator_pool"] = tuple(cfg.indicator_pool) + tuple(
            f for f in _INTRADAY_FACTORS if f not in cfg.indicator_pool
        )
    return dataclasses.replace(cfg, **changes)
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`):
`python -m pytest tests/test_intraday_profile.py tests/test_config.py -v`
Expected: PASS (all green, including the Phase-1 interval-override tests).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/config.py research/tests/test_intraday_profile.py research/tests/test_config.py
git commit -m "feat(research): sub-hour profile injects intraday factors + short horizons"
```

---

## Task 4: Full-suite regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the research suite**

Run (from `research/`): `python -m pytest tests/ -q`
Expected: PASS. (Pre-existing env failures unrelated to this work — `test_oauth_token_cache`, `test_swarm_presets_packaging`, `tests/factors/test_registry` — may appear; confirm they also fail on the merge base before treating as regressions.)

- [ ] **Step 2: Commit (only if any test-collateral fixups were needed)**

```bash
git add -A
git commit -m "test: green research suite after intraday factor library"
```

---

## Task 5: Discovery runs at 15m + 30m for ETH + BTC (operational)

**Files:** none in the TDD sense — this runs the live pipeline, fixes any sub-hour wiring that surfaces in stages 2-5, and records findings. Not CI.

**Context:** Phase 1 validated stage0a→1 at 15m. Stages 2 (strategy scaffold), 2b (signal compile), 2.5 (regime), 3 (backtest), 3-diag, 4 (sweep), 5 (select) were not exercised at sub-hour. Expect to find and fix a small number of hourly assumptions here, the same way Phase 1 surfaced the stage4 `archetype_misfit` fatal-abort (commit d21d460). For each gap: write a failing test reproducing it, fix, re-run.

- [ ] **Step 1: Run the full pipeline for ETH at 15m**

Run (PowerShell, from repo root):

```powershell
$env:RESEARCH_INTERVAL = "15m"
$env:RESEARCH_ONLY_SYMBOL = "eth"
cd research
python -m pipeline.stage0a_features
python -m pipeline.stage0_discovery
python -m pipeline.stage1_factors
python -m pipeline.stage2_strategies
python -m pipeline.stage2b_compile_signal
python -m pipeline.stage2_5_regime
python -m pipeline.stage3_backtest
python -m pipeline.stage3_diagnose
python -m pipeline.stage4_optimize
python -m pipeline.stage5_select
```

- [ ] **Step 2: Fix any sub-hour wiring gap that surfaces**

For each failure: reproduce with a targeted test in the relevant `research/tests/test_stage*.py`, fix the hourly assumption, re-run that stage. Commit each fix separately (`fix(stageN): <what> at sub-hour`). Do not paper over with `try/except` — fix the root cause (grep the failing stage for `* 24`, `/ 24`, `720`, `pct_change(`, hardcoded `"1H"`).

- [ ] **Step 3: Repeat for ETH@30m, BTC@15m, BTC@30m**

```powershell
# Re-run Step 1 with these env combos (one full pass each):
#   RESEARCH_INTERVAL=30m RESEARCH_ONLY_SYMBOL=eth
#   RESEARCH_INTERVAL=15m RESEARCH_ONLY_SYMBOL=btc
#   RESEARCH_INTERVAL=30m RESEARCH_ONLY_SYMBOL=btc
```

- [ ] **Step 4: Record findings**

Confirm artifacts under `research/manifests/15m/` and `research/manifests/30m/` (per-interval namespacing). For each (symbol, interval), note in a short report (e.g. `research/INTRADAY_pipeline_report.md`):
- top evidence factors + their IC (which intraday factors surfaced signal vs which were noise)
- any strategy that passed stage 5 (`selected=True`) with its train/OOS sharpe + DD
- whether intraday alpha exists at all, or this is a clean negative result (a negative result is a valid finding — record it and save a memory).

- [ ] **Step 5: Clean up env**

```powershell
Remove-Item Env:RESEARCH_INTERVAL
Remove-Item Env:RESEARCH_ONLY_SYMBOL
```

---

## Self-Review Notes

- **Spec coverage (§6):** intraday factor families → Task 1 (momentum/vol/volume; hour-of-day explicitly deferred with reason); short horizons → Task 3 (`_INTRADAY_HORIZONS_H`); run stage0a→5 on ETH+BTC at 15m/30m → Task 5; optional dashboard interval dimension → out of scope for this plan (revisit after Task 5 shows whether any intraday strategy is worth surfacing).
- **Type consistency:** factor names are identical across `_INDICATOR_DISPATCH` (Task 1), `_INDICATOR_CATEGORY` (Task 2), and `_INTRADAY_FACTORS` (Task 3); Task 3's `test_intraday_factor_names_match_dispatch` enforces dispatch↔profile agreement.
- **Zero 1H regression:** verified by `test_1H_profile_leaves_pool_and_horizons_untouched` (Task 3) — intraday factors never enter the 1H pool and 1H horizons stay as the YAML defines.
- **Known unknown:** Task 5 is exploratory. Stage 2-5 sub-hour wiring gaps are expected (not all hourly assumptions were on the stage0a→1 path Phase 1 exercised); each is fixed TDD-style as it surfaces.
