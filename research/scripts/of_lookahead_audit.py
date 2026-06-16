"""Lookahead audit: factor IC vs entry lag. A same-bar peek collapses from
lag 0 to lag 1; a real signal persists. Run on the server (DAYS must cover the
whole order-flow cache)."""
from pathlib import Path

import pandas as pd

from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import execution_ic
from lib.okx_data import fetch_candles

INTERVAL = "15m"
DAYS = 400
of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")
price = fetch_candles("ETH-USDT-SWAP", DAYS, bar=INTERVAL)["close"].reindex(of.index).ffill()
feats = orderflow_factors(of, of.index, INTERVAL)

print(f"{'factor':24} {'lag0 (signal-close)':>20} {'lag1 (next bar)':>16}")
for name, f in feats.items():
    ic0 = execution_ic(f, price, entry_lag_bars=0, hold_bars=4, min_obs=200)
    ic1 = execution_ic(f, price, entry_lag_bars=1, hold_bars=4, min_obs=200)
    print(f"{name:24} {ic0:+20.4f} {ic1:+16.4f}")
