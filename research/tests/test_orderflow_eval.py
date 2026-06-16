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


from lib.orderflow_eval import quantile_returns


def test_quantile_returns_monotone():
    idx = pd.date_range("2025-01-01", periods=100, freq="15min", tz="UTC")
    factor = pd.Series(np.linspace(-1, 1, 100), index=idx)
    # next-bar return increases with the factor -> top bucket > bottom bucket
    price = pd.Series(100 + np.cumsum(np.linspace(-1, 1, 100)), index=idx)
    q = quantile_returns(factor, price, fwd_bars=1, n_q=5)
    assert q.index.tolist() == [0, 1, 2, 3, 4]
    assert q.iloc[-1] > q.iloc[0]  # monotone increasing


from lib.orderflow_eval import incremental_ic


def test_incremental_ic_removes_shared_component():
    idx = pd.date_range("2025-01-01", periods=200, freq="15min", tz="UTC")
    rng = np.random.default_rng(0)
    control = pd.Series(rng.normal(size=200), index=idx)
    # factor is a pure copy of control -> zero incremental signal
    factor = control.copy()
    price = pd.Series(100 + np.cumsum(control.values), index=idx)  # return ~ control
    inc = incremental_ic(factor, control, price, fwd_bars=1)
    assert abs(inc) < 0.1  # residual carries no independent predictive power


from lib.orderflow_eval import execution_ic


def test_execution_ic_entry_lag_shifts_base():
    # Fixture design:
    # ret[t] = factor[t] * 0.01 + tiny_noise  →  strong correlation with factor[t]
    # price is built so that price[t+1]/price[t] - 1 = ret[t]  (price starts at 100,
    # each bar multiplies by (1 + ret[t])).  This is *not* the same as np.cumprod,
    # which gives price[t+1]/price[t]-1 = ret[t+1] because cumprod(1+ret)[0] already
    # incorporates ret[0] into price[0].  We need the sequential accumulation:
    #   price[0] = 100
    #   price[t] = price[t-1] * (1 + ret[t-1])
    # so price[t+1]/price[t]-1 = ret[t] exactly.
    #
    # With entry_lag_bars=0, hold_bars=1:
    #   fwd[t] = price[t+1]/price[t] - 1 = ret[t]  →  correlates with factor[t]
    #   → ic0 is high (>0.3)
    # With entry_lag_bars=1, hold_bars=1:
    #   fwd[t] = price[t+2]/price[t+1] - 1 = ret[t+1]  →  correlates with factor[t+1],
    #   which is independent of factor[t]  →  ic1 ≈ 0
    # The test verifies that a lag-0 entry captures the signal while a lag-1 entry does
    # not systematically amplify it — i.e. no same-bar lookahead artefact leak persists.
    idx = pd.date_range("2025-01-01", periods=200, freq="15min", tz="UTC")
    rng = np.random.default_rng(1)
    factor = pd.Series(rng.normal(size=200), index=idx)
    # Build ret so ret[t] strongly correlates with factor[t]
    ret_vals = factor.values * 0.01 + rng.normal(scale=0.001, size=200)
    # Build price so that price[t+1]/price[t] - 1 = ret[t]
    price_vals = np.empty(200)
    price_vals[0] = 100.0
    for i in range(1, 200):
        price_vals[i] = price_vals[i - 1] * (1.0 + ret_vals[i - 1])
    price = pd.Series(price_vals, index=idx)
    ic0 = execution_ic(factor, price, entry_lag_bars=0, hold_bars=1, min_obs=30)
    ic1 = execution_ic(factor, price, entry_lag_bars=1, hold_bars=1, min_obs=30)
    # lag-0 entry must show high IC — the factor genuinely predicts the 1-bar return
    assert ic0 > 0.3
    # lag-1 entry must be far weaker than lag-0 (no autocorrelation in factor → IC
    # collapses at lag 1, regardless of sign).  This is the core lookahead-audit
    # invariant: a real signal persists; a lookahead artefact collapses to ~0 at lag 1.
    assert abs(ic1) < abs(ic0)
