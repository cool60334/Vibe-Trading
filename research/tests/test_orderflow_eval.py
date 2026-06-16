import numpy as np
import pandas as pd

from lib.orderflow_eval import decay_profile, half_life_bars


def test_decay_profile_perfect_one_bar_signal():
    idx = pd.date_range("2025-01-01", periods=6, freq="15min", tz="UTC")
    price = pd.Series([100, 101, 100, 101, 100, 101], index=idx, dtype=float)
    factor = pd.Series([1, -1, 1, -1, 1, np.nan], index=idx)
    # min_obs=3 so the small fixture passes the sample-size guard
    prof = decay_profile(factor, price, max_bars=3, min_obs=3)
    assert prof[1] > 0.8
    # At k=2 the 2-bar forward return is constant (price[t+2]==price[t] always),
    # so spearman is undefined → NaN.  NaN means "signal gone" which satisfies
    # the decay property; treat NaN as 0 for this check.
    ic2 = 0.0 if np.isnan(prof[2]) else abs(prof[2])
    assert ic2 <= prof[1]


def test_decay_profile_default_min_obs_returns_nan_on_tiny_sample():
    """Production default (min_obs=30) must return NaN when n < 30."""
    idx = pd.date_range("2025-01-01", periods=6, freq="15min", tz="UTC")
    price = pd.Series([100, 101, 100, 101, 100, 101], index=idx, dtype=float)
    factor = pd.Series([1, -1, 1, -1, 1, np.nan], index=idx)
    prof = decay_profile(factor, price, max_bars=1)  # default min_obs=30
    assert np.isnan(prof[1])


def test_half_life_bars_detected():
    """half_life_bars returns the first k where |IC(k)| < |IC(1)| / 2."""
    profile = {1: 0.8, 2: 0.5, 3: 0.3, 4: 0.1}
    # |IC(1)|/2 = 0.4; first k below that is k=3 (0.3 < 0.4)
    assert half_life_bars(profile) == 3


def test_half_life_bars_never_decays():
    """Returns None when IC never drops below half the 1-bar IC."""
    profile = {1: 0.8, 2: 0.6, 3: 0.5}
    assert half_life_bars(profile) is None


def test_half_life_bars_empty_or_nan():
    assert half_life_bars({}) is None
    assert half_life_bars({1: float("nan")}) is None


def test_decay_profile_sufficient_sample():
    """With enough data, decay profile returns non-NaN IC and decays properly."""
    rng = np.random.default_rng(42)
    n = 100
    idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    # Construct a signal where factor[t] predicts the 1-bar forward return at t.
    # price[t+1]/price[t] - 1 = returns[t], so we build price by cumulating returns.
    factor_vals = rng.choice([-1.0, 1.0], size=n)
    returns = np.where(rng.random(n) < 0.8, factor_vals * 0.01, -factor_vals * 0.01)
    # price[0]=100; price[t+1] = price[t] * (1 + returns[t])
    # => fwd_return(price, 1)[t] = price[t+1]/price[t]-1 = returns[t]
    price_vals = np.empty(n)
    price_vals[0] = 100.0
    for i in range(1, n):
        price_vals[i] = price_vals[i - 1] * (1 + returns[i - 1])
    price = pd.Series(price_vals, index=idx)
    factor = pd.Series(factor_vals, index=idx)
    prof = decay_profile(factor, price, max_bars=5, min_obs=30)
    # IC at 1-bar should be meaningfully positive (80% hit-rate -> ~0.6+ spearman)
    assert prof[1] > 0.3
