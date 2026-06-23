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
