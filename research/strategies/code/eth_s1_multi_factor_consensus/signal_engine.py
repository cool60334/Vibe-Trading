# yaml-hash: c0b1ae565d941ed74324e11935002a3868e8440e8267822129ca22dcf96f6a77
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
    SYMBOL = "ETH-USDT-SWAP"

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
        stablecoin_supply_z = _factors["stablecoin_supply_z"]
        stablecoin_supply_z = stablecoin_supply_z.reindex(ohlcv.index, method='ffill')
        stablecoin_supply_z = stablecoin_supply_z.rolling(3, min_periods=1).mean()
        atr_14 = _factors["atr_14"]
        atr_14 = atr_14.reindex(ohlcv.index, method='ffill')
        atr_14 = atr_14.rolling(3, min_periods=1).mean()
        funding_z = _factors["funding_z"]
        funding_z = funding_z.reindex(ohlcv.index, method='ffill')
        funding_z = funding_z.rolling(3, min_periods=1).mean()
        rolling_std_20 = _factors["rolling_std_20"]
        rolling_std_20 = rolling_std_20.reindex(ohlcv.index, method='ffill')
        rolling_std_20 = rolling_std_20.rolling(3, min_periods=1).mean()

        # Entry signals
        entry_long = ((((stablecoin_supply_z.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 >= 80.0)).rolling(3).sum() >= 2) | (((atr_14.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 <= 20.0)).rolling(3).sum() >= 2) | (((funding_z.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 <= 20.0)).rolling(3).sum() >= 2) | (((rolling_std_20.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 <= 20.0)).rolling(3).sum() >= 2))
        entry_short = ((((stablecoin_supply_z.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 <= 20.0)).rolling(3).sum() >= 2) | (((atr_14.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 >= 80.0)).rolling(3).sum() >= 2) | (((funding_z.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 >= 80.0)).rolling(3).sum() >= 2) | (((rolling_std_20.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100 >= 80.0)).rolling(3).sum() >= 2))

        # Exit state machine
        signal = pd.Series(0.0, index=ohlcv.index)
        position = 0
        entry_price = None
        bars_held = 0

        _inv_pct_3 = stablecoin_supply_z.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100
        _inv_pct_4 = atr_14.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100
        _inv_pct_5 = funding_z.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100
        _inv_pct_6 = rolling_std_20.rolling(90*24, min_periods=90*24//2).rank(pct=True)*100

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
                if pnl_pct >= 6.0 / 100:
                    exit_flag = True
                if pnl_pct <= -3.0 / 100:
                    exit_flag = True
                if (40 <= _inv_pct_3.iloc[bar_i] <= 60) and position != 0:
                    exit_flag = True
                if (40 <= _inv_pct_4.iloc[bar_i] <= 60) and position != 0:
                    exit_flag = True
                if (40 <= _inv_pct_5.iloc[bar_i] <= 60) and position != 0:
                    exit_flag = True
                if (40 <= _inv_pct_6.iloc[bar_i] <= 60) and position != 0:
                    exit_flag = True

                if exit_flag:
                    position = 0
                    entry_price = None
                    bars_held = 0

            signal.iloc[bar_i] = float(position)

        return {symbol: signal}
