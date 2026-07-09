"""Tests for research/lib/regime.py's ffill_regime_to look-ahead fix.

compute_regime/daily_close_from_hourly themselves need network-fetched OHLCV
warmup data and are not unit-tested here (see test_stage2_5_regime.py's
module docstring for that boundary). ffill_regime_to is pure and
deterministic, so it is fully covered here.
"""
from __future__ import annotations

import pandas as pd
import pytest

from research.lib.regime import ffill_regime_to


def test_ffill_regime_to_does_not_leak_label_into_its_own_day():
    # resample("1D").last() labels day-D with day-D's OWN end-of-day close,
    # which isn't knowable until ~D 23:00/D+1 00:00. A naive
    # `daily.reindex(hourly_index, method="ffill")` would assign day-D's
    # label starting at D 00:00 -- ~23 hours before it was knowable.
    daily = pd.Series(
        ["bull"] * 5 + ["bear"] * 5,
        index=pd.date_range("2024-01-01", periods=10, freq="1D"),
    )
    hourly_index = pd.date_range("2024-01-01", periods=240, freq="1h")  # 10 days
    out = ffill_regime_to(daily, hourly_index)

    # day-1 (01-01 00:00..23:00) is before the first label becomes knowable
    # (bull, from 01-01, only knowable from 01-02 00:00 onward) -- must be NaN,
    # not eagerly filled with "bull".
    assert out.iloc[:24].isna().all()

    # 01-02 00:00 is the first hour the 01-01 "bull" label is legitimately
    # knowable (shifted forward 1 day) -- and it holds through 01-06 23:00
    # (the last "bull" source day, 01-05, shifted to 01-06).
    assert out.iloc[24] == "bull"
    assert out.iloc[24 + 5 * 24 - 1] == "bull"          # 01-06 23:00, still bull
    assert out.iloc[24 + 5 * 24] == "bear"               # 01-07 00:00, now bear


def test_ffill_regime_to_matches_naive_ffill_shifted_by_exactly_one_day():
    # Sanity-check the fix is precisely a 1-day shift, not some other offset:
    # ffill_regime_to(daily, idx) == naive reindex+ffill of daily re-indexed
    # +1 day.
    daily = pd.Series(
        ["bull", "bear", "neutral", "bull", "bear"],
        index=pd.date_range("2024-03-01", periods=5, freq="1D"),
    )
    hourly_index = pd.date_range("2024-03-01", periods=200, freq="1h")
    actual = ffill_regime_to(daily, hourly_index)

    manual_shift = daily.copy()
    manual_shift.index = manual_shift.index + pd.Timedelta(days=1)
    expected = manual_shift.reindex(hourly_index, method="ffill")

    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_ffill_regime_to_leaves_source_series_untouched():
    daily = pd.Series(["bull", "bear"], index=pd.date_range("2024-01-01", periods=2, freq="1D"))
    original_index = daily.index.copy()
    ffill_regime_to(daily, pd.date_range("2024-01-01", periods=48, freq="1h"))
    assert daily.index.equals(original_index)
