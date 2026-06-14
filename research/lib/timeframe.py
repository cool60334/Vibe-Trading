"""Timeframe ↔ bar conversions for sub-hourly research runs.

Single source of truth for "how many bars in an hour/day" at a given candle
interval. Hour-anchored quantities (forward-return horizons in hours, the
30-day rolling windows on funding/stablecoin, the IC rolling step) multiply by
these to stay correct when the pipeline runs at 15m/30m instead of legacy 1H.
"""
from __future__ import annotations

# OKX bar strings → bars per hour. Keys match calc_bars_per_year's _BARS_PER_DAY
# table and the research_config.yaml `interval` field. All values are integers
# (sub-hour intervals divide an hour evenly), so hour→bar conversions never
# produce fractional bars.
_BARS_PER_HOUR: dict[str, int] = {"15m": 4, "30m": 2, "1H": 1}

SUPPORTED_INTERVALS: frozenset[str] = frozenset(_BARS_PER_HOUR)


def bars_per_hour(interval: str) -> int:
    """Number of candle bars in one hour at `interval`.

    Raises ValueError for any interval outside SUPPORTED_INTERVALS.
    """
    try:
        return _BARS_PER_HOUR[interval]
    except KeyError:
        raise ValueError(
            f"unsupported interval {interval!r}; supported: {sorted(SUPPORTED_INTERVALS)}"
        ) from None


def bars_per_day(interval: str) -> int:
    """Number of candle bars in one 24h day at `interval`."""
    return bars_per_hour(interval) * 24
