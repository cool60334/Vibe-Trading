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
