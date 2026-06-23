"""Coin feasibility scout: probe free data-source coverage before onboarding a
coin into the research pipeline.

Probes three sources the pipeline depends on — OKX OHLCV, OKX funding, and the
Binance daily-metrics archive (OI + positioning factors) — for earliest date and
depth, then scores each coin GO / PARTIAL / NO_GO. Read-only and cheap (existence
+ earliest-date only). Network lives in the ``probe_*`` functions (which call
``lib.okx_data`` / ``lib.binance_dump``); scoring and serialization are pure.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

from lib import binance_dump, okx_data

# ── thresholds (module constants; tune after the first live run) ─────────────
MIN_OHLCV_DAYS = 365       # OKX OHLCV depth below this → NO_GO (can't even backtest)
TARGET_DEPTH_DAYS = 730    # all three sources ≥ this → GO
CONFIG_PERIOD_DAYS = 1460  # informational: existing coins carry ~4yr history

# Binance USDT-perp futures metrics archive does not predate this.
_ARCHIVE_FLOOR = date(2020, 1, 1)


@dataclasses.dataclass(frozen=True)
class SourceCoverage:
    """Coverage of one data source for one coin. `available` False ⇒ data absent
    or probe failed (see `error`); `earliest`/`depth_days` are then None."""
    available: bool
    earliest: date | None
    depth_days: int | None
    error: str | None = None


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def tickers_for(name: str) -> tuple[str, str, str]:
    """(okx_swap, ccxt_bybit, binance_usdt) derived from a coin short name.

    Matches research.pipeline.config.SymbolConfig conventions.
    """
    up = name.upper()
    return f"{up}-USDT-SWAP", f"{up}/USDT:USDT", f"{up}USDT"


def score_coin(
    ohlcv: SourceCoverage,
    funding: SourceCoverage,
    archive: SourceCoverage,
    min_ohlcv_days: int = MIN_OHLCV_DAYS,
    target_days: int = TARGET_DEPTH_DAYS,
) -> tuple[str, list[str]]:
    """Pure verdict: GO / PARTIAL / NO_GO + human reasons.

    NO_GO   : OKX OHLCV missing or shallower than `min_ohlcv_days`.
    GO      : all three sources available and depth ≥ `target_days`.
    PARTIAL : runnable but a source is missing/shallow (archive gap = no
              positioning factors, the proven edge).
    """
    reasons: list[str] = []
    o_depth = ohlcv.depth_days or 0

    if not ohlcv.available:
        return "NO_GO", ["okx_ohlcv unavailable"]
    if o_depth < min_ohlcv_days:
        return "NO_GO", [f"okx_ohlcv depth {o_depth}d < floor {min_ohlcv_days}d"]

    f_ok = funding.available and (funding.depth_days or 0) >= target_days
    a_ok = archive.available and (archive.depth_days or 0) >= target_days
    o_ok = o_depth >= target_days

    if o_ok and f_ok and a_ok:
        return "GO", [
            f"all sources >= {target_days}d "
            f"(ohlcv {o_depth}d, funding {funding.depth_days}d, archive {archive.depth_days}d)"
        ]

    if not archive.available:
        reasons.append("binance_archive missing -> no positioning factors")
    elif (archive.depth_days or 0) < target_days:
        reasons.append(f"binance_archive depth {archive.depth_days}d < target {target_days}d")
    if not funding.available:
        reasons.append("okx_funding missing")
    elif (funding.depth_days or 0) < target_days:
        reasons.append(f"okx_funding depth {funding.depth_days}d < target {target_days}d")
    if not o_ok:
        reasons.append(f"okx_ohlcv depth {o_depth}d < target {target_days}d")
    return "PARTIAL", reasons


def _earliest_archive_day(
    symbol: str,
    *,
    floor: date = _ARCHIVE_FLOOR,
    today: date | None = None,
    exists=None,
) -> date | None:
    """Earliest day with an available daily-metrics zip, via binary search.

    `exists(symbol, day) -> bool` is injected for testing (defaults to
    binance_dump.metrics_day_exists). Upper bound is `today - 2d` to dodge the
    T+1 unpublished tail; a short tail soft-gap is skipped by stepping back up to
    7 days to find an anchor that exists. Returns None if no day in range exists.

    A rare mid-range soft gap can over-estimate `earliest` by a few days — fine
    for a feasibility scout (depth is a coverage signal, not an exact count).
    """
    exists = exists or binance_dump.metrics_day_exists
    hi = (today or _utc_today()) - timedelta(days=2)
    lo = floor
    if hi < lo:
        return None

    # Anchor: first existing day at/just-before hi (skip a short tail gap).
    anchor = None
    probe = hi
    for _ in range(7):
        if probe < lo:
            break
        if exists(symbol, probe):
            anchor = probe
            break
        probe -= timedelta(days=1)
    if anchor is None:
        return None

    # Bisect [lo, anchor] for the smallest day where exists() is True.
    hi = anchor
    while lo < hi:
        mid = lo + (hi - lo) // 2
        if exists(symbol, mid):
            hi = mid
        else:
            lo = mid + timedelta(days=1)
    return lo


def _coverage_from_index(idx) -> SourceCoverage:
    """Build a SourceCoverage from a non-empty UTC DatetimeIndex."""
    earliest = idx.min().date()
    return SourceCoverage(
        available=True,
        earliest=earliest,
        depth_days=(_utc_today() - earliest).days,
        error=None,
    )


def probe_okx_ohlcv(okx_swap: str, lookback_days: int = 2000) -> SourceCoverage:
    """Earliest available 1H candle for `okx_swap` (deep history request)."""
    try:
        df = okx_data.fetch_candles(okx_swap, days=lookback_days, bar="1H")
    except Exception as exc:  # network / okx code!=0 / unknown instId — never crash the run
        return SourceCoverage(False, None, None, str(exc)[:200])
    if df is None or df.empty:
        return SourceCoverage(False, None, None, None)
    return _coverage_from_index(df.index)


def probe_okx_funding(okx_swap: str, lookback_days: int = 2000) -> SourceCoverage:
    """Earliest available funding rate for `okx_swap`."""
    try:
        df = okx_data.fetch_funding_history(okx_swap, lookback_days)
    except Exception as exc:
        return SourceCoverage(False, None, None, str(exc)[:200])
    if df is None or df.empty:
        return SourceCoverage(False, None, None, None)
    return _coverage_from_index(df.index)


def probe_binance_archive(binance_usdt: str) -> SourceCoverage:
    """Earliest available daily-metrics archive day for `binance_usdt`."""
    try:
        earliest = _earliest_archive_day(binance_usdt)
    except Exception as exc:
        return SourceCoverage(False, None, None, str(exc)[:200])
    if earliest is None:
        return SourceCoverage(False, None, None, None)
    return SourceCoverage(
        available=True,
        earliest=earliest,
        depth_days=(_utc_today() - earliest).days,
        error=None,
    )
