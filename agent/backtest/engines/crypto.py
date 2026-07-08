"""Crypto perpetual-contract backtest engine.

Market rules:
  - 24/7 trading, no restrictions on direction
  - Maker/Taker fee separation
  - Funding fee settlement every 8 hours (00:00/08:00/16:00 UTC)
  - Forced liquidation when maintenance margin ratio <= 100%
  - Fractional position sizes allowed
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtest.engines.base import BaseEngine
from backtest.engines._market_hooks import (
    calc_crypto_funding_fee,
    check_crypto_liquidation,
)


class CryptoEngine(BaseEngine):
    """Crypto perpetual contract engine.

    Config keys:
      - leverage: default 1.0
      - maker_rate: default 0.0002
      - taker_rate: default 0.0005
      - slippage: default 0.0005
      - margin_mode: "isolated" (default) or "cross"
      - funding_rate: fixed rate per settlement, default 0.0001
      - funding_series_path: optional parquet path with a "funding_rate_raw"
        column, timestamp-indexed; when set, overrides funding_rate with a
        real PIT-safe per-settlement lookup (raises on missing/NaN gaps)
      - taker_both_legs: charge taker rate on close leg too, default False
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.maker_rate: float = config.get("maker_rate", 0.0002)
        self.taker_rate: float = config.get("taker_rate", 0.0005)
        self.slippage_rate: float = config.get("slippage", 0.0005)
        self.funding_rate: float = config.get("funding_rate", 0.0001)
        self.taker_both_legs: bool = config.get("taker_both_legs", False)
        self.interval: str = config.get("interval", "1D")
        self._funding_applied: set = set()   # (symbol, date, hour) — per-slot dedup
        self._funding_daily_done: set = set()  # (symbol, date) — daily fallback dedup

        self._funding_lookup: "dict | None" = None
        fsp = config.get("funding_series_path")
        # `is not None` (not truthiness): if the key is present at all, honour it
        # strictly — an explicit "" must fail loud via read_parquet, never silently
        # fall back to the legacy scalar path (agy diff #6).
        if fsp is not None:
            fdf = pd.read_parquet(Path(fsp), columns=["funding_rate_raw"])
            # Feature-store parquets (research/lib/factor_io.py) always save a
            # tz-aware UTC DatetimeIndex. The engine's own bar timestamps (from
            # the okx/ccxt loader and local_loader, both tz-naive UTC-equivalent
            # — see agent/backtest/loaders/okx.py's `pd.to_datetime(..., unit=
            # "ms")` and local_loader.py's explicit `tz_localize(None)`) are
            # tz-naive. A dict keyed by tz-aware pd.Timestamps can never match a
            # tz-naive lookup key (equality is tz-sensitive even for the same
            # instant), so every real settlement bar would spuriously trip the
            # fail-loud "missing" check below. Normalize to tz-naive UTC here so
            # lookups always compare like-for-like, regardless of whether the
            # source parquet happened to be tz-aware or already tz-naive.
            if fdf.index.tz is not None:
                fdf.index = fdf.index.tz_convert("UTC").tz_localize(None)
            # Duplicate settlement timestamps would let to_dict() silently keep
            # the last, bypassing the fail-loud gap check at the source (agy
            # diff #1). A duplicated index is a feature-file integrity bug.
            if not fdf.index.is_unique:
                dupes = fdf.index[fdf.index.duplicated()].unique().tolist()
                raise ValueError(
                    f"funding series has duplicate timestamps: {dupes[:5]} "
                    f"({len(dupes)} total) — feature file is corrupt, cannot build lookup"
                )
            self._funding_lookup = fdf["funding_rate_raw"].to_dict()

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """Crypto: 24/7, long/short/close all allowed."""
        return True

    def round_size(self, raw_size: float, price: float) -> float:
        """Crypto supports fractional sizes, round to 6 decimals."""
        return round(max(raw_size, 0.0), 6)

    def calc_commission(self, size: float, price: float, _direction: int, is_open: bool) -> float:
        """Maker/Taker separated. Opens typically hit taker, closes hit maker.

        ``_direction`` is unused — reserved for future funding-rate asymmetry
        between long/short legs on perp swaps.

        If ``taker_both_legs`` is set, the close leg is also charged the
        taker rate (matches live market-order fills instead of assuming a
        resting limit order gets maker rebate on exit).
        """
        rate = self.taker_rate if (is_open or self.taker_both_legs) else self.maker_rate
        return size * price * rate

    def apply_slippage(self, price: float, direction: int) -> float:
        """Slippage: unfavourable direction."""
        return price * (1 + direction * self.slippage_rate)

    def on_bar(self, symbol: str, bar: pd.Series, timestamp: pd.Timestamp) -> None:
        """Crypto per-bar hooks: funding fee + liquidation check."""
        fee = calc_crypto_funding_fee(
            symbol, bar, timestamp, self.positions,
            self.funding_rate, self._funding_applied, self._funding_daily_done,
            interval=self.interval, funding_lookup=self._funding_lookup,
        )
        self.capital -= fee

        if check_crypto_liquidation(symbol, bar, self.positions):
            pos = self.positions.get(symbol)
            if pos is not None:
                mark_price = float(bar.get("close", pos.entry_price))
                liq_price = self.apply_slippage(mark_price, -pos.direction)
                self._close_position(symbol, liq_price, timestamp, "liquidation")
