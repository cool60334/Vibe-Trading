# research/tests/test_factor_gates.py
import numpy as np
import pandas as pd

from lib import factor_gates


def _idx(n):
    return pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")


def test_partial_ic_drops_to_zero_when_factor_is_a_control():
    idx = _idx(500)
    rng = np.random.default_rng(0)
    ctrl = pd.Series(rng.normal(size=500), index=idx)
    fwd = pd.Series(rng.normal(size=500), index=idx)
    factor = ctrl.copy()  # factor IS the control => no incremental info
    pic = factor_gates.partial_ic(factor, [ctrl], fwd)
    assert abs(pic) < 0.05


def test_partial_ic_survives_when_orthogonal_signal_present():
    idx = _idx(500)
    rng = np.random.default_rng(1)
    ctrl = pd.Series(rng.normal(size=500), index=idx)
    signal = pd.Series(rng.normal(size=500), index=idx)
    fwd = signal * 0.3 + pd.Series(rng.normal(size=500) * 0.1, index=idx)
    factor = signal + ctrl * 0.0  # carries orthogonal predictive signal
    pic = factor_gates.partial_ic(factor, [ctrl], fwd)
    assert abs(pic) > 0.1


def test_sign_flip_rate_high_for_alternating_series():
    idx = _idx(100)
    alt = pd.Series([1.0, -1.0] * 50, index=idx)
    assert factor_gates.sign_flip_rate(alt) > 0.9
    flat = pd.Series([1.0] * 100, index=idx)
    assert factor_gates.sign_flip_rate(flat) == 0.0


def test_decile_spread_net_of_cost():
    idx = _idx(400)
    rng = np.random.default_rng(2)
    factor = pd.Series(rng.normal(size=400), index=idx)
    fwd = factor * 0.05 + pd.Series(rng.normal(size=400) * 0.001, index=idx)  # strong monotone
    net = factor_gates.decile_spread_net_of_cost(factor, fwd, slippage_bps=7.5)
    assert net > 0  # gross spread beats round-trip cost
    # zero-signal factor => net should be negative after cost
    noise = pd.Series(rng.normal(size=400), index=idx)
    fwd_noise = pd.Series(rng.normal(size=400) * 0.0001, index=idx)
    assert factor_gates.decile_spread_net_of_cost(noise, fwd_noise, slippage_bps=7.5) < 0
