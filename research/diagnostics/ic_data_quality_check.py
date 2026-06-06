"""
ic_data_quality_check.py — Re-evaluate BTC factor IC after fixing two data-quality
defects identified in the stage-0a feature pipeline:

  1. OBV is a raw cumulative sum (non-stationary). Spearman IC on a monotonically
     drifting level measures spurious trend co-movement, not predictive power.
     Fix: evaluate stationary transforms (rolling z-score, hourly diff).

  2. Funding rate (8h settlements) and stablecoin supply (daily) are forward-filled
     onto the 1H grid, repeating each value 8x / 24x. This inflates the apparent
     sample size and autocorrelation. The headline IC is computed on dependent rows.
     Fix: subsample to the native settlement frequency (only rows where the raw
     value changes) before computing IC.

Runs fully offline from the existing feature store parquet + a cached OHLCV CSV.
Does NOT refetch network data and does NOT mutate any pipeline artifact.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

_THIS = Path(__file__).resolve()
_RESEARCH = _THIS.parent.parent
sys.path.insert(0, str(_RESEARCH))

from lib.factor_io import load_features  # noqa: E402

HORIZONS = [8, 24, 72, 168]
OHLCV_CSV = _RESEARCH.parent / "runs" / "btc_s1_multi_factor_consensus_sweep_000" / "artifacts" / "ohlcv_BTC-USDT-SWAP.csv"


def load_close() -> pd.Series:
    df = pd.read_csv(OHLCV_CSV, parse_dates=["trade_date"])
    df = df.set_index("trade_date").sort_index()
    idx = df.index
    if idx.tz is None:
        df.index = idx.tz_localize("UTC")
    return df["close"].astype("float64")


def forward_returns(close: pd.Series, horizons: list[int]) -> pd.DataFrame:
    out = pd.DataFrame(index=close.index)
    for h in horizons:
        out[f"ret_{h}h"] = close.shift(-h) / close - 1
    return out


def ic_row(factor: pd.Series, rets: pd.DataFrame) -> dict:
    row = {}
    for h in HORIZONS:
        paired = pd.concat([factor, rets[f"ret_{h}h"]], axis=1).dropna()
        if len(paired) < 20:
            row[h] = float("nan")
            continue
        rho, _ = spearmanr(paired.iloc[:, 0], paired.iloc[:, 1])
        row[h] = float(rho)
    row["n"] = int(pd.concat([factor, rets["ret_24h"]], axis=1).dropna().shape[0])
    return row


def native_subsample(factor: pd.Series) -> pd.Series:
    """Keep only rows where the value changes vs the previous row (drops ffill repeats)."""
    changed = factor.ne(factor.shift(1))
    return factor[changed]


def fmt(row: dict) -> str:
    return "  ".join(f"{h:>4}h={row[h]:+.4f}" for h in HORIZONS) + f"   n={row['n']}"


def main() -> None:
    feats = load_features("btc")
    if feats.index.tz is None:
        feats.index = feats.index.tz_localize("UTC")
    close = load_close()

    # Align close to feature index
    close = close.reindex(feats.index, method="ffill")
    rets = forward_returns(close, HORIZONS)

    print(f"feature rows: {len(feats)}   close rows aligned: {close.notna().sum()}")
    print(f"window: {feats.index.min()} .. {feats.index.max()}\n")

    print("=" * 78)
    print("OBV — raw cumulative vs stationary transforms")
    print("=" * 78)
    obv = feats["obv"]
    print(f"raw cumsum        {fmt(ic_row(obv, rets))}")
    obv_z = (obv - obv.rolling(720, min_periods=30).mean()) / obv.rolling(720, min_periods=30).std().replace(0, np.nan)
    print(f"720h z-score      {fmt(ic_row(obv_z, rets))}")
    print(f"hourly diff       {fmt(ic_row(obv.diff(), rets))}")
    print(f"24h diff          {fmt(ic_row(obv.diff(24), rets))}")

    print("\n" + "=" * 78)
    print("funding_rate_raw — 1H ffilled (inflated) vs native 8h settlements")
    print("=" * 78)
    fr = feats["funding_rate_raw"]
    print(f"1H ffilled        {fmt(ic_row(fr, rets))}")
    fr_native = native_subsample(fr)
    print(f"native 8h         {fmt(ic_row(fr_native, rets))}")

    print("\n" + "=" * 78)
    print("stablecoin_supply_z — hourly (ffill-driven) vs daily subsample")
    print("=" * 78)
    sc = feats["stablecoin_supply_z"]
    print(f"hourly            {fmt(ic_row(sc, rets))}")
    sc_daily = sc.resample("1D").last().dropna()
    rets_daily = forward_returns(close.resample("1D").last(), HORIZONS)
    # recompute IC on daily grid
    row = {}
    for h in HORIZONS:
        # daily forward return over h hours ≈ h/24 days; use the hourly close-based ret reindexed
        paired = pd.concat([sc_daily, rets[f"ret_{h}h"].reindex(sc_daily.index, method="nearest")], axis=1).dropna()
        rho, _ = spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]) if len(paired) >= 20 else (float("nan"), None)
        row[h] = float(rho)
    row["n"] = len(sc_daily)
    print(f"daily subsample   {fmt(row)}")

    print("\n" + "=" * 78)
    print("control: rsi_14 (already stationary — sanity check)")
    print("=" * 78)
    print(f"rsi_14            {fmt(ic_row(feats['rsi_14'], rets))}")


if __name__ == "__main__":
    main()
