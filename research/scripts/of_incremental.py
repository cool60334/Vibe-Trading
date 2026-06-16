"""Driver: incremental IC of order-flow factors orthogonal to funding_z.

For each order-flow factor, prints:
  raw IC@1  — standalone Spearman IC vs 1-bar forward return
  incremental IC — same factor residualized on funding_z (OLS) then IC'd

Usage (server, requires cached parquet + network):
    cd research && python scripts/of_incremental.py

Note on funding_z approximation:
    The rolling(720)-bar z-score below is a rough proxy for stage0a's
    apply_ic_eval_transform for funding (which uses native 8h settlements
    and a calibrated lookback).  The rolling window corresponds to ~180 days
    at one 8h settlement per bar, but here we forward-fill 8h funding onto
    15m bars, so 720 bars = 180 hours = 7.5 days — much shorter.  This is
    intentionally a quick relative-orthogonality read; for exact parity with
    stage0a, import and apply stage0a's transform directly.
"""
from pathlib import Path

import pandas as pd

from lib import orderflow
from lib.orderflow_eval import incremental_ic, _ic, _fwd_return
from lib.orderflow_factors import orderflow_factors
from lib.okx_data import fetch_candles, fetch_funding_history

INTERVAL = "15m"
DAYS = 365

of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")

candles = fetch_candles("ETH-USDT-SWAP", DAYS, bar=INTERVAL)
price = candles["close"].reindex(of.index).ffill()

# fetch_funding_history(symbol, days) -> DataFrame indexed by UTC time, column "funding_rate"
# The function requires a days argument (unlike what the task stub showed).
fund_df = fetch_funding_history("ETH-USDT-SWAP", DAYS)
fund = fund_df["funding_rate"].reindex(of.index, method="ffill")

# Approximate funding_z: rolling z-score over 720 bars.
# NOTE: at 15m resolution, forward-filled 8h funding gives repeated values;
# 720 bars = 180 hours = 7.5 days — much shorter than stage0a's calibrated
# lookback.  Sufficient for a relative orthogonality comparison.
funding_z = (fund - fund.rolling(720).mean()) / fund.rolling(720).std()

feats = orderflow_factors(of, of.index, INTERVAL)

print(f"{'factor':22} {'raw IC@1':>9} {'incremental IC':>15}")
for name, f in feats.items():
    # raw IC: pass a constant-zero control so OLS residual = demeaned factor,
    # and Spearman (rank-invariant to mean shift) equals standalone IC.
    raw = incremental_ic(f, pd.Series(0.0, index=f.index), price, 1)
    inc = incremental_ic(f, funding_z, price, 1)
    print(f"{name:22} {raw:+9.4f} {inc:+15.4f}")
