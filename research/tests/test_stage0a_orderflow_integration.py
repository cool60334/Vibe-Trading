# research/tests/test_stage0a_orderflow_integration.py
import pandas as pd
import pytest

from pipeline.stage0a_features import build_feature_dict, compute_evidence_entries
from pipeline.config import load_config


def _candles(n=200):
    idx = pd.date_range("2025-01-01", periods=n, freq="30min", tz="UTC")
    price = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1,
         "close": price, "volume": 10.0}, index=idx
    )


def _orderflow_df(idx):
    # alternating imbalance so the factor has variance
    import numpy as np
    sign = np.where(np.arange(len(idx)) % 2 == 0, 1.0, -1.0)
    return pd.DataFrame(
        {
            "buy_vol": 5.0 + sign, "sell_vol": 5.0 - sign,
            "buy_count": 5, "sell_count": 5,
            "total_vol": 10.0,
            "vol_lt10k": 8.0, "vol_10_50k": 0.0,
            "vol_50_200k": 2.0, "vol_gt200k": 0.0,
            "open": 100.0, "close": 100.0 + sign,
        },
        index=idx,
    )


def test_orderflow_features_enter_feature_dict_and_get_ic():
    candles = _candles()
    of = _orderflow_df(candles.index)
    cfg = load_config()  # default config; interval label irrelevant for the dict
    feats = build_feature_dict(candles, cfg, orderflow_df=of)
    assert "trade_imbalance" in feats
    assert "price_impact" in feats

    entries = compute_evidence_entries(
        candles, feats, horizons_h=(1, 2, 4), interval="30m"
    )
    keys = {e["feature_key"] for e in entries}
    assert "trade_imbalance" in keys  # got IC-evaluated by the existing machinery
