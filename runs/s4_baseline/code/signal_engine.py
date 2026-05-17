"""S4 baseline: dual_extreme_contrarian (BOTH funding AND fng at simultaneous extremes).

Per strategy_S4.yaml:
- Long if funding ≤ 15p AND fng ≤ 20p (over 120d window)
- Short if funding ≥ 85p AND fng ≥ 80p
- Persistence: at least 2 of last 3 funding settlements
- Hold up to 168h (1 week), exit on factor reversion to 35-65p neutral band
- Position size 0.5 weight, leverage 1.5 (per yaml)
"""

import pandas as pd
from pathlib import Path


class SignalEngine:
    def __init__(self):
        self.PCT_WIN = 120 * 24  # wider 120-day lookback
        self.MIN_PERIODS = 14 * 24
        self.FUND_HIGH = 0.85
        self.FUND_LOW = 0.15
        self.FNG_HIGH = 0.80
        self.FNG_LOW = 0.20
        self.PERSISTENCE_WIN = 24
        self.PERSISTENCE_HITS = 2
        self.POSITION_SIZE = 0.5

        data_dir = Path(__file__).resolve().parent.parent / "factor_data"
        self.funding_df = pd.read_parquet(data_dir / "funding.parquet")
        self.fng_df = pd.read_parquet(data_dir / "fng.parquet")

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

            funding = self.funding_df["funding_rate"].reindex(df_naive.index, method="ffill").bfill()
            fng = self.fng_df["fng"].reindex(df_naive.index, method="ffill").bfill()

            f_hi = self._pct_threshold(funding, self.FUND_HIGH)
            f_lo = self._pct_threshold(funding, self.FUND_LOW)
            fng_hi = self._pct_threshold(fng, self.FNG_HIGH)
            fng_lo = self._pct_threshold(fng, self.FNG_LOW)

            short_now = (funding >= f_hi) & (fng >= fng_hi)
            long_now = (funding <= f_lo) & (fng <= fng_lo)

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
