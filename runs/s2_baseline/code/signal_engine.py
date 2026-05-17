"""S2 baseline: funding_mean_reversion (single factor, percentile-based).

Per strategy_S2.yaml:
- Long if funding ≤ 20p over 90d, persistence 2-of-3 settlements
- Short if funding ≥ 80p over 90d, persistence 2-of-3 settlements
- Veto: skip if F&G at opposite extreme (≥85p for long; ≤15p for short)
- Exit on signal reversion (funding back to 40-60p band)
- Position size 0.5 weight, leverage 2.0 (per yaml)
"""

import pandas as pd
from pathlib import Path


class SignalEngine:
    def __init__(self):
        self.PCT_WIN = 90 * 24
        self.MIN_PERIODS = 7 * 24
        self.FUND_HIGH = 0.80
        self.FUND_LOW = 0.20
        self.FNG_VETO_HIGH = 0.85  # don't long when fng extreme greed
        self.FNG_VETO_LOW = 0.15
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
            fng_hi = self._pct_threshold(fng, self.FNG_VETO_HIGH)
            fng_lo = self._pct_threshold(fng, self.FNG_VETO_LOW)

            short_now = (funding >= f_hi) & (fng < fng_lo).pipe(lambda s: ~s)  # short unless fng extreme fear
            long_now = (funding <= f_lo) & (fng > fng_hi).pipe(lambda s: ~s)  # long unless fng extreme greed

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
