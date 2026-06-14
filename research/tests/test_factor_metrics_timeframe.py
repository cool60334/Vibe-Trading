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
