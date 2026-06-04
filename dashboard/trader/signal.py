"""Live signal generator — same logic as backtest signal engine.

Loads ``code/signal_engine.py`` from a run directory, fetches recent OHLCV
via ccxt, and returns the latest signal value for the symbol.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from trader.freshness import factor_index_end, is_stale

FACTOR_MAX_AGE_DAYS = float(os.environ.get("FACTOR_MAX_AGE_DAYS", "2"))


@dataclass
class SignalResult:
    signal: int
    stale: bool
    index_end: "datetime | None"
    age_days: "float | None"


_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1h", "4H": "4h", "1D": "1d",
}


def _load_signal_engine(run_dir: Path):
    """Load SignalEngine class from run_dir/code/signal_engine.py."""
    signal_path = run_dir / "code" / "signal_engine.py"
    if not signal_path.exists():
        raise FileNotFoundError(f"signal_engine.py not found in {run_dir}/code/")
    spec = importlib.util.spec_from_file_location("_trader_signal_engine", signal_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine_cls = getattr(module, "SignalEngine", None)
    if engine_cls is None:
        raise AttributeError(f"SignalEngine class not found in {signal_path}")
    return engine_cls


def _fetch_ohlcv(
    exchange,
    symbol: str,
    interval: str,
    lookback: int,
) -> pd.DataFrame:
    """Fetch recent OHLCV bars from the exchange.

    Returns DataFrame with columns [open, high, low, close, volume],
    indexed by UTC timestamp.
    """
    ccxt_tf = _INTERVAL_MAP.get(interval)
    if ccxt_tf is None:
        raise ValueError(f"Unsupported interval: {interval}")

    raw = exchange.fetch_ohlcv(symbol, ccxt_tf, limit=lookback)
    if not raw:
        raise RuntimeError(f"No OHLCV data returned for {symbol}")

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df.index = df.index.tz_localize(None)  # strip tz for compatibility with backtest engine
    return df


def compute_signal(
    run_dir: Path,
    exchange,
    symbol: str,
    interval: str = "1H",
    lookback: int = 200,
    manifests_dir: "Path | None" = None,
    now: "datetime | None" = None,
) -> SignalResult:
    """Compute the current signal for *symbol* using the run's signal engine.

    When *manifests_dir* is given, the factor store's freshness is checked
    first. If the newest factor timestamp is older than ``FACTOR_MAX_AGE_DAYS``,
    the OHLCV fetch is skipped and a stale ``SignalResult`` (signal=0) is
    returned so the caller can pause instead of trading on frozen factors.
    """
    engine_cls = _load_signal_engine(run_dir)
    engine = engine_cls()

    engine_symbol = getattr(engine, "SYMBOL", symbol)

    now = now or datetime.now(tz=timezone.utc)
    index_end = factor_index_end(manifests_dir, engine_symbol) if manifests_dir else None
    age_days = (now - index_end).total_seconds() / 86400 if index_end else None

    if manifests_dir is not None and is_stale(
        index_end, now, timedelta(days=FACTOR_MAX_AGE_DAYS)
    ):
        return SignalResult(signal=0, stale=True, index_end=index_end, age_days=age_days)

    df = _fetch_ohlcv(exchange, symbol, interval, lookback)
    data_map = {engine_symbol: df}

    signal_map = engine.generate(data_map)
    series: Optional[pd.Series] = signal_map.get(engine_symbol)
    if series is None or series.empty:
        return SignalResult(signal=0, stale=False, index_end=index_end, age_days=age_days)

    raw = float(series.iloc[-1])
    if raw > 1e-9:
        sig = 1
    elif raw < -1e-9:
        sig = -1
    else:
        sig = 0
    return SignalResult(signal=sig, stale=False, index_end=index_end, age_days=age_days)
