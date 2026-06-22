"""Parse Binance futures *daily metrics* dumps into hourly OI / positioning factors.

Source: data.binance.vision `futures/um/daily/metrics` — one zipped CSV per day,
5-minute snapshots. Free, no API key, ~4.5yr deep (BTC ~5.8yr). This is the
multi-year OI history the Bybit API (≈7-day retention) could never supply.

Offline + deterministic once the raw zips are cached (network lives only in
:mod:`lib.binance_dump`). Aggregation buckets are left-closed `[T, T+1h)` to
match the order-flow convention; entry-lag / look-ahead handling is downstream.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from lib import binance_dump

# Binance verbose column -> tidy factor name.
_RENAME = {
    "sum_open_interest": "oi",                              # OI in coin units
    "sum_open_interest_value": "oi_usd",                    # OI in USD
    "count_toptrader_long_short_ratio": "toptrader_ls_accounts",   # smart-money by accounts
    "sum_toptrader_long_short_ratio": "toptrader_ls_positions",    # smart-money by position size
    "count_long_short_ratio": "global_ls_accounts",        # crowd positioning
    "sum_taker_long_short_vol_ratio": "taker_buysell_ratio",  # taker buy/sell flow
}
_COLS = list(_RENAME.values())
OI_COLUMNS = tuple(_COLS)  # public, stable factor-column order

_CACHE_VERSION = 1

# Snapshots (stock) -> last value in the hour; flows -> mean over the hour.
_SNAPSHOT_COLS = (
    "oi",
    "oi_usd",
    "toptrader_ls_accounts",
    "toptrader_ls_positions",
    "global_ls_accounts",
)
_FLOW_COLS = ("taker_buysell_ratio",)


def parse_metrics_csv(text: str) -> pd.DataFrame:
    """One day's metrics CSV text -> tidy DataFrame indexed by UTC `time`."""
    df = pd.read_csv(io.StringIO(text))
    df["time"] = pd.to_datetime(df["create_time"], utc=True)
    df = df.set_index("time").rename(columns=_RENAME)
    return df[_COLS].astype("float64")


def read_metrics_zip(path: Path) -> pd.DataFrame:
    """Unzip a cached daily-metrics archive and parse its single CSV."""
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        text = z.read(name).decode()
    return parse_metrics_csv(text)


def aggregate_to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 5-minute snapshots to a continuous 1H grid.

    OI / long-short ratios are point-in-time stocks -> last snapshot in the bar.
    Taker buy/sell ratio is a per-window flow -> mean over the bar. Empty hours
    stay NaN (never forward-filled — ffill would inflate IC).
    """
    if df.empty:
        return df
    agg = {c: "last" for c in _SNAPSHOT_COLS if c in df.columns}
    agg.update({c: "mean" for c in _FLOW_COLS if c in df.columns})
    out = df.resample("1h", label="left", closed="left").agg(agg)
    return out[[c for c in _COLS if c in out.columns]]


def _daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def load_oi_range(
    symbol: str,
    start: date,
    end: date,
    cache_dir: Path,
    verify: bool = True,
) -> pd.DataFrame:
    """Download (idempotent), parse and aggregate metrics for ``[start, end]``.

    Missing upstream days (404 -> None) are skipped and logged; the resulting
    hourly grid stays continuous with NaN over the gap. Returns an empty frame
    (with the standard columns) if no day in the range is available.
    """
    frames: list[pd.DataFrame] = []
    gaps: list[date] = []
    for day in _daterange(start, end):
        zip_path = binance_dump.download_metrics_day(symbol, day, cache_dir, verify=verify)
        if zip_path is None:
            gaps.append(day)
            continue
        frames.append(read_metrics_zip(zip_path))

    if not frames:
        return pd.DataFrame(columns=_COLS)

    raw = pd.concat(frames).sort_index()
    raw = raw[~raw.index.duplicated(keep="last")]
    hourly = aggregate_to_hourly(raw)

    if gaps:
        print(
            f"[oi_metrics] {symbol}: {len(gaps)} missing day(s) skipped "
            f"({gaps[0].isoformat()}..{gaps[-1].isoformat()})"
        )
    return hourly


# ── parquet cache (aggregated hourly OI) ───────────────────────────────────


def _oi_cache_path(symbol: str, cache_dir: Path) -> Path:
    return Path(cache_dir) / f"oi_{symbol}_1H.parquet"


def dump_oi_parquet(symbol: str, df: pd.DataFrame, cache_dir: Path) -> Path:
    """Persist the aggregated hourly OI frame + sidecar meta.json. Returns the path."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _oi_cache_path(symbol, cache_dir)
    df.to_parquet(path, engine="pyarrow", compression="snappy")

    n = len(df)
    meta = {
        "version": _CACHE_VERSION,
        "symbol": symbol,
        "interval": "1H",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "columns": list(df.columns),
        "n_rows": n,
        "index_start": df.index.min().isoformat() if n else "",
        "index_end": df.index.max().isoformat() if n else "",
    }
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))
    return path


def load_oi_parquet(symbol: str, cache_dir: Path) -> pd.DataFrame:
    """Reload the cached hourly OI frame for ``symbol``."""
    path = _oi_cache_path(symbol, cache_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"OI parquet not found at '{path}'. Run dump_oi to build the cache."
        )
    return pd.read_parquet(path, engine="pyarrow")


# ── live L/S tail (freshness fix) ─────────────────────────────────────────

_LIVE_LS_COLS = ("global_ls_accounts", "toptrader_ls_positions")


def fetch_live_ls_ratios(symbol: str, period: str = "5m", limit: int = 500) -> pd.DataFrame:
    """Fetch the two live L/S ratios into a 5-min DataFrame [global_ls_accounts,
    toptrader_ls_positions], indexed by UTC timestamp."""
    paths = {
        "global_ls_accounts": binance_dump.GLOBAL_LS_ACCOUNT_PATH,
        "toptrader_ls_positions": binance_dump.TOPTRADER_LS_POSITION_PATH,
    }
    cols: dict[str, pd.Series] = {}
    for col, path in paths.items():
        rows = binance_dump.fetch_live_ls_raw(symbol, path, period, limit)
        s = pd.Series(
            {pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True): float(r["longShortRatio"])
             for r in rows}
        ).sort_index()
        cols[col] = s
    df = pd.DataFrame(cols)
    df.index.name = "time"
    return df


def merge_live_tail(archive_hourly: pd.DataFrame, live_5min: pd.DataFrame) -> pd.DataFrame:
    """Overlay the live L/S tail onto the archive hourly frame.

    Live 5-min is aggregated to 1H with the SAME convention as `aggregate_to_hourly`
    (snapshot=last). Only the two L/S columns are patched (live precedence on overlap,
    new hours appended); oi/oi_usd/taker keep their archive values (NaN on appended rows).
    """
    live_hourly = aggregate_to_hourly(live_5min)  # snapshot=last on the present cols
    out = archive_hourly.reindex(archive_hourly.index.union(live_hourly.index))
    for col in _LIVE_LS_COLS:
        if col in live_hourly.columns:
            out.loc[live_hourly.index, col] = live_hourly[col]
    return out


def reconcile_live_archive(
    archive: pd.DataFrame,
    live: pd.DataFrame,
    min_corr: float = 0.95,
    max_med_rel_err: float = 0.05,
    min_overlap: int = 12,
) -> None:
    """Raise ValueError unless each live L/S column matches archive in the overlap.

    Guards against a position-vs-account endpoint mix-up (the variants diverge far
    more than the tolerance) and unit/scale errors. Tolerance, not exact equality —
    the two series are computed independently. `live` is the 1H-aggregated tail.
    """
    overlap = archive.index.intersection(live.index)
    if len(overlap) < min_overlap:
        raise ValueError(f"reconcile: insufficient overlap ({len(overlap)} < {min_overlap} hrs)")
    for col in _LIVE_LS_COLS:
        a = archive.loc[overlap, col]
        b = live.loc[overlap, col]
        mask = a.notna() & b.notna()
        if int(mask.sum()) < min_overlap:
            raise ValueError(f"reconcile {col}: insufficient non-NaN overlap ({int(mask.sum())})")
        corr = float(a[mask].corr(b[mask]))
        med_rel = float(((b[mask] - a[mask]).abs() / a[mask].abs().replace(0, np.nan)).median())
        if corr < min_corr or med_rel > max_med_rel_err:
            raise ValueError(
                f"reconcile {col} fail: corr={corr:.3f} (min {min_corr}), "
                f"med_rel_err={med_rel:.3f} (max {max_med_rel_err})"
            )
