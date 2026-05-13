"""
ccxt-based fetchers for data not exposed by OKX public endpoints.

Use cases:
- OI history (Bybit / Binance publish historical OI publicly via ccxt)
- Long/short ratio (some exchanges)
"""

from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd


def fetch_oi_history_bybit(symbol: str = "BTC/USDT:USDT", days: int = 90, timeframe: str = "1h") -> pd.DataFrame:
    """Fetch hourly historical open interest from Bybit via ccxt.

    Args:
        symbol: ccxt unified symbol for Bybit perpetual (e.g. "BTC/USDT:USDT")
        days: lookback
        timeframe: "5m" / "15m" / "30m" / "1h" / "4h" / "1d"

    Returns:
        DataFrame indexed by UTC time, columns: oi (contracts), oi_usd
    """
    exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    rows: list[dict] = []
    # ccxt's fetch_open_interest_history paginates by since + limit
    while True:
        data = exchange.fetch_open_interest_history(symbol, timeframe=timeframe, since=since, limit=200)
        if not data:
            break
        for d in data:
            rows.append(
                {
                    "time": datetime.fromtimestamp(d["timestamp"] / 1000, tz=timezone.utc),
                    "oi": float(d.get("openInterestAmount") or d.get("openInterestValue") or 0),
                    "oi_usd": float(d.get("openInterestValue") or 0),
                }
            )
        last_ts = data[-1]["timestamp"]
        if last_ts <= since:
            break
        since = last_ts + 1
        if datetime.fromtimestamp(since / 1000, tz=timezone.utc) >= datetime.now(timezone.utc):
            break

    if not rows:
        return pd.DataFrame(columns=["oi", "oi_usd"])
    df = pd.DataFrame(rows).drop_duplicates("time").set_index("time").sort_index()
    return df


def fetch_ohlcv_ccxt(exchange_name: str, symbol: str, days: int, timeframe: str = "1h") -> pd.DataFrame:
    """Generic OHLCV fetcher via ccxt (fallback for exchanges other than OKX)."""
    exchange = getattr(ccxt, exchange_name)({"enableRateLimit": True})
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[dict] = []
    while True:
        data = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not data:
            break
        for ts, o, h, l, c, v in data:
            rows.append(
                {
                    "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                }
            )
        last_ts = data[-1][0]
        if last_ts <= since:
            break
        since = last_ts + 1
        if datetime.fromtimestamp(since / 1000, tz=timezone.utc) >= datetime.now(timezone.utc):
            break
    df = pd.DataFrame(rows).drop_duplicates("time").set_index("time").sort_index()
    return df
