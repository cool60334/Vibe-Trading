"""S1 v2: contrarian with rolling percentile thresholds + ATR exits + regime filter + FNG lag.

Improvements over v1 (from gpt-5 backtest-diagnose):
- Rolling 90d percentile thresholds (not absolute) for funding + FNG
- FNG smoothed 3-day EMA + lagged 1 day (daily data — avoid look-ahead)
- Regime filter: long only if price > SMA100; short only if price < SMA100
- ATR-based exits: 2x ATR stop, 3x ATR TP, 72h time stop (stateful bar loop)
- Persistence: 3 funding settlements (24h) with min 2 hits
"""

import numpy as np
import pandas as pd
from pathlib import Path


class SignalEngine:
    """S1 v2 multi-factor consensus contrarian with ATR exits."""

    def __init__(self):
        # Constants in __init__ (class-body negative literals rejected by AST validator)
        self.PERCENTILE_WIN_H = 90 * 24       # 90-day rolling window in hours
        self.MIN_PERIODS = 7 * 24             # need at least 1 week to compute percentile
        self.FUND_HIGH_PCT = 0.80
        self.FUND_LOW_PCT = 0.20
        self.FNG_HIGH_PCT = 0.70
        self.FNG_LOW_PCT = 0.30
        self.PERSISTENCE_WIN = 24             # last 24 hours
        self.PERSISTENCE_HITS = 2             # of last 3 funding settlements (every 8h)

        self.SMA_WIN_H = 100 * 24             # 100-day SMA for regime gate
        self.ATR_WIN = 24                     # 24-hour ATR
        self.ATR_STOP_MULT = 2.0
        self.ATR_TP_MULT = 3.0
        self.MAX_HOLD_H = 72                  # hard time stop
        self.POSITION_SIZE = 0.5

        self.FNG_SMOOTH = 3                   # 3-day EMA on daily FNG
        self.FNG_LAG_DAYS = 1                 # lag FNG by 1 day to avoid look-ahead

        data_dir = Path(__file__).resolve().parent.parent / "factor_data"
        self.funding_df = pd.read_parquet(data_dir / "funding.parquet")
        fng_raw = pd.read_parquet(data_dir / "fng.parquet")
        # smooth FNG with 3-day EMA, then lag 1 day
        fng_raw["fng_smooth"] = fng_raw["fng"].ewm(span=self.FNG_SMOOTH).mean()
        fng_raw["fng_smooth"] = fng_raw["fng_smooth"].shift(self.FNG_LAG_DAYS)
        self.fng_df = fng_raw

    def _align_factors(self, idx):
        """Align funding + FNG (smoothed, lagged) to hourly index."""
        funding_h = self.funding_df["funding_rate"].reindex(idx, method="ffill").bfill()
        fng_h = self.fng_df["fng_smooth"].reindex(idx, method="ffill").bfill()
        return funding_h, fng_h

    def _percentile_threshold(self, s, pct):
        """Rolling quantile threshold series (right-aligned, leaks no future)."""
        return s.rolling(self.PERCENTILE_WIN_H, min_periods=self.MIN_PERIODS).quantile(pct)

    def _raw_factor_signal(self, idx):
        """Return raw factor signal Series in {-1, 0, +1}."""
        funding_h, fng_h = self._align_factors(idx)
        fund_high = self._percentile_threshold(funding_h, self.FUND_HIGH_PCT)
        fund_low = self._percentile_threshold(funding_h, self.FUND_LOW_PCT)
        fng_high = self._percentile_threshold(fng_h, self.FNG_HIGH_PCT)
        fng_low = self._percentile_threshold(fng_h, self.FNG_LOW_PCT)
        short_now = (funding_h >= fund_high) & (fng_h >= fng_high)
        long_now = (funding_h <= fund_low) & (fng_h <= fng_low)
        raw = pd.Series(0.0, index=idx)
        raw[short_now] = -1.0
        raw[long_now] = 1.0
        # persistence: at least PERSISTENCE_HITS hits in last PERSISTENCE_WIN bars
        short_hits = (raw == -1.0).rolling(self.PERSISTENCE_WIN, min_periods=1).sum()
        long_hits = (raw == 1.0).rolling(self.PERSISTENCE_WIN, min_periods=1).sum()
        persisted = pd.Series(0.0, index=idx)
        persisted[(short_hits.values >= self.PERSISTENCE_HITS)] = -1.0
        persisted[(long_hits.values >= self.PERSISTENCE_HITS)] = 1.0
        return persisted

    def _atr(self, df):
        """24-hour ATR."""
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(self.ATR_WIN, min_periods=1).mean()

    def _regime_gate(self, df):
        """SMA100 regime gate (momentum-aligned per gpt-5 diagnose):
        - long_ok only when price ABOVE SMA100 (established uptrend)
        - short_ok only when price BELOW SMA100 (established downtrend)
        Filters contrarian signals to align with major trend regime.
        """
        sma = df["close"].rolling(self.SMA_WIN_H, min_periods=self.SMA_WIN_H // 2).mean()
        long_ok = df["close"] > sma
        short_ok = df["close"] < sma
        return long_ok, short_ok

    def _apply_exits(self, df, raw_signal, atr, long_ok, short_ok):
        """Stateful bar loop applying ATR TP/SL + time stop + regime gate. Returns target weight series."""
        prices = df["close"].values
        atr_v = atr.values
        raw_v = raw_signal.values
        long_v = long_ok.values
        short_v = short_ok.values
        n = len(df)
        weights = np.zeros(n)

        position = 0  # 0 / +1 / -1
        entry_price = 0.0
        entry_atr = 0.0
        entry_bar = -1

        for i in range(n):
            price = prices[i]
            # Check exit first if in position
            if position != 0:
                bars_held = i - entry_bar
                if position == 1:
                    stop = entry_price - self.ATR_STOP_MULT * entry_atr
                    tp = entry_price + self.ATR_TP_MULT * entry_atr
                    exit_now = (price <= stop) or (price >= tp) or (bars_held >= self.MAX_HOLD_H)
                else:
                    stop = entry_price + self.ATR_STOP_MULT * entry_atr
                    tp = entry_price - self.ATR_TP_MULT * entry_atr
                    exit_now = (price >= stop) or (price <= tp) or (bars_held >= self.MAX_HOLD_H)
                if exit_now:
                    position = 0
                    entry_price = 0.0

            # Check entry if flat (signal + regime gate + valid ATR)
            if position == 0 and not np.isnan(atr_v[i]) and atr_v[i] > 0:
                sig = raw_v[i]
                if sig == 1.0 and long_v[i]:
                    position = 1
                    entry_price = price
                    entry_atr = atr_v[i]
                    entry_bar = i
                elif sig == -1.0 and short_v[i]:
                    position = -1
                    entry_price = price
                    entry_atr = atr_v[i]
                    entry_bar = i

            weights[i] = position * self.POSITION_SIZE

        return pd.Series(weights, index=df.index)

    def generate(self, data_map):
        signals = {}
        for code, df in data_map.items():
            # Ensure naive index for factor alignment
            idx = df.index
            if hasattr(idx, "tz") and idx.tz is not None:
                idx = idx.tz_convert(None)
            df_naive = df.copy()
            df_naive.index = idx

            raw_signal = self._raw_factor_signal(df_naive.index)
            atr = self._atr(df_naive)
            long_ok, short_ok = self._regime_gate(df_naive)
            weights = self._apply_exits(df_naive, raw_signal, atr, long_ok, short_ok)
            # restore original index
            weights.index = df.index
            signals[code] = weights
        return signals
