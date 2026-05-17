"""S3 baseline: trend_with_factor_filter (EMA20/50 trend + funding gate).

Per strategy_S3.yaml:
- Long: EMA20 > EMA50 (uptrend) AND funding ≤ 60p (not crowded long)
- Short: EMA20 < EMA50 (downtrend) AND funding ≥ 40p (not crowded short)
- Persistence: trend + filter both hold for 2 of last 3 funding settlements
- Exit on EMA crossover reversal or funding return to 40-60p neutral band
- Position size 0.5 weight, leverage 1.5 (per yaml)
"""

import pandas as pd
from pathlib import Path


class SignalEngine:
    def __init__(self):
        self.EMA_FAST = 20
        self.EMA_SLOW = 50
        self.PCT_WIN = 90 * 24
        self.MIN_PERIODS = 7 * 24
        self.FUND_GATE_LONG = 0.60   # block long if funding > 60p (crowded)
        self.FUND_GATE_SHORT = 0.40  # block short if funding < 40p (crowded short)
        self.PERSISTENCE_WIN = 24
        self.PERSISTENCE_HITS = 2
        self.POSITION_SIZE = 0.5

        data_dir = Path(__file__).resolve().parent.parent / "factor_data"
        self.funding_df = pd.read_parquet(data_dir / "funding.parquet")

    def _pct_threshold(self, s, pct):
        return s.rolling(self.PCT_WIN, min_periods=self.MIN_PERIODS).quantile(pct)

    def generate(self, data_map):
        signals = {}
        for code, df in data_map.items():
            idx = df.index
            if hasattr(idx, "tz") and idx.tz is not None:
                idx = idx.tz_convert(None)
            df_naive = df.copy()
            df_naive.index = idx

            ema_fast = df_naive["close"].ewm(span=self.EMA_FAST).mean()
            ema_slow = df_naive["close"].ewm(span=self.EMA_SLOW).mean()
            uptrend = ema_fast > ema_slow
            downtrend = ema_fast < ema_slow

            funding = self.funding_df["funding_rate"].reindex(df_naive.index, method="ffill").bfill()
            f_long_gate = self._pct_threshold(funding, self.FUND_GATE_LONG)
            f_short_gate = self._pct_threshold(funding, self.FUND_GATE_SHORT)

            long_now = uptrend & (funding <= f_long_gate)
            short_now = downtrend & (funding >= f_short_gate)

            raw = pd.Series(0.0, index=df_naive.index)
            raw[short_now] = -1.0
            raw[long_now] = 1.0

            short_hits = (raw == -1.0).rolling(self.PERSISTENCE_WIN, min_periods=1).sum()
            long_hits = (raw == 1.0).rolling(self.PERSISTENCE_WIN, min_periods=1).sum()
            sig = pd.Series(0.0, index=df.index)
            sm = short_hits.values >= self.PERSISTENCE_HITS
            lm = long_hits.values >= self.PERSISTENCE_HITS
            sig.iloc[sm] = -self.POSITION_SIZE
            sig.iloc[lm] = self.POSITION_SIZE

            signals[code] = sig
        return signals
