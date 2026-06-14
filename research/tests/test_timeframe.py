"""Tests for research/lib/timeframe.py — run from research/ (pytest tests/)."""
import pytest

from lib.timeframe import SUPPORTED_INTERVALS, bars_per_day, bars_per_hour


def test_bars_per_hour_known_intervals():
    assert bars_per_hour("15m") == 4
    assert bars_per_hour("30m") == 2
    assert bars_per_hour("1H") == 1


def test_bars_per_day_known_intervals():
    assert bars_per_day("15m") == 96
    assert bars_per_day("30m") == 48
    assert bars_per_day("1H") == 24


def test_supported_intervals_set():
    assert SUPPORTED_INTERVALS == frozenset({"15m", "30m", "1H"})


def test_unsupported_interval_raises():
    with pytest.raises(ValueError, match="unsupported interval"):
        bars_per_hour("4H")
