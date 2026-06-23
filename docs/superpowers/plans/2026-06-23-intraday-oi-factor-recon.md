# Intraday OI Factor Recon — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A cheap, decisive recon that answers — per coin, at 15m/30m — whether positioning/OI factors carry **incremental intraday alpha beyond their own 1H signal**, with the Binance-archive timestamp hazard correctly normalized. Output a GO/NO-GO table; build nothing into production.

**Architecture:** New data-hygiene helpers in `oi_metrics.py` (per-file `create_time` normalization + a generalized `aggregate_to_interval`), pure intraday factor builders + a causal-1H control in a new `intraday_oi_factors.py`, a pure verdict classifier, and a thin driver script that composes everything with the existing `orderflow_eval` evaluators. No changes to production `derived_factors`/`stage0a`.

**Tech Stack:** pandas, numpy, scipy (already deps). Reuses `lib/orderflow_eval.py`, `lib/oi_metrics.py`, `lib/timeframe.py`, `lib/okx_data.py`.

## Spec
`docs/superpowers/specs/2026-06-23-intraday-oi-factor-recon-design.md`

## Conventions
- Run research tests from `research/`: `cd research && python -m pytest tests/<f> -v` (CWD on sys.path; separate from dashboard/server suite).
- `orderflow_eval` is already tested — reuse, don't retest. Note `incremental_ic`/`_ic` default `min_n=30`, so IC tests need ≥30 aligned rows or pass a smaller `min_obs` where supported.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

## File Structure
| File | Responsibility |
|---|---|
| `research/lib/oi_metrics.py` (modify) | add `normalize_create_time`, `aggregate_to_interval`; make `aggregate_to_hourly` a thin wrapper |
| `research/tests/test_oi_metrics.py` (modify) | tests for the two new helpers |
| `research/lib/intraday_oi_factors.py` (create) | interval-correct factor builders + `causal_1h_control` |
| `research/lib/intraday_oi_recon.py` (create) | pure `classify_factor` verdict logic |
| `research/tests/test_intraday_oi_factors.py` (create) | tests for builders + control + classifier |
| `research/scripts/intraday_oi_recon.py` (create) | driver: load→normalize→aggregate→factors→evaluate→report |

---

### Task 1: `oi_metrics` — per-file timestamp normalization + interval aggregation

**Files:**
- Modify: `research/lib/oi_metrics.py` (add after `aggregate_to_hourly`, ~line 78)
- Test: `research/tests/test_oi_metrics.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `research/tests/test_oi_metrics.py`:

```python
# ── timestamp normalization (mixed archive conventions) ─────────────────────

def test_normalize_create_time_shifts_open_labeled_to_close():
    # OPEN-labeled: first row at midnight, 5-min cadence
    idx = pd.date_range("2024-01-01 00:00", periods=12, freq="5min", tz="UTC")
    df = pd.DataFrame({"oi": range(12)}, index=idx)
    out = oi_metrics.normalize_create_time(df)
    # every stamp moved forward one cadence -> window-close labeling
    assert out.index[0] == pd.Timestamp("2024-01-01 00:05", tz="UTC")
    assert list(out["oi"]) == list(range(12))  # values unchanged


def test_normalize_create_time_leaves_close_labeled_unchanged():
    # CLOSE-labeled: first row one cadence past midnight (2025+ convention)
    idx = pd.date_range("2025-01-01 00:05", periods=12, freq="5min", tz="UTC")
    df = pd.DataFrame({"oi": range(12)}, index=idx)
    out = oi_metrics.normalize_create_time(df)
    assert out.index[0] == pd.Timestamp("2025-01-01 00:05", tz="UTC")  # unchanged


def test_normalize_create_time_handles_2p5min_cadence():
    # 2020-21 cadence is 2.5min; OPEN-labeled -> shift by 2.5min
    idx = pd.date_range("2021-01-01 00:00", periods=8, freq="150s", tz="UTC")
    df = pd.DataFrame({"oi": range(8)}, index=idx)
    out = oi_metrics.normalize_create_time(df)
    assert out.index[0] == pd.Timestamp("2021-01-01 00:02:30", tz="UTC")


# ── aggregate_to_interval (generalizes aggregate_to_hourly) ─────────────────

def test_aggregate_to_interval_1h_matches_hourly():
    idx = pd.date_range("2024-06-01 00:00", periods=24, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {"oi": range(24), "oi_usd": [v * 4000.0 for v in range(24)],
         "toptrader_ls_accounts": 2.0, "toptrader_ls_positions": 2.0,
         "global_ls_accounts": 2.0, "taker_buysell_ratio": [1.0] * 12 + [2.0] * 12},
        index=idx,
    )
    pd.testing.assert_frame_equal(
        oi_metrics.aggregate_to_interval(df, "1h"), oi_metrics.aggregate_to_hourly(df)
    )


def test_aggregate_to_interval_30min_snapshot_last_flow_mean():
    idx = pd.date_range("2024-06-01 00:00", periods=12, freq="5min", tz="UTC")  # 1h of data
    df = pd.DataFrame(
        {"oi": range(12), "oi_usd": [0.0] * 12,
         "toptrader_ls_accounts": 1.0, "toptrader_ls_positions": 1.0,
         "global_ls_accounts": 1.0, "taker_buysell_ratio": [1.0] * 6 + [3.0] * 6},
        index=idx,
    )
    out = oi_metrics.aggregate_to_interval(df, "30min")
    assert list(out.index) == [
        pd.Timestamp("2024-06-01 00:00", tz="UTC"),
        pd.Timestamp("2024-06-01 00:30", tz="UTC"),
    ]
    assert out["oi"].iloc[0] == 5 and out["oi"].iloc[1] == 11      # snapshot = last in bar
    assert out["taker_buysell_ratio"].iloc[0] == 1.0 and out["taker_buysell_ratio"].iloc[1] == 3.0  # flow = mean
```

- [ ] **Step 2: Run — verify fail**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "normalize or aggregate_to_interval" -v`
Expected: FAIL — `AttributeError: module 'lib.oi_metrics' has no attribute 'normalize_create_time'`

- [ ] **Step 3: Implement**

In `research/lib/oi_metrics.py`, replace `aggregate_to_hourly` (lines 65-77) with the generalized pair, and add `normalize_create_time`:

```python
def aggregate_to_interval(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample 5-/2.5-minute snapshots to a continuous `freq` grid (pandas
    offset alias, e.g. '1h', '30min', '15min').

    Snapshots (OI, L/S ratios) -> last in the bar; taker flow -> mean. Empty
    bars stay NaN (never forward-filled — ffill inflates IC).
    """
    if df.empty:
        return df
    agg = {c: "last" for c in _SNAPSHOT_COLS if c in df.columns}
    agg.update({c: "mean" for c in _FLOW_COLS if c in df.columns})
    out = df.resample(freq, label="left", closed="left").agg(agg)
    return out[[c for c in _COLS if c in out.columns]]


def aggregate_to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """1H aggregation (thin wrapper over aggregate_to_interval; zero regression)."""
    return aggregate_to_interval(df, "1h")


def normalize_create_time(df: pd.DataFrame) -> pd.DataFrame:
    """Stamp every snapshot at its window CLOSE, fixing the archive's mixed
    conventions. Pre-2025 daily files are OPEN-labeled (first row at 00:00);
    2025+ are CLOSE-labeled (first row one cadence in). Cadence is 2.5min
    (2020-21) or 5min (2022+). Apply ONCE per raw day-file before aggregation.

    Detects per-file: if the first stamp is within half a cadence of the day
    boundary it is OPEN-labeled and is shifted forward one cadence; otherwise
    it is already close-labeled and returned unchanged.
    """
    if df.empty or len(df) < 2:
        return df
    cadence = df.index.to_series().diff().median()
    first = df.index[0]
    offset = first - first.normalize()
    if offset < cadence / 2:  # OPEN-labeled
        return df.set_axis(df.index + cadence)
    return df
```

- [ ] **Step 4: Run — verify pass**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -v`
Expected: PASS (new tests + all existing oi_metrics tests — `aggregate_to_hourly` regression covered by `test_aggregate_to_interval_1h_matches_hourly` + the original hourly test).

- [ ] **Step 5: Commit**

```bash
git add research/lib/oi_metrics.py research/tests/test_oi_metrics.py
git commit -m "feat(oi): per-file create_time normalization + aggregate_to_interval

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: intraday OI factor builders + causal-1H control

**Files:**
- Create: `research/lib/intraday_oi_factors.py`
- Test: `research/tests/test_intraday_oi_factors.py`

- [ ] **Step 1: Write failing tests**

Create `research/tests/test_intraday_oi_factors.py`:

```python
import numpy as np
import pandas as pd

from lib import intraday_oi_factors as iof


def _oi_frame(n, freq="30min"):
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"oi": np.linspace(100, 200, n),
         "global_ls_accounts": np.linspace(1.0, 2.0, n),
         "toptrader_ls_positions": np.linspace(2.0, 1.0, n)},
        index=idx,
    )


def test_ls_factors_window_scales_with_interval():
    df = _oi_frame(200, "30min")
    f30 = iof.ls_factors(df, "30m", window_h=10)   # 10h -> 20 bars at 30m
    assert "global_ls_z_s" in f30 and "toptrader_ls_z_s" in f30 and "ls_divergence_s" in f30
    # divergence is exactly the difference of the two z's
    pd.testing.assert_series_equal(
        f30["ls_divergence_s"], (f30["global_ls_z_s"] - f30["toptrader_ls_z_s"]),
        check_names=False,
    )
    # window scaled: at 15m the same 10h is 40 bars, so early NaNs differ
    f15 = iof.ls_factors(_oi_frame(200, "15min"), "15m", window_h=10)
    assert f15["global_ls_z_s"].isna().sum() > f30["global_ls_z_s"].isna().sum()


def test_oi_velocity_factors_present_and_interval_scaled():
    df = _oi_frame(100, "30min")
    close = pd.Series(np.linspace(10, 11, 100), index=df.index)
    f = iof.oi_velocity_factors(df, close, "30m")
    assert set(f) == {"oi_mom_1h", "oi_accel", "oi_price_div"}
    # oi_mom_1h at 30m is pct_change over 2 bars
    pd.testing.assert_series_equal(f["oi_mom_1h"], df["oi"].pct_change(2), check_names=False)


def test_causal_1h_control_has_no_lookahead():
    # 1H factor: value at hour H is known only after H closes (at H+1h)
    h_idx = pd.date_range("2024-01-01 00:00", periods=4, freq="1h", tz="UTC")
    factor_1h = pd.Series([10.0, 20.0, 30.0, 40.0], index=h_idx)
    intraday_idx = pd.date_range("2024-01-01 00:00", periods=8, freq="30min", tz="UTC")
    ctrl = iof.causal_1h_control(factor_1h, intraday_idx)
    # at 00:00 and 00:30 (hour 0 not yet closed) -> NaN (no known 1H value)
    assert np.isnan(ctrl.loc["2024-01-01 00:00"])
    assert np.isnan(ctrl.loc["2024-01-01 00:30"])
    # at 01:00 (hour 0 just closed) -> hour-0 value
    assert ctrl.loc["2024-01-01 01:00"] == 10.0
    assert ctrl.loc["2024-01-01 01:30"] == 10.0
    # at 02:00 -> hour-1 value
    assert ctrl.loc["2024-01-01 02:00"] == 20.0
```

- [ ] **Step 2: Run — verify fail**

Run: `cd research && python -m pytest tests/test_intraday_oi_factors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.intraday_oi_factors'`

- [ ] **Step 3: Implement**

Create `research/lib/intraday_oi_factors.py`:

```python
"""Interval-correct intraday OI / positioning factor builders for the recon.

All hour-anchored windows scale by lib.timeframe.bars_per_hour so the same
window_h means the same wall-clock span at 15m / 30m / 1H. Pure functions; no
network, no production-pipeline dependency.
"""
from __future__ import annotations

import pandas as pd

from lib.timeframe import bars_per_hour


def _rolling_z(s: pd.Series, window_bars: int) -> pd.Series:
    m = s.rolling(window_bars, min_periods=window_bars // 2).mean()
    sd = s.rolling(window_bars, min_periods=window_bars // 2).std()
    return (s - m) / sd


def ls_factors(oi_df: pd.DataFrame, interval: str, window_h: int = 36) -> dict[str, pd.Series]:
    """Short-window L/S positioning z-scores (the live-feed-deployable subset)."""
    w = window_h * bars_per_hour(interval)
    out: dict[str, pd.Series] = {}
    if "global_ls_accounts" in oi_df:
        out["global_ls_z_s"] = _rolling_z(oi_df["global_ls_accounts"], w)
    if "toptrader_ls_positions" in oi_df:
        out["toptrader_ls_z_s"] = _rolling_z(oi_df["toptrader_ls_positions"], w)
    if "global_ls_z_s" in out and "toptrader_ls_z_s" in out:
        out["ls_divergence_s"] = out["global_ls_z_s"] - out["toptrader_ls_z_s"]
    return out


def oi_velocity_factors(oi_df: pd.DataFrame, close: pd.Series, interval: str) -> dict[str, pd.Series]:
    """OI-velocity factors — RESEARCH-ONLY (archive-only, no live feed → not
    intraday-deployable). Magnitude form of oi_price_div (not sign)."""
    bph = bars_per_hour(interval)
    oi = oi_df["oi"]
    mom = oi.pct_change(1 * bph)
    return {
        "oi_mom_1h": mom,
        "oi_accel": mom.diff(1 * bph),
        "oi_price_div": oi.pct_change(1 * bph) * close.pct_change(1 * bph),
    }


# Factors with a live 5-min feed (oi_metrics._LIVE_LS_COLS) — the only intraday-deployable set.
DEPLOYABLE_FACTORS = frozenset({"global_ls_z_s", "toptrader_ls_z_s", "ls_divergence_s"})


def build_intraday_oi_factors(oi_df: pd.DataFrame, close: pd.Series, interval: str) -> dict[str, pd.Series]:
    f = ls_factors(oi_df, interval)
    f.update(oi_velocity_factors(oi_df, close, interval))
    return f


def causal_1h_control(factor_1h: pd.Series, intraday_index: pd.Index) -> pd.Series:
    """The 1H factor reindexed onto the intraday grid WITHOUT look-ahead.

    A left-labeled 1H bar stamped at hour H spans [H, H+1h) and is only known at
    H+1h (its close). Shift by one 1H bar so the value first becomes available at
    H+1h, then forward-fill onto the intraday grid. An intraday bar inside hour H
    therefore sees hour (H-1)'s value, never hour H's.
    """
    return factor_1h.shift(1).reindex(intraday_index, method="ffill")
```

- [ ] **Step 4: Run — verify pass**

Run: `cd research && python -m pytest tests/test_intraday_oi_factors.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/intraday_oi_factors.py research/tests/test_intraday_oi_factors.py
git commit -m "feat(intraday-oi): interval-correct factor builders + causal-1H control

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: pure verdict classifier

**Files:**
- Create: `research/lib/intraday_oi_recon.py`
- Test: `research/tests/test_intraday_oi_factors.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `research/tests/test_intraday_oi_factors.py`:

```python
from lib import intraday_oi_recon as recon


def test_classify_nogo_when_ic_below_floor():
    v, reasons = recon.classify_factor(decay={4: 0.01, 8: 0.02}, incr_vs_1h=0.05, peak_h=2.0)
    assert v == "NO_GO" and any("IC" in r for r in reasons)


def test_classify_nogo_when_no_incremental_over_1h():
    # strong raw IC but ~0 incremental vs its own 1H => intraday adds nothing
    v, reasons = recon.classify_factor(decay={2: 0.08}, incr_vs_1h=0.004, peak_h=2.0)
    assert v == "NO_GO" and any("1H" in r for r in reasons)


def test_classify_nogo_when_peak_is_slow():
    # edge only at 72h => it's the slow signal in disguise, not intraday
    v, reasons = recon.classify_factor(decay={144: 0.06}, incr_vs_1h=0.05, peak_h=72.0)
    assert v == "NO_GO" and any("slow" in r or "peak" in r for r in reasons)


def test_classify_go_when_intraday_incremental_and_fast():
    v, reasons = recon.classify_factor(decay={2: 0.06}, incr_vs_1h=0.045, peak_h=2.0)
    assert v == "GO"
```

- [ ] **Step 2: Run — verify fail**

Run: `cd research && python -m pytest tests/test_intraday_oi_factors.py -k classify -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.intraday_oi_recon'`

- [ ] **Step 3: Implement**

Create `research/lib/intraday_oi_recon.py`:

```python
"""Pure GO/NO-GO verdict for an intraday OI/positioning factor.

The decisive gate is incremental IC vs the factor's own causal 1H value: if the
intraday factor adds ~nothing beyond the slow 1H signal, it's NO_GO regardless of
raw IC. Per-coin only (positioning is coin-specific — no cross-coin gate).
"""
from __future__ import annotations

import math

MIN_ABS_IC = 0.03            # consistent with the stage-1 screening gate
INTRADAY_PEAK_MAX_H = 24.0   # an edge peaking beyond this is the slow signal, not intraday


def classify_factor(
    decay: dict[float, float],
    incr_vs_1h: float,
    peak_h: float | None,
    min_abs_ic: float = MIN_ABS_IC,
    intraday_peak_max_h: float = INTRADAY_PEAK_MAX_H,
) -> tuple[str, list[str]]:
    """decay: {horizon_hours -> IC}. incr_vs_1h: incremental IC vs own causal 1H.
    peak_h: horizon (hours) of max |IC|."""
    ics = [abs(v) for v in decay.values() if v is not None and not math.isnan(v)]
    max_ic = max(ics) if ics else 0.0
    if max_ic < min_abs_ic:
        return "NO_GO", [f"max |IC| {max_ic:.3f} < {min_abs_ic}"]
    if incr_vs_1h is None or math.isnan(incr_vs_1h) or abs(incr_vs_1h) < min_abs_ic:
        return "NO_GO", [
            f"incremental IC vs own 1H {incr_vs_1h:.3f} ~ 0 — intraday adds nothing beyond the 1H signal"
        ]
    if peak_h is None or peak_h > intraday_peak_max_h:
        return "NO_GO", [f"IC peaks at {peak_h}h — slow signal in disguise, not intraday"]
    return "GO", [
        f"max |IC| {max_ic:.3f}, incremental vs 1H {incr_vs_1h:.3f}, peak {peak_h}h — worth a cost-aware backtest"
    ]
```

- [ ] **Step 4: Run — verify pass**

Run: `cd research && python -m pytest tests/test_intraday_oi_factors.py -k classify -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/intraday_oi_recon.py research/tests/test_intraday_oi_factors.py
git commit -m "feat(intraday-oi): pure GO/NO-GO verdict classifier

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: driver script

**Files:**
- Create: `research/scripts/intraday_oi_recon.py`
- Test: `research/tests/test_intraday_oi_factors.py` (append a driver smoke test)

- [ ] **Step 1: Write failing test (driver building blocks are importable + report shape)**

Append to `research/tests/test_intraday_oi_factors.py`:

```python
def test_driver_run_recon_report_shape(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, "scripts")
    import intraday_oi_recon as driver

    # synthetic 30m OI frame + close with a built-in 2-bar-forward edge in ls_divergence
    n = 400
    idx = pd.date_range("2024-01-01", periods=n, freq="30min", tz="UTC")
    rng = np.random.default_rng(0)
    g = pd.Series(np.cumsum(rng.normal(0, 1, n)) + 50, index=idx)
    t = pd.Series(np.cumsum(rng.normal(0, 1, n)) + 50, index=idx)
    oi_df = pd.DataFrame({"oi": g * 100, "global_ls_accounts": g, "toptrader_ls_positions": t}, index=idx)
    close = pd.Series(np.cumsum(rng.normal(0, 0.5, n)) + 1000, index=idx)

    report = driver.run_recon(oi_df, close, interval="30m", symbol="sol")
    assert report["symbol"] == "sol" and report["interval"] == "30m"
    f = report["factors"]
    assert "ls_divergence_s" in f
    row = f["ls_divergence_s"]
    assert {"max_abs_ic", "peak_h", "incr_vs_1h", "verdict", "deployable"} <= set(row)
    assert row["deployable"] is True
    assert row["verdict"] in {"GO", "NO_GO"}
```

- [ ] **Step 2: Run — verify fail**

Run: `cd research && python -m pytest tests/test_intraday_oi_factors.py -k driver -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'intraday_oi_recon'`

- [ ] **Step 3: Implement the driver**

Create `research/scripts/intraday_oi_recon.py`:

```python
"""Intraday OI/positioning factor recon — GO/NO-GO per coin at 15m/30m.

    python research/scripts/intraday_oi_recon.py --symbol sol --interval 30m

Loads the Binance OI archive (raw 5-min, per-file create_time normalized),
re-aggregates to the target interval AND to 1H, builds interval-correct factors,
and for each measures: decay IC across horizons, half-life, the decisive
incremental IC vs its own causal 1H value, and a realistic-entry-lag execution IC.
Writes research/manifests/<interval>/intraday_oi_recon_<sym>.json + prints a table.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

_SCRIPTS = Path(__file__).resolve().parent
_RESEARCH = _SCRIPTS.parent
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from lib import intraday_oi_factors as iof          # noqa: E402
from lib import intraday_oi_recon as verdict        # noqa: E402
from lib import oi_metrics, orderflow_eval          # noqa: E402
from lib.timeframe import bars_per_hour             # noqa: E402

_PANDAS_FREQ = {"15m": "15min", "30m": "30min", "1H": "1h"}
_HORIZONS_H = [0.5, 1, 2, 4, 8, 24, 48, 72]


def run_recon(oi_df: pd.DataFrame, close: pd.Series, interval: str, symbol: str) -> dict:
    """Pure core: given an interval OI frame + aligned close, evaluate every factor."""
    bph = bars_per_hour(interval)
    close = close.reindex(oi_df.index)
    factors_iv = iof.build_intraday_oi_factors(oi_df, close, interval)

    # 1H versions for the causal-1H control (resample OI to 1H, rebuild at 1H)
    oi_1h = oi_df.resample("1h", label="left", closed="left").last()
    close_1h = close.resample("1h", label="left", closed="left").last()
    factors_1h = iof.build_intraday_oi_factors(oi_1h, close_1h, "1H")

    max_bars = int(max(_HORIZONS_H) * bph)
    out: dict[str, dict] = {}
    for name, fac in factors_iv.items():
        decay = orderflow_eval.decay_profile(fac, close, max_bars=max_bars)
        decay_h = {h: decay.get(int(h * bph)) for h in _HORIZONS_H if int(h * bph) >= 1}
        valid = {h: v for h, v in decay_h.items() if v is not None and v == v}
        peak_h = max(valid, key=lambda h: abs(valid[h])) if valid else None
        ctrl = iof.causal_1h_control(factors_1h.get(name, pd.Series(dtype=float)), oi_df.index)
        incr = orderflow_eval.incremental_ic(fac, ctrl, close, fwd_bars=max(1, int(2 * bph)))
        exec0 = orderflow_eval.execution_ic(fac, close, entry_lag_bars=0, hold_bars=max(1, int(2 * bph)))
        exec1 = orderflow_eval.execution_ic(fac, close, entry_lag_bars=1, hold_bars=max(1, int(2 * bph)))
        v, reasons = verdict.classify_factor(decay_h, incr, peak_h)
        out[name] = {
            "deployable": name in iof.DEPLOYABLE_FACTORS,
            "max_abs_ic": max((abs(v2) for v2 in valid.values()), default=0.0),
            "peak_h": peak_h,
            "ic_by_h": valid,
            "half_life_bars": orderflow_eval.half_life_bars(decay),
            "incr_vs_1h": incr,
            "exec_ic_lag0": exec0,
            "exec_ic_lag1": exec1,
            "verdict": v,
            "reasons": reasons,
        }
    return {"symbol": symbol, "interval": interval, "factors": out}


def _load_interval_oi(symbol_binance: str, interval: str, cache_dir: Path,
                      start: date, end: date) -> pd.DataFrame:
    """Load raw 5-min metrics day-by-day, normalize each file's create_time,
    then aggregate to the target interval."""
    frames = []
    day = start
    from datetime import timedelta
    while day <= end:
        zp = oi_metrics.binance_dump.download_metrics_day(symbol_binance, day, cache_dir, verify=False)
        if zp is not None:
            frames.append(oi_metrics.normalize_create_time(oi_metrics.read_metrics_zip(zp)))
        day += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    raw = pd.concat(frames).sort_index()
    raw = raw[~raw.index.duplicated(keep="last")]
    return oi_metrics.aggregate_to_interval(raw, _PANDAS_FREQ[interval])


def _format_table(report: dict) -> str:
    lines = [f"{report['symbol']} @ {report['interval']}",
             f"{'factor':22s} {'dep':4s} {'maxIC':>7s} {'peak_h':>7s} {'incr1H':>7s} {'lag0':>6s} {'lag1':>6s} verdict"]
    for name, r in report["factors"].items():
        def f(x): return f"{x:.3f}" if isinstance(x, float) and x == x else "—"
        lines.append(f"{name:22s} {'Y' if r['deployable'] else 'n':4s} "
                     f"{f(r['max_abs_ic']):>7s} {str(r['peak_h']):>7s} {f(r['incr_vs_1h']):>7s} "
                     f"{f(r['exec_ic_lag0']):>6s} {f(r['exec_ic_lag1']):>6s} {r['verdict']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Intraday OI factor recon")
    p.add_argument("--symbol", required=True, help="short coin name, e.g. sol")
    p.add_argument("--interval", default="30m", choices=["15m", "30m"])
    p.add_argument("--cache-dir", default=str(_RESEARCH / "data" / "oi" / "_raw"))
    p.add_argument("--start", type=date.fromisoformat, default=date(2022, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date.today())
    args = p.parse_args(argv)

    from pipeline.config import load_config
    cfg = load_config()
    sym = next((s for s in cfg.symbols if s.name == args.symbol), None)
    if sym is None:
        raise SystemExit(f"unknown symbol {args.symbol!r}")

    oi_df = _load_interval_oi(sym.binance_usdt, args.interval, Path(args.cache_dir), args.start, args.end)
    if oi_df.empty:
        raise SystemExit(f"no OI data for {sym.binance_usdt} in range")
    from lib import okx_data
    days = (args.end - args.start).days
    candles = okx_data.fetch_candles(sym.okx_swap, days=days, bar=args.interval)
    report = run_recon(oi_df, candles["close"], args.interval, args.symbol)

    out_dir = _RESEARCH / "manifests" / args.interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"intraday_oi_recon_{args.symbol}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(_format_table(report))
    print(f"\nreport -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run — verify pass**

Run: `cd research && python -m pytest tests/test_intraday_oi_factors.py -k driver -v`
Expected: PASS (the synthetic frame flows through `run_recon` and yields a well-formed report; `ls_divergence_s` flagged deployable with a GO/NO_GO verdict).

- [ ] **Step 5: Commit**

```bash
git add research/scripts/intraday_oi_recon.py research/tests/test_intraday_oi_factors.py
git commit -m "feat(intraday-oi): recon driver (load->normalize->aggregate->evaluate)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: full suite + live recon run + interpret

**Files:** none (verification + research run)

- [ ] **Step 1: Full new-test suite green**

Run: `cd research && python -m pytest tests/test_oi_metrics.py tests/test_intraday_oi_factors.py -v`
Expected: all PASS, zero real network (all synthetic/local).

- [ ] **Step 2: Confirm raw 5-min cache exists for btc/eth/sol**

Run: `ls research/data/oi/_raw/ | head` (or the configured cache dir). If the raw daily-metrics ZIPs are present (they were dumped earlier), the recon runs offline. If absent, run a small dump first (network): `python research/dump_oi.py --symbols SOLUSDT --start 2022-01-01` (heavy; run in background, mind the env's long-job reaping — chunk by year if needed).

- [ ] **Step 3: Live recon — SOL then ETH/BTC at 30m**

Run (per coin, foreground; each reads local cache so it's fast):
`python research/scripts/intraday_oi_recon.py --symbol sol --interval 30m`
then `--symbol eth`, `--symbol btc`. Repeat key ones at `--interval 15m`.

- [ ] **Step 4: Interpret + decide**

Read each `research/manifests/30m/intraday_oi_recon_<sym>.json`. The decisive column is `incr_vs_1h`:
- If the deployable L/S factors all show `incr_vs_1h ≈ 0` (NO_GO) → intraday positioning is the 1H signal sampled more often → **close the intraday-positioning class** (record the numbers in memory; this is the confirm-and-close outcome).
- If any deployable factor clears `|IC|≥0.03`, `incr_vs_1h` significant, peak ≤24h → **GO candidate**; next step (separate) is a cost-aware intraday backtest, not deployment.

Also sanity-check the logged NaN-bar fraction per interval (gap density) and that `peak_h` is not always 72 (would confirm slow-signal-in-disguise).

- [ ] **Step 5: Record outcome**

Update memory `project_intraday_oi_recon_review` with the measured `incr_vs_1h` per coin/factor and the GO/NO-GO verdict. Do not commit `research/manifests/<interval>/intraday_oi_recon_*.json` (generated artifact under gitignored manifests).

---

## Self-Review

**Spec coverage:**
- Data hazard: per-file `create_time` normalization (OPEN≤2024→CLOSE2025+, 2.5/5-min cadence) + `aggregate_to_interval` → Task 1 ✓
- Deployable arm = 3 live-feed L/S factors; OI-velocity research-only labeled (`DEPLOYABLE_FACTORS`) → Task 2 ✓
- Interval-correct windows (bars_per_hour) → Task 2 ✓
- Core test = incremental IC vs own causal 1H (no look-ahead) → Task 2 (`causal_1h_control`) + Task 4 (wired) ✓
- Verdict: ≥0.03, incremental>0, peak ≤24h, per-coin (no cross-coin) → Task 3 ✓
- Reuse orderflow_eval (decay/half_life/incremental_ic/execution_ic), no production wiring → Task 4 ✓
- Run + interpret + confirm-and-close → Task 5 ✓

**Placeholder scan:** No TBD/TODO; pure-function tasks (1-3) have full code + assertions; driver (4) has complete code; run steps (5) have exact commands + decision rule. ✓

**Type consistency:** `aggregate_to_interval(df, freq)`, `normalize_create_time(df)` consistent (Task 1 def ↔ Task 4 use). `ls_factors`/`oi_velocity_factors`/`build_intraday_oi_factors`/`causal_1h_control`/`DEPLOYABLE_FACTORS` consistent (Task 2 def ↔ Task 4 use). `classify_factor(decay, incr_vs_1h, peak_h, …)` signature matches across Task 3 def, its tests, and the Task 4 call. `run_recon(oi_df, close, interval, symbol)` matches the Task 4 smoke test. orderflow_eval calls match the real signatures (`decay_profile(factor, price, max_bars)`, `incremental_ic(factor, control, price, fwd_bars)`, `execution_ic(factor, price, entry_lag_bars, hold_bars)`). ✓

**Known risk (documented, acceptable for a recon):** `_load_interval_oi` uses `verify=False` for speed on cached zips; the live run reads the local `_raw` cache so it's offline + fast. The driver smoke test exercises `run_recon` (the pure core) directly, not the network loader — network paths are covered by existing `oi_metrics`/`binance_dump` tests.
