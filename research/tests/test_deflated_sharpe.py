"""Tests for the Deflated Sharpe Ratio lib (pure, no IO).

Run from repo root:  cd research && python -m pytest tests/test_deflated_sharpe.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.deflated_sharpe import bars_in_window, bars_per_year, deflated_sharpe  # noqa: E402


class TestBarsPerYear:
    def test_known_intervals(self):
        assert bars_per_year("1H") == 8760
        assert bars_per_year("30m") == 17520
        assert bars_per_year("15m") == 35040

    def test_unknown_defaults_to_hourly(self):
        assert bars_per_year("banana") == 8760


class TestBarsInWindow:
    def test_one_year_hourly(self):
        assert bars_in_window("2021-01-01", "2022-01-01", "1H") == 8760

    def test_handles_iso_datetime_prefix(self):
        assert bars_in_window("2021-01-01T00:00:00", "2022-01-01T00:00:00", "1H") == 8760

    def test_reversed_dates_clamp_to_zero(self):
        assert bars_in_window("2022-01-01", "2021-01-01", "1H") == 0


class TestDeflatedSharpe:
    def test_single_trial_returns_one(self):
        assert deflated_sharpe(0.1, [0.1], 1000) == 1.0

    def test_zero_variance_returns_one(self):
        assert deflated_sharpe(0.1, [0.05, 0.05, 0.05], 1000) == 1.0

    def test_short_sample_returns_one(self):
        assert deflated_sharpe(0.1, [0.01, 0.05, 0.2], 1) == 1.0

    def test_best_far_above_tight_cluster_is_high(self):
        dsr = deflated_sharpe(0.02, [0.001, 0.002, 0.0015, 0.0012], 8760)
        assert dsr > 0.95

    def test_best_at_top_of_wide_spread_is_haircut(self):
        trials = [-0.02, 0.02, -0.01, 0.015, 0.01, -0.015]
        dsr = deflated_sharpe(0.02, trials, 8760)
        assert dsr < 0.7

    def test_dsr_decreases_as_trials_grow(self):
        base = [0.005, -0.005, 0.003, -0.003]
        few = deflated_sharpe(0.02, base, 8760)
        many = deflated_sharpe(0.02, base * 25, 8760)  # 100 trials, same dispersion
        assert many < few

    def test_nonfinite_trials_dropped(self):
        clean = deflated_sharpe(0.02, [0.001, 0.002, 0.0015, 0.0012], 8760)
        dirty = deflated_sharpe(
            0.02, [0.001, 0.002, 0.0015, 0.0012, float("nan"), float("inf")], 8760
        )
        assert dirty == clean

    def test_known_numerical_value(self):
        import pytest
        # Pins the DSR formula coefficients against silent regression.
        # trials=[0.0, 0.01], best=0.01, T=100 (normal-returns assumption: skew=0, kurt=3)
        # var_srs = ddof-1 variance = 5e-5  →  std ≈ 0.007071
        # max_z ≈ 0.5198  →  expected_max_sr ≈ 0.003675
        # sr_std = sqrt((1 + 0.5*0.01^2) / 99) ≈ 0.10051
        # z ≈ 0.06293  →  dsr ≈ 0.5251
        dsr = deflated_sharpe(0.01, [0.0, 0.01], 100)
        assert dsr == pytest.approx(0.525, abs=0.005)
