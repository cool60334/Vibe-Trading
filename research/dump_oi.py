"""Bulk-dump Binance futures daily OI metrics -> hourly parquet, per symbol.

Backfills multi-year open-interest + positioning factors from the free
data.binance.vision archive (the history the Bybit API's ~7-day retention could
never give). Idempotent: cached raw zips are skipped, so re-runs only fetch new
days. Missing upstream days are logged and skipped (NaN on the hourly grid).

    python dump_oi.py                                   # all 3 symbols, full history
    python dump_oi.py --symbols ETHUSDT --start 2025-01-01 --end 2025-02-01
    python dump_oi.py --no-verify                       # skip checksum (faster, less safe)

Outputs (gitignored runtime data):
    research/data/oi/oi_<SYMBOL>_1H.parquet   (+ .meta.json)
    research/data/oi/_raw/<SYMBOL>-metrics-*.zip   (raw cache)
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from lib import oi_metrics

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Earliest day the futures *metrics* archive publishes per symbol, verified
# against the data.binance.vision S3 listing (2026-06-17). Using too early a
# start just wastes 404s; too late silently drops history — so pin them.
EARLIEST = {
    "BTCUSDT": date(2020, 9, 1),
    "ETHUSDT": date(2021, 12, 1),
    "SOLUSDT": date(2021, 12, 1),
}
_FALLBACK_START = date(2021, 12, 1)

DEFAULT_CACHE = Path(__file__).resolve().parent / "data" / "oi"


def dump_symbol(
    symbol: str,
    start: date,
    end: date,
    cache_dir: Path,
    verify: bool = True,
) -> Path | None:
    """Download + aggregate + cache one symbol's hourly OI for ``[start, end]``."""
    cache_dir = Path(cache_dir)
    raw_dir = cache_dir / "_raw"
    print(f"[dump_oi] {symbol}: {start.isoformat()}..{end.isoformat()} ...")
    df = oi_metrics.load_oi_range(symbol, start, end, cache_dir=raw_dir, verify=verify)
    if df.empty:
        print(f"[dump_oi] {symbol}: no data in range — skipped")
        return None
    path = oi_metrics.dump_oi_parquet(symbol, df, cache_dir=cache_dir)
    nan_oi = int(df["oi"].isna().sum())
    print(
        f"[dump_oi] {symbol}: {len(df)} hourly rows "
        f"{df.index[0].date()}..{df.index[-1].date()} "
        f"({nan_oi} NaN-OI gap hours) -> {path.name}"
    )
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--start", type=date.fromisoformat, default=None,
                    help="ISO date; default = per-symbol archive earliest")
    ap.add_argument("--end", type=date.fromisoformat, default=None,
                    help="ISO date; default = today (UTC)")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip sha256 checksum verification")
    args = ap.parse_args(argv)

    end = args.end or datetime.now(timezone.utc).date()
    for sym in args.symbols:
        start = args.start or EARLIEST.get(sym, _FALLBACK_START)
        dump_symbol(sym, start, end, args.cache_dir, verify=not args.no_verify)


if __name__ == "__main__":
    main()
