"""OI factor go/no-go: standalone IC + entry-lag audit on the multi-year OI cache.

The cheap first gate before any pipeline wiring (mirrors of_lookahead_audit.py).
A same-bar lookahead collapses from lag0 (enter at signal close) to lag1 (enter
next bar); a real, tradeable signal persists. OI is a *positioning* factor, so
the thesis is long half-life => lag0 ~= lag1 (unlike order-flow microstructure).

    python scripts/oi_lookahead_audit.py            # BTC (deepest history)
    python scripts/oi_lookahead_audit.py ETHUSDT ETH-USDT-SWAP
"""
import sys
from pathlib import Path

import pandas as pd

from lib import oi_metrics
from lib.derived_factors import SCREEN_ZSCORE_DAYS, _rolling_z, oi_factors
from lib.okx_data import fetch_candles
from lib.orderflow_eval import decay_profile, execution_ic, half_life_bars

SYMBOL_OI = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
SYMBOL_PX = sys.argv[2] if len(sys.argv) > 2 else "BTC-USDT-SWAP"
DAYS = 2200          # cover BTC OI back to 2020-09 (OKX returns what it has)
HOLD = 24            # 1-day hold at 1H
MIN_OBS = 500

oi = oi_metrics.load_oi_parquet(SYMBOL_OI, Path("data/oi"))
price = fetch_candles(SYMBOL_PX, DAYS, bar="1H")["close"].reindex(oi.index).ffill()

W = SCREEN_ZSCORE_DAYS * 24
feats: dict[str, pd.Series] = {}
feats.update(oi_factors(oi["oi"], price))                         # oi_z / oi_price_divergence / oi_mom
feats["toptrader_ls_pos_z"] = _rolling_z(oi["toptrader_ls_positions"], W)  # smart money
feats["global_ls_acct_z"] = _rolling_z(oi["global_ls_accounts"], W)        # crowd
feats["taker_buysell_z"] = _rolling_z(oi["taker_buysell_ratio"], W)        # taker flow

overlap = int(price.notna().sum())
print(f"OI={SYMBOL_OI}  px={SYMBOL_PX}  overlap={overlap} bars  hold={HOLD}h  min_obs={MIN_OBS}")
print(f"{'factor':22} {'IC lag0':>9} {'IC lag1':>9} {'drop%':>7} {'half_life':>10}")
print("-" * 62)
for name, f in feats.items():
    ic0 = execution_ic(f, price, entry_lag_bars=0, hold_bars=HOLD, min_obs=MIN_OBS)
    ic1 = execution_ic(f, price, entry_lag_bars=1, hold_bars=HOLD, min_obs=MIN_OBS)
    drop = (1 - abs(ic1) / abs(ic0)) * 100 if (ic0 == ic0 and abs(ic0) > 1e-9) else float("nan")
    hl = half_life_bars(decay_profile(f, price, max_bars=72, min_obs=MIN_OBS))
    print(f"{name:22} {ic0:+9.4f} {ic1:+9.4f} {drop:7.1f} {str(hl):>10}")
