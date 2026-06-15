# research/tests/test_intraday_factors.py
"""Intraday OHLCV factor correctness + causality — run from research/ (pytest tests/)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

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
