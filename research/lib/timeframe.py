"""Timeframe ↔ bar conversions for sub-hourly research runs.

Single source of truth for "how many bars in an hour/day" at a given candle
interval. Hour-anchored quantities (forward-return horizons in hours, the
30-day rolling windows on funding/stablecoin, the IC rolling step) multiply by
these to stay correct when the pipeline runs at 15m/30m instead of legacy 1H.
"""
from __future__ import annotations

import os
from pathlib import Path

_RESEARCH_DIR = Path(__file__).resolve().parent.parent  # research/lib/../ = research/
_MANIFESTS_BASE = _RESEARCH_DIR / "manifests"

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


def active_manifests_dir() -> Path:
    """Manifests directory for the active RESEARCH_INTERVAL.

    research/manifests for 1H or unset (zero 1H regression); research/manifests/<interval>
    for a supported sub-hour interval. Raises ValueError for an unsupported value.

    RESEARCH_MANIFESTS_DIR overrides the base directory (defaults to
    research/manifests). Set it to point factor/regime reads at a frozen fixture
    so backtests are hermetic and immune to stage1 rewriting the live parquet.
    The sub-hour interval namespace is still applied on top of the override.
    """
    base_override = os.environ.get("RESEARCH_MANIFESTS_DIR", "").strip()
    manifests_base = Path(base_override) if base_override else _MANIFESTS_BASE

    iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
    if not iv or iv == "1H":
        return manifests_base
    if iv not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"RESEARCH_INTERVAL={iv!r} is not supported; valid: {sorted(SUPPORTED_INTERVALS)}"
        )
    return manifests_base / iv
