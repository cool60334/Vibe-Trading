from pathlib import Path
import pandas as pd
from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import quantile_returns
from lib.okx_data import fetch_candles

INTERVAL = "15m"
DAYS = 365
of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")
price = fetch_candles("ETH-USDT-SWAP", DAYS, bar=INTERVAL)["close"].reindex(of.index).ffill()
feats = orderflow_factors(of, of.index, INTERVAL)
for name, f in feats.items():
    print(f"\n== {name} (deciles, 1-bar fwd return, bps) ==")
    q = quantile_returns(f, price, fwd_bars=1, n_q=10) * 1e4
    print(q.round(2).to_string())
    qt = quantile_returns(f, price, fwd_bars=1, n_q=100) * 1e4
    print(f"  bottom 1%: {qt.iloc[0]:.2f} bps | top 1%: {qt.iloc[-1]:.2f} bps")
