"""Ensemble S2+S3+S4 with v1c regime gate.

Each sub-strategy emits raw signal in {-1, 0, +1}. Ensemble = mean / 3.
Final position = ensemble_signal * POSITION_SIZE * bear_mask.

POSITION_SIZE = 0.5 → fully aligned 3-way vote ≈ 0.5 weight (matches single-strat baseline).
Single strategy alone with two zeros ≈ 0.167 (auto risk-reduction when uncorrelated).

Reads factor_data/{funding,fng,regime}.parquet.
"""

import pandas as pd
from pathlib import Path


class SignalEngine:
    def __init__(self):
        self.PERSISTENCE_WIN = 72
        self.PERSISTENCE_HITS = 4
        self.POSITION_SIZE = 0.5

        # S2 params
        self.S2_PCT_WIN = 90 * 24
        self.S2_MIN = 7 * 24
        self.S2_FH = 0.80
        self.S2_FL = 0.20
        self.S2_FNG_VETO_HI = 0.85
        self.S2_FNG_VETO_LO = 0.15

        # S3 params
        self.S3_EMA_FAST = 20
        self.S3_EMA_SLOW = 50
        self.S3_PCT_WIN = 90 * 24
        self.S3_MIN = 7 * 24
        self.S3_FUND_GATE_LONG = 0.60
        self.S3_FUND_GATE_SHORT = 0.40

        # S4 params
        self.S4_PCT_WIN = 120 * 24
        self.S4_MIN = 14 * 24
        self.S4_FH = 0.85
        self.S4_FL = 0.15
        self.S4_FNG_HI = 0.80
        self.S4_FNG_LO = 0.20

        data_dir = Path(__file__).resolve().parent.parent / "factor_data"
        self.funding_df = pd.read_parquet(data_dir / "funding.parquet")
        self.fng_df = pd.read_parquet(data_dir / "fng.parquet")
        self.regime_df = pd.read_parquet(data_dir / "regime.parquet")

    def _pct(self, series, win, minp, q):
        return series.rolling(win, min_periods=minp).quantile(q)

    def _persist(self, raw, win, hits):
        short_h = (raw == -1.0).rolling(win, min_periods=1).sum()
        long_h = (raw == 1.0).rolling(win, min_periods=1).sum()
        out = pd.Series(0.0, index=raw.index)
        out[short_h.values >= hits] = -1.0
        out[long_h.values >= hits] = 1.0
        return out

    def _signal_s2(self, funding, fng):
        f_hi = self._pct(funding, self.S2_PCT_WIN, self.S2_MIN, self.S2_FH)
        f_lo = self._pct(funding, self.S2_PCT_WIN, self.S2_MIN, self.S2_FL)
        fng_hi = self._pct(fng, self.S2_PCT_WIN, self.S2_MIN, self.S2_FNG_VETO_HI)
        fng_lo = self._pct(fng, self.S2_PCT_WIN, self.S2_MIN, self.S2_FNG_VETO_LO)
        short_now = (funding >= f_hi) & (fng < fng_lo).pipe(lambda s: ~s)
        long_now = (funding <= f_lo) & (fng > fng_hi).pipe(lambda s: ~s)
        raw = pd.Series(0.0, index=funding.index)
        raw[short_now] = -1.0
        raw[long_now] = 1.0
        return self._persist(raw, self.PERSISTENCE_WIN, self.PERSISTENCE_HITS)

    def _signal_s3(self, close, funding):
        ema_f = close.ewm(span=self.S3_EMA_FAST, adjust=False).mean()
        ema_s = close.ewm(span=self.S3_EMA_SLOW, adjust=False).mean()
        uptrend = ema_f > ema_s
        downtrend = ema_f < ema_s
        f_gate_long = self._pct(funding, self.S3_PCT_WIN, self.S3_MIN, self.S3_FUND_GATE_LONG)
        f_gate_short = self._pct(funding, self.S3_PCT_WIN, self.S3_MIN, self.S3_FUND_GATE_SHORT)
        long_now = uptrend & (funding <= f_gate_long)
        short_now = downtrend & (funding >= f_gate_short)
        raw = pd.Series(0.0, index=funding.index)
        raw[short_now] = -1.0
        raw[long_now] = 1.0
        return self._persist(raw, self.PERSISTENCE_WIN, self.PERSISTENCE_HITS)

    def _signal_s4(self, funding, fng):
        f_hi = self._pct(funding, self.S4_PCT_WIN, self.S4_MIN, self.S4_FH)
        f_lo = self._pct(funding, self.S4_PCT_WIN, self.S4_MIN, self.S4_FL)
        fng_hi = self._pct(fng, self.S4_PCT_WIN, self.S4_MIN, self.S4_FNG_HI)
        fng_lo = self._pct(fng, self.S4_PCT_WIN, self.S4_MIN, self.S4_FNG_LO)
        short_now = (funding >= f_hi) & (fng >= fng_hi)
        long_now = (funding <= f_lo) & (fng <= fng_lo)
        raw = pd.Series(0.0, index=funding.index)
        raw[short_now] = -1.0
        raw[long_now] = 1.0
        return self._persist(raw, self.PERSISTENCE_WIN, self.PERSISTENCE_HITS)

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
            regime = self.regime_df["regime"].reindex(df_naive.index, method="ffill").bfill()
            close = df_naive["close"]

            s2 = self._signal_s2(funding, fng)
            s3 = self._signal_s3(close, funding)
            s4 = self._signal_s4(funding, fng)

            ensemble = (s2 + s3 + s4) / 3.0

            sig = pd.Series(ensemble.values * self.POSITION_SIZE, index=df.index)
            bear_mask = (regime.values == "bear")
            sig.iloc[~bear_mask] = 0.0

            signals[code] = sig
        return signals
