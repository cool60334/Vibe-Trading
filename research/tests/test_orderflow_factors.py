import sys
from pathlib import Path
_RESEARCH_DIR = Path(__file__).resolve().parents[1]  # research/
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

import numpy as np
import pandas as pd
import pytest

from lib.orderflow_factors import orderflow_factors


def _of_df(ts, **cols):
    return pd.DataFrame({"ts": ts, **cols}).set_index("ts")


def test_trade_imbalance_basic():
    idx = pd.to_datetime(["2025-01-01 00:00", "2025-01-01 00:30"], utc=True)
    of = _of_df(idx, buy_vol=[3.0, 1.0], sell_vol=[1.0, 1.0],
               buy_count=[2, 1], sell_count=[1, 1],
               total_vol=[4.0, 2.0],
               vol_lt10k=[2.0, 2.0], vol_10_50k=[0.0, 0.0],
               vol_50_200k=[2.0, 0.0], vol_gt200k=[0.0, 0.0],
               open=[100.0, 101.0], close=[101.0, 101.0])
    out = orderflow_factors(of, idx, "30m")
    assert out["trade_imbalance"].iloc[0] == pytest.approx(0.5)   # (3-1)/4
    assert out["trade_count_imbalance"].iloc[0] == pytest.approx(1/3)  # (2-1)/3
    assert out["large_trade_ratio"].iloc[0] == pytest.approx(0.5)  # (2+0)/4, edge >$50k
    assert out["price_impact"].iloc[0] == pytest.approx(0.25)      # (101-100)/4


def test_zero_volume_bar_yields_nan_not_inf():
    idx = pd.to_datetime(["2025-01-01 00:00"], utc=True)
    of = _of_df(idx, buy_vol=[0.0], sell_vol=[0.0], buy_count=[0], sell_count=[0],
               total_vol=[0.0], vol_lt10k=[0.0], vol_10_50k=[0.0],
               vol_50_200k=[0.0], vol_gt200k=[0.0], open=[100.0], close=[100.0])
    out = orderflow_factors(of, idx, "30m")
    assert np.isnan(out["trade_imbalance"].iloc[0])
    assert np.isnan(out["trade_count_imbalance"].iloc[0])
    assert np.isnan(out["large_trade_ratio"].iloc[0])
    assert not np.isinf(out["price_impact"].iloc[0])


def test_reindexes_to_candle_index():
    of_idx = pd.to_datetime(["2025-01-01 00:00"], utc=True)
    of = _of_df(of_idx, buy_vol=[2.0], sell_vol=[0.0], buy_count=[1], sell_count=[0],
               total_vol=[2.0], vol_lt10k=[2.0], vol_10_50k=[0.0],
               vol_50_200k=[0.0], vol_gt200k=[0.0], open=[100.0], close=[100.0])
    candle_idx = pd.to_datetime(
        ["2025-01-01 00:00", "2025-01-01 00:30"], utc=True
    )  # second bar absent in `of`
    out = orderflow_factors(of, candle_idx, "30m")
    assert list(out["trade_imbalance"].index) == list(candle_idx)
    assert np.isnan(out["trade_imbalance"].iloc[1])  # missing bar -> NaN, no ffill


def test_trade_count_imbalance_no_uint32_underflow():
    """Polars emits buy_count/sell_count as uint32; subtraction must not wrap."""
    import polars as pl
    from lib.orderflow import aggregate

    T0 = 1735689600000  # 2025-01-01T00:00:00Z
    MIN = 60_000
    trades = pl.DataFrame({
        "transact_time": [T0, T0 + MIN, T0 + 2 * MIN],
        "price": [100.0, 100.0, 100.0],
        "quantity": [1.0, 1.0, 1.0],
        "is_buyer_maker": [False, True, True],  # 1 buy, 2 sells
    })
    bars = aggregate(trades, "30m").to_pandas().set_index("ts")
    out = orderflow_factors(bars, bars.index, "30m")
    # trade_count_imbalance = (1 - 2) / (1 + 2) = -1/3
    # With uint32 underflow this would be ~4.3e9/3 ≈ 1.4e9 instead of -0.333
    val = out["trade_count_imbalance"].iloc[0]
    assert val == pytest.approx(-1 / 3, abs=1e-6), f"got {val!r} — likely uint32 underflow"
