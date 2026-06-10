# yaml-hash: 5ab42c05a1bfe325b9e9192b8ea3ed00ab496d4edd5774b865d2cb61c8cff912
import sys
from pathlib import Path

import pandas as pd


def _ensure_research_on_syspath() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    research_dir = repo_root / "research"
    sp = str(research_dir)
    if sp not in sys.path:
        sys.path.insert(0, sp)


class SignalEngine:
    SYMBOL = "SOL-USDT-SWAP"

    def generate(self, data_map: dict) -> dict:
        _ensure_research_on_syspath()
        from lib.factor_io import load_factor_values

        symbol = self.SYMBOL
        ohlcv = data_map[symbol]

        # Load factor values
        _factors = load_factor_values(symbol)
        # Strip tz on factor index to match naive ohlcv.index in backtest.
        if _factors.index.tz is not None:
            _factors.index = _factors.index.tz_localize(None)

        # Indicator setup
        stablecoin_supply_zscore = _factors["stablecoin_supply_zscore"]
        stablecoin_supply_zscore = stablecoin_supply_zscore.reindex(ohlcv.index, method='ffill')
        stablecoin_supply_zscore = stablecoin_supply_zscore.rolling(3, min_periods=1).mean()
        bollinger_band_width = _factors["bollinger_band_width"]
        bollinger_band_width = bollinger_band_width.reindex(ohlcv.index, method='ffill')
        bollinger_band_width = bollinger_band_width.rolling(3, min_periods=1).mean()

        # Entry signals
        entry_long = ((((stablecoin_supply_zscore.rolling(120*24, min_periods=120*24//2).rank(pct=True)*100 >= 75.0)).rolling(3).sum() >= 2) & (((bollinger_band_width.rolling(120*24, min_periods=120*24//2).rank(pct=True)*100 >= 70.0)).rolling(3).sum() >= 2))
        entry_short = ((((stablecoin_supply_zscore.rolling(120*24, min_periods=120*24//2).rank(pct=True)*100 <= 25.0)).rolling(3).sum() >= 2) & (((bollinger_band_width.rolling(120*24, min_periods=120*24//2).rank(pct=True)*100 <= 30.0)).rolling(3).sum() >= 2))

        # Exit state machine
        signal = pd.Series(0.0, index=ohlcv.index)
        position = 0
        entry_price = None
        bars_held = 0

        for bar_i, ts in enumerate(ohlcv.index):
            if position == 0:
                if entry_long.iloc[bar_i]:
                    position = 1
                    entry_price = ohlcv["close"].iloc[bar_i]
                    bars_held = 0
                elif entry_short.iloc[bar_i]:
                    position = -1
                    entry_price = ohlcv["close"].iloc[bar_i]
                    bars_held = 0
            else:
                bars_held += 1
                _close_price = ohlcv["close"].iloc[bar_i]
                pnl_pct = (_close_price - entry_price) / entry_price * position
                exit_flag = False

                if bars_held >= 120:
                    exit_flag = True
                if pnl_pct >= 7.0 / 100:
                    exit_flag = True
                if pnl_pct <= -3.0 / 100:
                    exit_flag = True

                if exit_flag:
                    position = 0
                    entry_price = None
                    bars_held = 0

            signal.iloc[bar_i] = float(position)

        return {symbol: signal}
