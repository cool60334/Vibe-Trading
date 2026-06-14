# 15m/30m Intraday Timeframe Pipeline — Phase 1 (Infra) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the research pipeline mathematically correct and runnable at 15m/30m candle bars, validated by a null-result smoke run, with zero regression to existing 1H strategies.

**Architecture:** A single `bars_per_hour(interval)` helper is the one source of truth for hour→bar conversions. Every hour-anchored quantity (forward-return horizons, 30-day rolling windows, IC rolling step) multiplies by it. Timeframe is selected per-run via a new `RESEARCH_INTERVAL` env var (mirrors the existing `RESEARCH_ONLY_SYMBOL`), which `load_config` honors and which also namespaces the feature store so 15m/30m/1H artifacts never clobber. Funding fee gains interval-awareness so sub-hour bars settle exactly at 8h boundaries; the 1H/daily code path is left untouched to preserve validated strategy numbers.

**Tech Stack:** Python 3, pandas, scipy, pytest. Research code under `research/` (pytest run from `research/`); backtest engine under `agent/` (pytest run from `agent/`). The two test suites run separately.

**Scope:** This plan is **Phase 1 only** (timeframe infra). Phase 2 (the intraday OHLCV factor library + discovery runs on ETH/BTC) is a separate plan, written after Phase 1 is verified — its factor windows and artifact layout depend on Phase 1 being concrete, and its discovery runs are exploratory rather than TDD-able. See spec: `docs/superpowers/specs/2026-06-14-intraday-15m-30m-timeframe-pipeline-design.md`.

---

## File Structure

**New files**
- `research/lib/timeframe.py` — `bars_per_hour(interval)`, `bars_per_day(interval)`, `SUPPORTED_INTERVALS`. One responsibility: timeframe↔bar conversion.
- `research/tests/test_timeframe.py` — unit tests for the helper.
- `research/tests/test_factor_metrics_timeframe.py` — forward-return + evaluate_factor interval scaling.
- `research/tests/test_stage0a_timeframe.py` — build_feature_dict windows, apply_ic_eval_transform, compute_evidence_entries end-to-end IC scaling.
- `agent/tests/test_crypto_funding_subhour.py` — funding settlement cadence on sub-hour bars.

**Modified files**
- `research/lib/factor_metrics.py` — `add_forward_returns` + `evaluate_factor` gain `interval="1H"` param.
- `research/pipeline/stage0a_features.py` — `apply_ic_eval_transform` + `compute_evidence_entries` gain `interval`; `build_feature_dict` scales the stablecoin/OI windows; call site passes `cfg.interval`.
- `research/pipeline/config.py` — `_apply_interval_override` (RESEARCH_INTERVAL → interval + feature_store_path namespace).
- `research/tests/test_config.py` — append interval-override tests.
- `agent/backtest/engines/crypto.py` — read `interval` from config, pass to funding hook.
- `agent/backtest/engines/_market_hooks.py` — `calc_crypto_funding_fee` interval-aware (sub-hour = settlement-only).

**Design note — zero regression:** every new param defaults to `"1H"`, and the funding change only alters sub-hour behaviour. Existing callers and existing 1H/daily backtests are byte-for-byte unchanged.

---

## Task 1: `bars_per_hour` timeframe helper

**Files:**
- Create: `research/lib/timeframe.py`
- Test: `research/tests/test_timeframe.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_timeframe.py
"""Tests for research/lib/timeframe.py — run from research/ (pytest tests/)."""
import pytest

from lib.timeframe import SUPPORTED_INTERVALS, bars_per_day, bars_per_hour


def test_bars_per_hour_known_intervals():
    assert bars_per_hour("15m") == 4
    assert bars_per_hour("30m") == 2
    assert bars_per_hour("1H") == 1


def test_bars_per_day_known_intervals():
    assert bars_per_day("15m") == 96
    assert bars_per_day("30m") == 48
    assert bars_per_day("1H") == 24


def test_supported_intervals_set():
    assert SUPPORTED_INTERVALS == frozenset({"15m", "30m", "1H"})


def test_unsupported_interval_raises():
    with pytest.raises(ValueError, match="unsupported interval"):
        bars_per_hour("4H")
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_timeframe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lib.timeframe'`.

- [ ] **Step 3: Write minimal implementation**

```python
# research/lib/timeframe.py
"""Timeframe ↔ bar conversions for sub-hourly research runs.

Single source of truth for "how many bars in an hour/day" at a given candle
interval. Hour-anchored quantities (forward-return horizons in hours, the
30-day rolling windows on funding/stablecoin, the IC rolling step) multiply by
these to stay correct when the pipeline runs at 15m/30m instead of legacy 1H.
"""
from __future__ import annotations

# OKX bar strings → bars per hour. Keys match calc_bars_per_year's _BARS_PER_DAY
# table and the research_config.yaml `interval` field. All values are integers
# (sub-hour intervals divide an hour evenly), so hour→bar conversions never
# produce fractional bars.
_BARS_PER_HOUR: dict[str, int] = {"15m": 4, "30m": 2, "1H": 1}

SUPPORTED_INTERVALS: frozenset[str] = frozenset(_BARS_PER_HOUR)


def bars_per_hour(interval: str) -> int:
    """Number of candle bars in one hour at `interval`.

    Raises ValueError for any interval outside SUPPORTED_INTERVALS.
    """
    try:
        return _BARS_PER_HOUR[interval]
    except KeyError:
        raise ValueError(
            f"unsupported interval {interval!r}; supported: {sorted(SUPPORTED_INTERVALS)}"
        ) from None


def bars_per_day(interval: str) -> int:
    """Number of candle bars in one 24h day at `interval`."""
    return bars_per_hour(interval) * 24
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_timeframe.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/timeframe.py research/tests/test_timeframe.py
git commit -m "feat(research): add bars_per_hour timeframe helper"
```

---

## Task 2: `add_forward_returns` — scale horizon hours to bars

**Files:**
- Modify: `research/lib/factor_metrics.py:24-29`
- Test: `research/tests/test_factor_metrics_timeframe.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_factor_metrics_timeframe.py
"""Interval-scaling tests for factor_metrics — run from research/ (pytest tests/)."""
import pandas as pd
import pytest

from lib.factor_metrics import add_forward_returns


def test_forward_returns_1H_default_shifts_by_h_bars():
    px = pd.Series(
        [100.0, 101.0, 102.0, 103.0, 104.0],
        index=pd.date_range("2025-01-01", periods=5, freq="1h"),
    )
    df = pd.DataFrame({"close": px})
    out = add_forward_returns(df, "close", [2])  # default interval="1H"
    # ret_2h at t0 == px[2]/px[0]-1 (2 bars == 2 hours at 1H)
    assert out["ret_2h"].iloc[0] == pytest.approx(102.0 / 100.0 - 1)


def test_forward_returns_15m_scales_horizon_hours_to_bars():
    px = pd.Series(
        [float(v) for v in range(100, 120)],
        index=pd.date_range("2025-01-01", periods=20, freq="15min"),
    )
    df = pd.DataFrame({"close": px})
    out = add_forward_returns(df, "close", [2], interval="15m")
    # 2h horizon at 15m == 2*4 == 8 bars forward
    assert out["ret_2h"].iloc[0] == pytest.approx(px.iloc[8] / px.iloc[0] - 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_factor_metrics_timeframe.py -v`
Expected: FAIL — `test_forward_returns_15m_...` fails (current code shifts by 2 bars, not 8); also `TypeError` if `interval` kw not yet accepted.

- [ ] **Step 3: Write minimal implementation**

Replace `add_forward_returns` (currently `research/lib/factor_metrics.py:24-29`):

```python
def add_forward_returns(
    df: pd.DataFrame, price_col: str, horizons_h: list, interval: str = "1H"
) -> pd.DataFrame:
    """Append forward simple returns columns named ret_<h>h for each horizon in hours.

    `horizons_h` are in HOURS. At sub-hour `interval` they are converted to bars
    via bars_per_hour so ret_24h always means 24 hours forward, not 24 bars.
    """
    out = df.copy()
    bph = bars_per_hour(interval)
    for h in horizons_h:
        out[f"ret_{h}h"] = out[price_col].shift(-(h * bph)) / out[price_col] - 1
    return out
```

Add the import near the top of `research/lib/factor_metrics.py` (after the existing imports):

```python
from lib.timeframe import bars_per_day, bars_per_hour
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_factor_metrics_timeframe.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/factor_metrics.py research/tests/test_factor_metrics_timeframe.py
git commit -m "fix(research): scale forward-return horizons hours->bars by interval"
```

---

## Task 3: `evaluate_factor` — scale rolling IC window/step to bars

**Files:**
- Modify: `research/lib/factor_metrics.py:67-102`
- Test: `research/tests/test_factor_metrics_timeframe.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_factor_metrics_timeframe.py`:

```python
import numpy as np

from lib.factor_metrics import evaluate_factor


def test_evaluate_factor_accepts_interval_and_scales_window():
    # 15m bars: a factor equal to the realised forward 4h return is a perfect
    # predictor; IC at the 4h horizon must be ~1 once horizon hours scale to bars.
    idx = pd.date_range("2025-01-01", periods=4000, freq="15min")
    rng = np.random.default_rng(0)
    px = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(idx))), index=idx)
    df = pd.DataFrame({"close": px})
    df = add_forward_returns(df, "close", [4], interval="15m")
    df["oracle"] = df["ret_4h"]  # factor == the thing it predicts
    results = evaluate_factor(df, "oracle", [4], interval="15m")
    r4 = next(r for r in results if r.horizon == "4h")
    assert r4.ic > 0.95
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_factor_metrics_timeframe.py::test_evaluate_factor_accepts_interval_and_scales_window -v`
Expected: FAIL with `TypeError: evaluate_factor() got an unexpected keyword argument 'interval'`.

- [ ] **Step 3: Write minimal implementation**

Change `evaluate_factor`'s signature and the `rolling_ic` call inside it (`research/lib/factor_metrics.py:67-96`):

```python
def evaluate_factor(
    df: pd.DataFrame,
    factor_col: str,
    horizons_h: list,
    rolling_window_days: int = 30,
    min_samples: int = 200,
    interval: str = "1H",
) -> list:
    """Compute IC + IR for one factor across multiple forward-return horizons.

    Expects df to already contain ret_<h>h columns (use add_forward_returns first).
    The rolling-IR window/step are in DAYS internally and convert to bars via
    `interval` so a "30-day" window is 30 days at any candle size.
    """
    bpd = bars_per_day(interval)
    results: list = []
    for h in horizons_h:
        ret_col = f"ret_{h}h"
        if ret_col not in df.columns:
            raise KeyError(f"missing column {ret_col} (call add_forward_returns first)")
        sub = df[[factor_col, ret_col]].dropna()
        n = len(sub)
        if n < min_samples:
            results.append(FactorResult(factor_col, f"{h}h", float("nan"), float("nan"), n))
            continue
        ic, _ = spearmanr(sub[factor_col], sub[ret_col])
        rolling = rolling_ic(
            df,
            factor_col,
            ret_col,
            window_bars=rolling_window_days * bpd,
            step_bars=bpd,
            min_samples=min_samples,
        )
        if rolling.size > 0 and rolling.std(ddof=0) > 0:
            ir = float(rolling.mean() / rolling.std(ddof=0))
        else:
            ir = float("nan")
        results.append(FactorResult(factor_col, f"{h}h", float(ic), ir, n))
    return results
```

(No import change needed — `bars_per_day` was imported in Task 2.)

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_factor_metrics_timeframe.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/factor_metrics.py research/tests/test_factor_metrics_timeframe.py
git commit -m "fix(research): scale evaluate_factor rolling IC window/step by interval"
```

---

## Task 4: Thread `interval` through `apply_ic_eval_transform` + `compute_evidence_entries`

**Files:**
- Modify: `research/pipeline/stage0a_features.py` (`apply_ic_eval_transform` ~147, `compute_evidence_entries` ~256-329)
- Test: `research/tests/test_stage0a_timeframe.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_stage0a_timeframe.py
"""Interval-scaling tests for stage0a_features — run from research/ (pytest tests/)."""
import numpy as np
import pandas as pd
import pytest

from pipeline.stage0a_features import (
    apply_ic_eval_transform,
    compute_evidence_entries,
)


def test_apply_ic_eval_transform_scales_obv_zscore_window():
    # obv uses a 720h rolling z-score. At 15m the window must be 720*4 bars so a
    # value older than the window no longer influences the current z-score.
    idx = pd.date_range("2025-01-01", periods=720 * 4 + 50, freq="15min")
    s = pd.Series(np.arange(len(idx), dtype=float), index=idx, name="obv")
    out_1h, label = apply_ic_eval_transform("obv", s, interval="1H")
    out_15m, _ = apply_ic_eval_transform("obv", s, interval="15m")
    assert label == "zscore_720h"
    # First non-NaN z appears later for the wider 15m window than the 1H window.
    assert out_15m.first_valid_index() > out_1h.first_valid_index()


def test_compute_evidence_entries_15m_oracle_has_high_ic():
    idx = pd.date_range("2025-01-01", periods=4000, freq="15min")
    rng = np.random.default_rng(1)
    px = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(idx))), index=idx)
    candles = pd.DataFrame({"close": px})
    # Build an oracle feature equal to forward 4h return (perfect predictor).
    fwd = px.shift(-(4 * 4)) / px - 1
    entries = compute_evidence_entries(
        candles, {"oracle": fwd}, horizons_h=(4,), interval="15m"
    )
    assert entries[0]["feature_key"] == "oracle"
    assert entries[0]["ic_by_horizon"][4] > 0.95
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_stage0a_timeframe.py -v`
Expected: FAIL — `TypeError` on the unexpected `interval` kwarg.

- [ ] **Step 3: Write minimal implementation**

(a) Add import near the top of `research/pipeline/stage0a_features.py` (with the other `from lib...` imports):

```python
from lib.timeframe import bars_per_hour
```

(b) Change `apply_ic_eval_transform` (signature at ~147; zscore branch at ~157-158):

```python
def apply_ic_eval_transform(
    feat_name: str, series: pd.Series, interval: str = "1H"
) -> tuple[pd.Series, str | None]:
    """Return (series_for_ic, transform_label) for honest screening IC.

    Measurement-layer only — does not mutate the stored feature. Features not
    listed are returned unchanged with a ``None`` label. `interval` scales the
    720h rolling z-score window so it stays 30 days at any candle size.
    """
    if feat_name in _IC_STATIONARY_TRANSFORM:
        kind = _IC_STATIONARY_TRANSFORM[feat_name]
        if kind == "zscore_720h":
            return _rolling_zscore(series, window=720 * bars_per_hour(interval)), kind

    if feat_name in _IC_NATIVE_FREQ:
        rule = _IC_NATIVE_FREQ[feat_name]
        if rule == "on_change":
            changed = series.ne(series.shift(1))
            return series.where(changed), "native_freq"
        native = series.resample(rule).last()
        return native.reindex(series.index), f"native_{rule}"

    return series, None
```

(Resample rules `"1D"`/`"8h"` are timestamp-based and need no scaling.)

(c) Change `compute_evidence_entries` (signature at ~256; the two internal calls at ~283 and ~289/295):

```python
def compute_evidence_entries(
    candles: pd.DataFrame,
    feature_dict: dict[str, pd.Series],
    horizons_h: tuple[int, ...],
    price_col: str = "close",
    interval: str = "1H",
) -> list[dict]:
```

Inside, update the three call sites:

```python
    base_df = add_forward_returns(base_df, "price", list(horizons_h), interval=interval)
```
```python
        eval_series, ic_transform = apply_ic_eval_transform(feat_name, feat_series, interval=interval)
```
```python
        results: list[FactorResult] = evaluate_factor(df, feat_name, list(horizons_h), interval=interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_stage0a_timeframe.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_timeframe.py
git commit -m "fix(research): thread interval through stage0a IC eval transform + evidence"
```

---

## Task 5: `build_feature_dict` — scale stored stablecoin/OI windows

**Files:**
- Modify: `research/pipeline/stage0a_features.py:222-243` (oi_change_24h, stablecoin_supply_z)
- Test: `research/tests/test_stage0a_timeframe.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_stage0a_timeframe.py`:

```python
from pipeline.config import load_config
from pipeline.stage0a_features import build_feature_dict


def _cfg(interval):
    cfg = load_config()  # real config; we only need its shape
    import dataclasses
    return dataclasses.replace(cfg, interval=interval)


def test_oi_change_window_scales_to_24h_in_bars():
    idx = pd.date_range("2025-01-01", periods=300, freq="15min")
    candles = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )
    oi = pd.DataFrame({"open_interest": np.arange(len(idx), dtype=float)}, index=idx)
    feats = build_feature_dict(candles, _cfg("15m"), oi_df=oi)
    # oi_change_24h must be pct_change over 24h == 96 bars at 15m.
    expected = oi["open_interest"].pct_change(periods=24 * 4)
    pd.testing.assert_series_equal(
        feats["oi_change_24h"].reset_index(drop=True),
        expected.reset_index(drop=True),
        check_names=False,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_stage0a_timeframe.py::test_oi_change_window_scales_to_24h_in_bars -v`
Expected: FAIL — current code uses `pct_change(periods=24)` (24 bars == 6h at 15m), not 96.

- [ ] **Step 3: Write minimal implementation**

At the top of `build_feature_dict` (just after `features: dict[str, pd.Series] = {}`), compute the factor once:

```python
    bph = bars_per_hour(config.interval)
```

Change the OI block (`stage0a_features.py:227`):

```python
        oi_change = oi_on_candle.pct_change(periods=24 * bph)
```

Change the stablecoin block (`stage0a_features.py:239-240`):

```python
        # 30-day rolling z-score (720 hours), scaled to bars for the active interval
        roll_mean = sc_aligned.rolling(720 * bph, min_periods=30 * bph).mean()
        roll_std = sc_aligned.rolling(720 * bph, min_periods=30 * bph).std()
```

(`bars_per_hour` is already imported from Task 4.)

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_stage0a_timeframe.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_timeframe.py
git commit -m "fix(research): scale stored stablecoin/OI windows by interval"
```

---

## Task 6: `RESEARCH_INTERVAL` config override + feature-store namespacing

**Files:**
- Modify: `research/pipeline/config.py` (`load_config` return ~309-323; add `_apply_interval_override`)
- Test: `research/tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_config.py`:

```python
# ─── Unit: RESEARCH_INTERVAL env var override ────────────────────────────────

def test_load_config_interval_override_sets_interval(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")
    cfg = load_config(_REAL_CONFIG)
    assert cfg.interval == "15m"


def test_load_config_interval_override_namespaces_feature_store(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")
    cfg = load_config(_REAL_CONFIG)
    assert cfg.feature_store_path.rstrip("/").endswith("/15m")


def test_load_config_interval_1H_leaves_feature_store_unchanged(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    cfg = load_config(_REAL_CONFIG)
    assert not cfg.feature_store_path.rstrip("/").endswith("/1H")


def test_load_config_no_interval_env_unchanged(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    cfg = load_config(_REAL_CONFIG)
    assert cfg.interval in {"15m", "30m", "1H"}  # whatever the YAML ships


def test_load_config_unknown_interval_raises(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "7m")
    with pytest.raises(ValueError, match="RESEARCH_INTERVAL"):
        load_config(_REAL_CONFIG)
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_config.py -k interval -v`
Expected: FAIL — override not applied (interval stays as YAML value; unknown value does not raise).

- [ ] **Step 3: Write minimal implementation**

Add `_apply_interval_override` next to `_apply_symbol_filter` in `research/pipeline/config.py`:

```python
def _apply_interval_override(cfg: ResearchConfig) -> ResearchConfig:
    """If RESEARCH_INTERVAL is set, return a copy with that candle interval and a
    namespaced feature store.

    Lets the dashboard / CLI run the pipeline at 15m or 30m without editing the
    YAML, mirroring RESEARCH_ONLY_SYMBOL. The feature store is suffixed with the
    interval (e.g. research/manifests/15m) so sub-hour artifacts never clobber the
    1H ones. Unset or "1H" -> path unchanged (zero regression). Unknown -> ValueError.
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
    new_store = cfg.feature_store_path
    if iv != "1H":
        new_store = f"{cfg.feature_store_path.rstrip('/')}/{iv}"
    return dataclasses.replace(cfg, interval=iv, feature_store_path=new_store)
```

Change the final return of `load_config` (currently `return _apply_symbol_filter(cfg)` at ~323):

```python
    return _apply_interval_override(_apply_symbol_filter(cfg))
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_config.py -k interval -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/config.py research/tests/test_config.py
git commit -m "feat(research): RESEARCH_INTERVAL env override + feature-store namespacing"
```

---

## Task 7: Wire stage0a evidence call to pass `cfg.interval`

**Files:**
- Modify: `research/pipeline/stage0a_features.py:503`

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_stage0a_timeframe.py`:

```python
import inspect

from pipeline import stage0a_features as s0a


def test_stage0a_passes_interval_to_compute_evidence_entries():
    src = inspect.getsource(s0a)
    # The production call site must forward the configured interval, otherwise
    # evidence IC silently reverts to 1H scaling on a 15m run.
    assert "compute_evidence_entries(candles, feature_dict, cfg.horizons_h, interval=cfg.interval)" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_stage0a_timeframe.py::test_stage0a_passes_interval_to_compute_evidence_entries -v`
Expected: FAIL — current call (line 503) omits `interval=`.

- [ ] **Step 3: Write minimal implementation**

Change `research/pipeline/stage0a_features.py:503`:

```python
        entries = compute_evidence_entries(candles, feature_dict, cfg.horizons_h, interval=cfg.interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_stage0a_timeframe.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_timeframe.py
git commit -m "fix(research): stage0a forwards cfg.interval into evidence IC"
```

---

## Task 8: Funding fee — interval-aware sub-hour settlement

**Files:**
- Modify: `agent/backtest/engines/_market_hooks.py:148-194` (`calc_crypto_funding_fee`)
- Modify: `agent/backtest/engines/crypto.py:34-69` (read interval, pass it)
- Test: `agent/tests/test_crypto_funding_subhour.py`

**Context:** The 1H/daily path stays exactly as-is (the existing `else` daily-fallback is preserved so validated 1H strategy numbers don't move). Only sub-hour intervals (`15m`/`30m`/`1m`/`5m`) switch to settlement-only: funding is charged once per 8h boundary bar (hour ∈ {0,8,16}, minute 0) and never via the daily fallback. A separate follow-up should revisit the latent 1H over-charge, but that is out of scope here.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_crypto_funding_subhour.py
"""Funding settlement cadence on sub-hour bars — run from agent/ (pytest tests/)."""
import pandas as pd

from backtest.engines._market_hooks import calc_crypto_funding_fee
from backtest.models import Position


def _long_position(symbol="BTC-USDT", size=1.0, price=100.0):
    return Position(
        symbol=symbol, direction=1, size=size, entry_price=price, leverage=1.0
    )


def _charges_over_one_day(interval, freq):
    """Count non-zero funding charges for a held long over a full UTC day."""
    symbol = "BTC-USDT"
    positions = {symbol: _long_position(symbol)}
    applied, daily = set(), set()
    idx = pd.date_range("2025-01-01 00:00", "2025-01-01 23:59", freq=freq, tz="UTC")
    charges = []
    for ts in idx:
        bar = pd.Series({"close": 100.0})
        fee = calc_crypto_funding_fee(
            symbol, bar, ts, positions, 0.0001, applied, daily, interval=interval
        )
        if fee != 0.0:
            charges.append(ts)
    return charges


def test_15m_charges_exactly_three_settlements_per_day():
    charges = _charges_over_one_day("15m", "15min")
    assert [t.hour for t in charges] == [0, 8, 16]


def test_30m_charges_exactly_three_settlements_per_day():
    charges = _charges_over_one_day("30m", "30min")
    assert [t.hour for t in charges] == [0, 8, 16]


def test_1H_behaviour_preserved_with_default_interval():
    # Default interval keeps the legacy path: the call must still accept no
    # interval kwarg and charge at the settlement hours.
    symbol = "BTC-USDT"
    positions = {symbol: _long_position(symbol)}
    applied, daily = set(), set()
    ts = pd.Timestamp("2025-01-01 08:00", tz="UTC")
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(symbol, bar, ts, positions, 0.0001, applied, daily)
    assert fee == 100.0 * 0.0001 * 1  # notional * rate * direction
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `agent/`): `python -m pytest tests/test_crypto_funding_subhour.py -v`
Expected: FAIL — `TypeError` (no `interval` kwarg); the 15m/30m bars would otherwise also charge a spurious daily-fallback at 00:15/00:30.

- [ ] **Step 3: Write minimal implementation**

(a) `agent/backtest/engines/_market_hooks.py` — change `calc_crypto_funding_fee` signature (line 148) to add a trailing `interval` param:

```python
def calc_crypto_funding_fee(
    symbol: str,
    bar: pd.Series,
    timestamp: pd.Timestamp,
    positions: Dict[str, Position],
    funding_rate: float,
    applied_set: set,
    daily_done_set: set,
    interval: str = "1D",
) -> float:
```

Then branch the dedup logic — replace the block at lines 171-186 (`if not hasattr(...)` through the `daily_done_set.add(day_key)`):

```python
    if not hasattr(timestamp, "date"):
        return 0.0

    current_date = timestamp.date()
    hour = timestamp.hour if hasattr(timestamp, "hour") else 0
    minute = timestamp.minute if hasattr(timestamp, "minute") else 0

    _SUB_HOUR = {"1m", "5m", "15m", "30m"}
    if interval in _SUB_HOUR:
        # Settlement-only: charge once at each 8h boundary bar; no daily fallback
        # (continuous sub-hour bars include the exact 00/08/16 bars).
        if hour in FUNDING_HOURS and minute == 0:
            key = (symbol, current_date, hour)
            if key in applied_set:
                return 0.0
            applied_set.add(key)
        else:
            return 0.0
    else:
        # Legacy 1H/daily path — unchanged.
        if hour in FUNDING_HOURS:
            key = (symbol, current_date, hour)
            if key in applied_set:
                return 0.0
            applied_set.add(key)
        else:
            day_key = (symbol, current_date)
            if day_key in daily_done_set:
                return 0.0
            daily_done_set.add(day_key)
```

(The `pos = positions.get(symbol)` / notional return below stays unchanged.)

(b) `agent/backtest/engines/crypto.py` — read interval in `__init__` and pass it in `on_bar`. Add after line 39 (`self.funding_rate = ...`):

```python
        self.interval: str = config.get("interval", "1D")
```

Change the `calc_crypto_funding_fee(...)` call in `on_bar` (lines 66-69) to pass it:

```python
        fee = calc_crypto_funding_fee(
            symbol, bar, timestamp, self.positions,
            self.funding_rate, self._funding_applied, self._funding_daily_done,
            interval=self.interval,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `agent/`): `python -m pytest tests/test_crypto_funding_subhour.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the existing crypto-engine suite for zero regression**

Run (from `agent/`): `python -m pytest tests/test_crypto_engine.py -v`
Expected: PASS (all existing tests green — the default `interval="1D"` preserves legacy behaviour).

- [ ] **Step 6: Commit**

```bash
git add agent/backtest/engines/_market_hooks.py agent/backtest/engines/crypto.py agent/tests/test_crypto_funding_subhour.py
git commit -m "fix(engine): interval-aware funding settlement for sub-hour bars"
```

---

## Task 9: Full-suite regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the research suite**

Run (from `research/`): `python -m pytest tests/ -q`
Expected: PASS — all green. (If a pre-existing unrelated failure appears, confirm it also fails on `main` before treating it as a regression.)

- [ ] **Step 2: Run the agent suite**

Run (from `agent/`): `python -m pytest tests/ -q`
Expected: PASS — all green.

- [ ] **Step 3: Commit (only if any test-collateral fixups were needed)**

```bash
git add -A
git commit -m "test: green research + agent suites after interval refactor"
```

---

## Task 10: Phase 1 smoke — null-result validation at 15m (manual)

**Files:** none (operational verification; not CI). Record the outcome in the PR description.

- [ ] **Step 1: Run stage0a → stage1 for ETH at 15m**

Run (PowerShell, from repo root):

```powershell
$env:RESEARCH_INTERVAL = "15m"
$env:RESEARCH_ONLY_SYMBOL = "eth"
cd research
python -m pipeline.stage0a_features
python -m pipeline.stage0_discovery
python -m pipeline.stage1_factors
```

- [ ] **Step 2: Verify the plumbing**

Confirm ALL of:
- Artifacts written under `research/manifests/15m/` (feature parquet + `evidence_eth.json`), NOT under `research/manifests/` root.
- `evidence_eth.json` exists and parses; entries carry `ic_by_horizon` keyed by the configured horizons.
- No crash / no traceback in any of the three stages.
- The slow factors (`funding_*`, `stablecoin_supply_z`) show |IC| near zero at short horizons — the expected **null result** that proves the wiring is correct (this is NOT a claim of alpha).

- [ ] **Step 3: Clean up env**

```powershell
Remove-Item Env:RESEARCH_INTERVAL
Remove-Item Env:RESEARCH_ONLY_SYMBOL
```

- [ ] **Step 4: Record findings**

Write a 3-5 line note in the PR description: bar count fetched, top evidence entries + their IC, confirmation that 1H artifacts were untouched. This closes Phase 1.

---

## Phase 2 (separate plan — not in this document)

After Phase 1 is merged and the smoke is recorded, the next plan adds the intraday OHLCV factor library and runs discovery:

- New factor functions in `research/lib/indicators.py` (short-term momentum/reversal over 4/8/16 bars, realised-vol breakout, volume z-score, range/ATR expansion, hour-of-day seasonality), registered in `compute_indicator_pool` + `_INDICATOR_CATEGORY` + the `indicator_pool` config, with short `horizons_h` (1/2/4/8h).
- Operational runs of stage0a→stage5 at 15m and 30m for ETH + BTC, recording which intraday factors surface real IC.
- Optional dashboard interval dimension.

These are written as their own plan because the factor windows and any artifact-layout follow-ups depend on Phase 1 being concrete, and the discovery runs are exploratory rather than TDD-able.

---

## Self-Review Notes

- **Spec coverage:** §4 coupling table rows 1-5 → Tasks 2/3/5 (forward returns, IC window, stored windows), Task 4 (IC eval transform), Task 8 (funding), Task 6 (config override + namespacing). Row 2 (annualization) needs no code change — `calc_bars_per_year` already keys on interval and stage3 already writes `cfg.interval` into run config.json; the Task 10 smoke confirms it end-to-end. Data-fetch coupling (`bar=cfg.interval`) was already present in stage0a, so no task is required.
- **Out-of-scope, deliberately:** legacy aux scripts `research/factor_regime.py` and `research/factor_extended.py` still hardcode `bar="1H"`; they are not on the stage0a→5 path and are not retrofitted here.
- **Latent bug flagged, not fixed:** the 1H funding path over-charges (~4 settlements/day). Preserved for zero regression; revisit separately.
