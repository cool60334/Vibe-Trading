"""S1: multi-factor consensus contrarian (funding + F&G).

Walk-through v1 simplifications:
- Absolute factor thresholds (production: rolling percentile)
- 2-of-24h persistence (production: 2-of-3 funding settlements)
- Signal magnitude 0.5 (half-size); engine + leverage handle gross exposure
- Exit by signal reversion only (no explicit TP/SL in v1)
"""

import pandas as pd
from pathlib import Path


class SignalEngine:
    """S1 multi-factor consensus contrarian."""

    def __init__(self):
        # All thresholds inside __init__ — class-body assigns reject UnaryOp negatives.
        self.FUNDING_HIGH = 0.00015
        self.FUNDING_LOW = -0.00005
        self.FNG_HIGH = 70
        self.FNG_LOW = 30
        self.PERSISTENCE_WIN = 24
        self.PERSISTENCE_HITS = 2
        self.POSITION_SIZE = 0.5

        data_dir = Path(__file__).resolve().parent.parent / "factor_data"
        self.funding_df = pd.read_parquet(data_dir / "funding.parquet")
        self.fng_df = pd.read_parquet(data_dir / "fng.parquet")

    def _raw_signal(self, idx):
        funding_h = self.funding_df["funding_rate"].reindex(idx, method="ffill").bfill()
        fng_h = self.fng_df["fng"].reindex(idx, method="ffill").bfill()
        short_cond = (funding_h >= self.FUNDING_HIGH) & (fng_h >= self.FNG_HIGH)
        long_cond = (funding_h <= self.FUNDING_LOW) & (fng_h <= self.FNG_LOW)
        raw = pd.Series(0.0, index=idx)
        raw[short_cond] = -1.0
        raw[long_cond] = 1.0
        return raw

    def generate(self, data_map):
        signals = {}
        for code, df in data_map.items():
            idx = df.index
            if hasattr(idx, "tz") and idx.tz is not None:
                idx = idx.tz_convert(None)
            df_naive = df.copy()
            df_naive.index = idx

            raw = self._raw_signal(df_naive.index)

            short_hits = (raw == -1.0).rolling(self.PERSISTENCE_WIN, min_periods=1).sum()
            long_hits = (raw == 1.0).rolling(self.PERSISTENCE_WIN, min_periods=1).sum()
            sig = pd.Series(0.0, index=df.index)
            short_mask = short_hits.values >= self.PERSISTENCE_HITS
            long_mask = long_hits.values >= self.PERSISTENCE_HITS
            sig.iloc[short_mask] = -self.POSITION_SIZE
            sig.iloc[long_mask] = self.POSITION_SIZE

            signals[code] = sig
        return signals
