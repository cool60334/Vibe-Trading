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
