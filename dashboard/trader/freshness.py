"""Factor-store freshness helpers for the live trader.

Pure functions — no network, no ccxt. Used by the signal guard to refuse
trading when the factor parquet is too old (e.g. the refresh cron broke).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


def _symbol_short(symbol: str) -> str:
    """Normalize a trading/canonical symbol to the factor-file short form.

    "ETH-USDT-SWAP" -> "eth", "ETH/USDT:USDT" -> "eth", "eth" -> "eth".
    """
    s = symbol.strip()
    if "-" in s:
        return s.split("-")[0].lower()
    if "/" in s:
        return s.split("/")[0].lower()
    return s.lower()


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def factor_index_end(manifests_dir: Path, symbol: str) -> Optional[datetime]:
    """Return the newest factor timestamp for *symbol*, or None if unavailable.

    Prefers ``factor_values_<short>.meta.json`` ``index_end``; falls back to the
    parquet index max if no meta is present.
    """
    short = _symbol_short(symbol)
    meta_path = manifests_dir / f"factor_values_{short}.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = meta.get("index_end")
        if raw:
            return _to_utc(datetime.fromisoformat(raw))

    parquet_path = manifests_dir / f"factor_values_{short}.parquet"
    if parquet_path.exists():
        df = pd.read_parquet(parquet_path, engine="pyarrow")
        if len(df) > 0:
            ts = pd.Timestamp(df.index.max()).to_pydatetime()
            return _to_utc(ts)

    return None


def is_stale(
    index_end: Optional[datetime],
    now: datetime,
    max_age: timedelta,
) -> bool:
    """True if the factor data is missing or older than *max_age*.

    Exactly *max_age* old is NOT stale (strict greater-than triggers).
    """
    if index_end is None:
        return True
    return (_to_utc(now) - index_end) > max_age


def refresh_generated_at(manifests_dir: Path, symbol: str) -> Optional[datetime]:
    """Return the factor meta's ``generated_at`` (when the refresh cron last wrote
    the store), or None. Distinct from ``factor_index_end`` (the data's own age):
    the archive is ~1 day lagged *by nature*, so cron health is judged by how
    recently the parquet was REWRITTEN, not by how old the newest bar is.
    """
    short = _symbol_short(symbol)
    meta_path = manifests_dir / f"factor_values_{short}.meta.json"
    if not meta_path.exists():
        return None
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8")).get("generated_at")
    except (OSError, json.JSONDecodeError):
        return None
    return _to_utc(datetime.fromisoformat(raw)) if raw else None


def refresh_is_stalled(
    generated_at: Optional[datetime], now: datetime, max_age: timedelta,
) -> bool:
    """True if the refresh is missing or older than *max_age* (cron likely broke)."""
    if generated_at is None:
        return True
    return (_to_utc(now) - generated_at) > max_age
