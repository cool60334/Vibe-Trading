import numpy as np
import pandas as pd
import pytest
from research.hermes.pit import assert_no_lookahead, LookaheadError


def _panel(n=300, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    close = 100.0 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame({"close": close, "volume": rng.random(n) * 1000}, index=idx)


def test_causal_feature_passes():
    df = _panel()
    # 5-bar momentum: only past+current → causal, must pass
    assert_no_lookahead(lambda d: d["close"].pct_change(5), df) is None


def test_bfill_leak_is_caught():
    df = _panel()
    # bfill pulls FUTURE close into present NaN → must raise
    def leaky(d):
        s = d["close"].copy()
        s.iloc[100] = np.nan
        return s.bfill()
    with pytest.raises(LookaheadError):
        assert_no_lookahead(leaky, df)


def test_negative_shift_leak_is_caught():
    df = _panel()
    with pytest.raises(LookaheadError):
        assert_no_lookahead(lambda d: d["close"].shift(-1), df)  # tomorrow's close
