"""Per-factor decay profile + half-life + maker/taker verdict (ETH 15m).

Run from the research/ directory (so lib.* imports resolve):
    python scripts/of_decay.py

Requires:
  - research/data/orderflow/of_eth_15m_v1.parquet  (server-side cache)
  - network access to OKX public API for candles

Maker/taker verdict interpretation
------------------------------------
  half_life <= 1 bar (15 min)  -> signal evaporates before a limit order is
                                   likely to fill; must use taker (cross spread).
  half_life >= 2 bars          -> some window for maker fill; fee advantage
                                   depends on the exchange's maker/taker spread.
  half_life = None             -> IC never drops to half the 1-bar value in
                                   the evaluated window (or IC(1) is NaN).
"""
from pathlib import Path

import pandas as pd

from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import decay_profile, half_life_bars

# fetch_candles signature (from lib/okx_data.py):
#   fetch_candles(symbol: str, days: int, bar: str = "1H",
#                 use_history_endpoint: bool = True) -> pd.DataFrame
# stage0a calls it as: fetch_candles(symbol=sym_cfg.okx_swap, days=cfg.period, bar=cfg.interval)
# OKX swap symbol format: "ETH-USDT-SWAP"; sub-hour bar strings are lowercase ("15m").
from lib.okx_data import fetch_candles

SYMBOL = "ETH-USDT-SWAP"
INTERVAL = "15m"
# Candles MUST span the whole order-flow cache (~365 days). DAYS=30 fetched only
# the last ~14 overlapping days -> tiny noisy sample -> bogus inflated IC. Fetch
# enough to cover the full cache window (cache starts ~365d ago; pad to 400).
DAYS = 400
MAX_BARS = 8       # profile from 1 bar (15 min) to 8 bars (2 h)

of_dir = Path(__file__).parent.parent / "data" / "orderflow"
of_pl = orderflow.read_cache("eth", INTERVAL, of_dir)
of = of_pl.to_pandas().set_index("ts")

# fetch_candles returns DataFrame indexed by UTC time with open/high/low/close/volume
candles = fetch_candles(symbol=SYMBOL, days=DAYS, bar=INTERVAL)
price = candles["close"].reindex(of.index).ffill()

feats = orderflow_factors(of, of.index, INTERVAL)

print(f"{'factor':26} {'hl':>5}  IC by bar (1=15m, 8=2h)")
print("-" * 70)
for name, f in feats.items():
    prof = decay_profile(f, price, max_bars=MAX_BARS)   # default min_obs=30
    hl = half_life_bars(prof)
    verdict = "TAKER" if (hl is not None and hl <= 1) else ("MAKER-ok" if hl is not None else "flat/NaN")
    bars_str = " ".join(
        f"{k}:{prof[k]:+.3f}" if not __import__("math").isnan(prof[k]) else f"{k}:NaN"
        for k in sorted(prof)
    )
    print(f"{name:26} {str(hl):>5}  {bars_str}  [{verdict}]")
