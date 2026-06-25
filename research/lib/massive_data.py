# research/lib/massive_data.py
"""Massive (Polygon.io) crypto USD aggregate-bar fetcher.

Only USD spot aggregate bars (1H by default) are needed for the cross-venue
premium factor. No tick / quote / WebSocket here (see design spec §2 non-goals).
Failure (missing key, no coverage, empty result) returns None so callers can
skip the factor without breaking the 1H main line.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

#: API host. Polygon endpoints remain valid post-Massive-rebrand; override here
#: in one place if the host moves to api.massive.com.
BASE_URL = "https://api.polygon.io"


def _interval_to_mult_timespan(interval: str) -> tuple[int, str]:
    """Map a candle string ('1H','30m','15m') to (multiplier, timespan)."""
    s = interval.strip().lower()
    if s.endswith("h"):
        return int(s[:-1] or "1"), "hour"
    if s.endswith("m"):
        return int(s[:-1]), "minute"
    if s.endswith("d"):
        return int(s[:-1] or "1"), "day"
    raise ValueError(f"unsupported interval: {interval!r}")


def fetch_spot_bars(
    ticker: str,
    days: int,
    interval: str = "1H",
    *,
    max_retries: int = 3,
) -> pd.DataFrame | None:
    """Fetch Massive USD aggregate bars for `ticker` (e.g. 'X:BTCUSD').

    Returns a DataFrame indexed by UTC DatetimeIndex with OHLCV columns
    (at least 'close'), or None on missing key / no coverage / empty result.
    """
    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        return None
    mult, timespan = _interval_to_mult_timespan(interval)
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    to = now.strftime("%Y-%m-%d")
    url = (
        f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/{mult}/{timespan}/{frm}/{to}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}

    rows: list[dict] = []
    next_url: str | None = url
    next_params: dict | None = params
    while next_url:
        resp = None
        for attempt in range(max_retries):
            resp = requests.get(next_url, params=next_params, timeout=30)
            if getattr(resp, "status_code", 200) == 429:
                time.sleep(2 ** attempt)
                continue
            break
        if resp is None or getattr(resp, "status_code", 200) == 429:
            break
        resp.raise_for_status()
        body = resp.json()
        rows.extend(body.get("results") or [])
        nxt = body.get("next_url")
        if nxt:
            next_url, next_params = nxt, {"apiKey": key}  # next_url carries the cursor
        else:
            next_url = None

    if not rows:
        return None
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["open", "high", "low", "close", "volume"]].sort_index()
