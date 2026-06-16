# research/lib/orderflow.py
"""Aggregate Binance aggTrades into per-bar order-flow primitives (Polars).

Offline + deterministic. Bucketing is [T, T+interval) left-closed/right-open
(no look-ahead). is_buyer_maker semantics: False => aggressive BUY (taker is
buyer, fill at ask); True => aggressive SELL (taker is seller, fill at bid).
"""
from __future__ import annotations

import hashlib as _hashlib
import inspect as _inspect
import json as _json
import subprocess as _subprocess
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

# USD-notional bucket edges (single-pass, look-ahead-safe). "Large trade" is
# composed downstream by summing buckets above a chosen edge (Task 5), so the
# threshold re-tunes without re-scanning raw.
BUCKET_EDGES_USD = (10_000.0, 50_000.0, 200_000.0)

_INTERVAL_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1H": 60 * 60_000}


def aggregate(trades: pl.DataFrame, interval: str) -> pl.DataFrame:
    """raw aggTrades -> per-bar primitives. `trades` needs columns
    transact_time (ms), price, quantity, is_buyer_maker (+ optional agg_trade_id)."""
    if interval not in _INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}; supported: {list(_INTERVAL_MS)}")
    step = _INTERVAL_MS[interval]
    df = trades.sort("transact_time")  # ensure "first" in dedup = earliest by time
    if "agg_trade_id" in df.columns:
        df = df.unique(subset=["agg_trade_id"], keep="first", maintain_order=True)

    e1, e2, e3 = BUCKET_EDGES_USD
    df = df.with_columns(
        ((pl.col("transact_time") // step) * step).alias("_bar_ms"),
        (~pl.col("is_buyer_maker")).alias("_is_buy"),
        (pl.col("price") * pl.col("quantity")).alias("_usd"),
    )

    out = (
        df.group_by("_bar_ms")
        .agg(
            pl.col("quantity").filter(pl.col("_is_buy")).sum().alias("buy_vol"),
            pl.col("quantity").filter(~pl.col("_is_buy")).sum().alias("sell_vol"),
            pl.col("_is_buy").filter(pl.col("_is_buy")).len().alias("buy_count"),
            pl.col("_is_buy").filter(~pl.col("_is_buy")).len().alias("sell_count"),
            pl.col("quantity").sum().alias("total_vol"),
            pl.col("price").first().alias("open"),
            pl.col("price").last().alias("close"),
            pl.col("quantity").filter(pl.col("_usd") < e1).sum().alias("vol_lt10k"),
            pl.col("quantity").filter((pl.col("_usd") >= e1) & (pl.col("_usd") < e2)).sum().alias("vol_10_50k"),
            pl.col("quantity").filter((pl.col("_usd") >= e2) & (pl.col("_usd") < e3)).sum().alias("vol_50_200k"),
            pl.col("quantity").filter(pl.col("_usd") >= e3).sum().alias("vol_gt200k"),
        )
        .with_columns(
            pl.col("_bar_ms").cast(pl.Datetime("ms")).dt.replace_time_zone("UTC").alias("ts"),
            *[pl.col(c).fill_null(0.0) for c in
              ("buy_vol", "sell_vol", "vol_lt10k", "vol_10_50k", "vol_50_200k", "vol_gt200k")],
        )
        .drop("_bar_ms")
        .sort("ts")
    )
    return out


AGG_VERSION = 1


def _git_sha() -> str:
    try:
        return _subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, text=True
        ).strip()
    except Exception:
        return "unknown"


def _logic_hash() -> str:
    return _hashlib.sha256(_inspect.getsource(aggregate).encode()).hexdigest()[:16]


def _cache_path(symbol: str, interval: str, dest_dir: Path) -> Path:
    return Path(dest_dir) / f"of_{symbol}_{interval}_v{AGG_VERSION}.parquet"


def write_cache(df: pl.DataFrame, symbol: str, interval: str, dest_dir: Path,
                months: list | None = None, raw_checksums: dict | None = None) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path(symbol, interval, dest_dir)
    df.write_parquet(p)
    meta = {
        "version": AGG_VERSION,
        "git_sha": _git_sha(),
        "logic_hash": _logic_hash(),
        "interval": interval,
        "symbol": symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months": months or [],
        "raw_checksums": raw_checksums or {},
        "factor_columns": [c for c in df.columns if c != "ts"],
    }
    p.with_suffix(".meta.json").write_text(_json.dumps(meta, indent=2))
    return p


def read_cache(symbol: str, interval: str, dest_dir: Path) -> pl.DataFrame:
    p = _cache_path(symbol, interval, dest_dir)
    meta_path = p.with_suffix(".meta.json")
    if not meta_path.exists():
        raise ValueError(f"cache meta missing for {p.name} — re-run aggregation")
    meta = _json.loads(meta_path.read_text())
    if meta.get("version") != AGG_VERSION:
        raise ValueError(
            f"cache version mismatch for {p.name}: meta={meta.get('version')} "
            f"expected={AGG_VERSION} — re-run aggregation"
        )
    return pl.read_parquet(p)
