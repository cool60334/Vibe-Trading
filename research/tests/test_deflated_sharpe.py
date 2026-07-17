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

    def test_n_trials_defaults_to_the_sample_count(self):
        """不給 n_trials 時行為完全不變（向後相容）。"""
        srs = [0.01, -0.01, 0.02, -0.02, 0.005]
        assert deflated_sharpe(0.03, srs, T=20000) == deflated_sharpe(
            0.03, srs, T=20000, n_trials=len(srs))

    def test_more_trials_is_stricter_at_the_same_variance(self):
        """N 是多重測試債：試越多次，同一個 SR 越不顯著。
        這是拆開參數的全部理由 —— 債務要能獨立於變異數樣本累計。"""
        srs = [0.01, -0.01, 0.02, -0.02, 0.005]
        few = deflated_sharpe(0.03, srs, T=20000, n_trials=5)
        many = deflated_sharpe(0.03, srs, T=20000, n_trials=500)
        assert many < few

    def test_variance_still_comes_from_the_series_not_from_n_trials(self):
        """n_trials 只影響 max_z，不得影響變異數估計。"""
        tight = [0.001, -0.001, 0.002, -0.002]
        wide = [0.10, -0.10, 0.20, -0.20]
        assert deflated_sharpe(0.03, tight, T=20000, n_trials=50) > \
               deflated_sharpe(0.03, wide, T=20000, n_trials=50)

    def test_n_trials_below_two_is_undefined_and_never_blocks(self):
        assert deflated_sharpe(0.03, [0.01, -0.01], T=20000, n_trials=1) == 1.0

    def test_n_trials_can_exceed_the_variance_sample_count(self):
        """真實用途：39 筆舊 trial 的『次數』要接續，但它們的 net SR 不進變異數。

        Proof: if two series have the same variance, and we call deflated_sharpe
        with the same n_trials on both, the results must be identical. This proves
        that sample length doesn't leak into n_trials' effect — only variance does.
        """
        import pytest
        import numpy as np

        # Two series with equal variance but different lengths
        # (3 elements vs 6 elements). Constructed so that np.var(..., ddof=1)
        # produces identical values to machine precision.
        s1 = np.array([0.01, -0.01, 0.02])
        s2 = np.array([0.01, -0.01, 0.02, -0.02, 0.0091287093, -0.0091287093])

        # Verify they have equal variance (within machine precision, ~1e-10)
        v1 = np.var(s1, ddof=1)
        v2 = np.var(s2, ddof=1)
        assert v1 == pytest.approx(v2, abs=1e-10)

        # With equal variance and equal n_trials, results must be identical
        # This demonstrates independence: n_trials drives the debt independently
        # of how many values are in the variance sample
        r1 = deflated_sharpe(0.03, s1, T=20000, n_trials=42)
        r2 = deflated_sharpe(0.03, s2, T=20000, n_trials=42)
        # Tolerances account for floating-point error in variance & formula
        assert r1 == pytest.approx(r2, abs=1e-8, rel=1e-7)

        # Also verify the original weak assertion still holds
        assert 0.0 <= r1 <= 1.0

    def test_n_trials_must_be_positive(self):
        import pytest
        with pytest.raises(ValueError, match="n_trials"):
            deflated_sharpe(0.03, [0.01, -0.01], T=20000, n_trials=0)
