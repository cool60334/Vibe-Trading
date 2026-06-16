"""Order-flow feature family: per-bar primitives -> named factor Series.

Mirrors funding_factors / oi_factors. Pure pandas, aligned to candle index.
Intraday-native: every factor is computed from within-bar trades, so it is
genuinely aligned with no ffill (unlike funding/stablecoin).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# Default "large trade" = > $50k USD notional == these two bucket columns.
LARGE_BUCKETS = ("vol_50_200k", "vol_gt200k")


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den.where(den != 0, np.nan)


def orderflow_factors(
    of_df: pd.DataFrame, candle_idx: pd.DatetimeIndex, interval: str,  # interval: reserved for future rolling-window scaling
    large_buckets: tuple[str, ...] = LARGE_BUCKETS,
) -> dict[str, pd.Series]:
    """of_df indexed by bar-start UTC with columns buy_vol/sell_vol/buy_count/
    sell_count/total_vol/open/close + USD-notional bucket volumes
    (vol_lt10k/vol_10_50k/vol_50_200k/vol_gt200k). Returns 4 factor Series
    reindexed to candle_idx (missing bars -> NaN, never ffill). `large_buckets`
    selects which buckets count as "large" (default > $50k)."""
    df = of_df.reindex(candle_idx)

    buy_v, sell_v = df["buy_vol"], df["sell_vol"]
    # Cast to Int64 (nullable int) to prevent uint32 underflow when Polars emits uint32
    buy_c = df["buy_count"].astype("Int64")
    sell_c = df["sell_count"].astype("Int64")
    total = df["total_vol"]
    large_v = df[list(large_buckets)].sum(axis=1, min_count=1)

    # Convert to float64 for division to get NaN on 0/0, not pd.NA
    count_diff = (buy_c - sell_c).astype("float64")
    count_sum = (buy_c + sell_c).astype("float64")

    feats = {
        "trade_imbalance": _safe_div(buy_v - sell_v, total),
        "trade_count_imbalance": _safe_div(count_diff, count_sum),
        "large_trade_ratio": _safe_div(large_v, total),
        "price_impact": _safe_div(df["close"] - df["open"], total),
    }
    for name, s in feats.items():
        s.name = name
    return feats
