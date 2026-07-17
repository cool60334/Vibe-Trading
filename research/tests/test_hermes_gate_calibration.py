"""Gate calibration controls. No LLM, no cost -- these run in CI.

The gate's blind spot lived for the project's whole life and was found by
accident. These tests exist so it cannot come back quietly."""
import numpy as np
import pandas as pd
import pytest

from research.hermes.calibration import PRIME_SHIFT_DAYS, circular_shift, plant_alpha


def test_prime_shifts_avoid_market_periodicity():
    """+90 days -- one quarter -- was the first choice and the worst one: crypto
    has quarterly expiry and funding cycles, so shifting by a whole quarter can
    ALIGN a control with the very periodicity it is meant to destroy."""
    def is_prime(n):
        return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))
    for d in PRIME_SHIFT_DAYS:
        assert is_prime(d), f"{d} is not prime"
        assert 365 % d != 0, f"{d} divides a year"
        assert d > 90, f"{d} is not clear of the longest rolling window (30d)"
        for period in (90, 180, 270, 365):
            assert abs(d - period) > 15, f"{d} sits on the {period}d cycle"


def test_circular_shift_preserves_length_and_values():
    idx = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    s = pd.Series(np.arange(100.0), index=idx)
    out = circular_shift(s, days=1, bars_per_day=24)
    assert len(out) == len(s)
    assert out.index.equals(s.index)
    assert sorted(out.dropna().tolist()) == sorted(s.tolist())   # wrapped, not dropped


def test_circular_shift_actually_moves_the_series():
    idx = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    s = pd.Series(np.arange(100.0), index=idx)
    out = circular_shift(s, days=1, bars_per_day=24)
    assert out.iloc[24] == 0.0                     # first value moved 24 bars on
    assert not out.equals(s)


def test_circular_shift_preserves_autocorrelation():
    """This is the whole point of shifting rather than shuffling: a shuffled
    control loses its autocorrelation, its turnover explodes, and it degenerates
    into the too-clean noise this design exists to avoid."""
    rng = np.random.default_rng(0)
    idx = pd.date_range("2024-01-01", periods=5000, freq="h", tz="UTC")
    s = pd.Series(rng.standard_normal(5000), index=idx).rolling(48).mean()
    out = circular_shift(s, days=101)
    assert out.autocorr(1) == pytest.approx(s.autocorr(1), abs=0.02)


def test_plant_alpha_strength_rises_with_w():
    idx = pd.date_range("2024-01-01", periods=3000, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    fwd = pd.Series(rng.standard_normal(3000) * 0.01, index=idx)
    weak = plant_alpha(fwd, w=0.05).corr(fwd.shift(-1), method="spearman")
    strong = plant_alpha(fwd, w=0.50).corr(fwd.shift(-1), method="spearman")
    assert abs(strong) > abs(weak)


def test_plant_alpha_is_smooth_enough_to_be_tradeable():
    """The signal term z(fwd.shift(-1)) is nearly white. Smoothing must be applied
    to the WHOLE blend, not just the noise -- otherwise turnover explodes and the
    positive control measures the turnover gate instead of DSR."""
    from research.hermes.gatekeeper import factor_to_weights, turnover_of
    idx = pd.date_range("2024-01-01", periods=5000, freq="h", tz="UTC")
    rng = np.random.default_rng(2)
    fwd = pd.Series(rng.standard_normal(5000) * 0.01, index=idx)
    for w in (0.05, 0.5):
        f = plant_alpha(fwd, w=w)
        to = float(turnover_of(factor_to_weights(f)).fillna(0.0).mean())
        assert to < 0.5, f"w={w} turnover {to:.3f} would hit the physical ceiling"


def test_plant_alpha_is_deterministic():
    idx = pd.date_range("2024-01-01", periods=1000, freq="h", tz="UTC")
    fwd = pd.Series(np.linspace(-0.01, 0.01, 1000), index=idx)
    pd.testing.assert_series_equal(plant_alpha(fwd, 0.2, seed=7), plant_alpha(fwd, 0.2, seed=7))
