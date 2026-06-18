"""Does the OI/positioning signal add value beyond funding_z, or just shadow it?

Both measure leverage crowding, so before wiring OI into the pipeline we check
orthogonality: incremental_ic residualizes the candidate on funding_z, then
re-scores vs forward return. retained% ~100 => orthogonal/additive (wire it);
retained% ~0 => redundant with funding (park it).

    python scripts/oi_orthogonality.py                          # BTC
    python scripts/oi_orthogonality.py ETHUSDT ETH-USDT-SWAP ETH/USDT:USDT
"""
import sys
from pathlib import Path

from lib import oi_metrics
from lib.ccxt_data import fetch_funding_history_multiyear
from lib.derived_factors import SCREEN_ZSCORE_DAYS, _rolling_z, funding_factors, oi_factors
from lib.okx_data import fetch_candles
from lib.orderflow_eval import _fwd_return, _ic, incremental_ic

SYMBOL_OI = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
SYMBOL_PX = sys.argv[2] if len(sys.argv) > 2 else "BTC-USDT-SWAP"
CCXT_SYM = sys.argv[3] if len(sys.argv) > 3 else "BTC/USDT:USDT"
DAYS = 2200
FWD = 24
W = SCREEN_ZSCORE_DAYS * 24

oi = oi_metrics.load_oi_parquet(SYMBOL_OI, Path("data/oi"))
price = fetch_candles(SYMBOL_PX, DAYS, bar="1H")["close"].reindex(oi.index).ffill()
funding = fetch_funding_history_multiyear(CCXT_SYM, DAYS, exchange="binance")["funding_rate"]
fz = funding_factors(funding.reindex(oi.index, method="ffill"))["funding_z"]

cands = {
    "oi_mom": oi_factors(oi["oi"], price)["oi_mom"],
    "global_ls_acct_z": _rolling_z(oi["global_ls_accounts"], W),
}

fwd = _fwd_return(price, FWD)
print(f"{SYMBOL_OI}: standalone vs incremental-IC (control=funding_z), fwd={FWD}h")
print(f"{'factor':20} {'standalone':>11} {'incr|funding':>13} {'retained%':>10}")
print("-" * 56)
for name, f in cands.items():
    base = _ic(f, fwd)
    inc = incremental_ic(f, fz, price, FWD)
    ret = abs(inc) / abs(base) * 100 if abs(base) > 1e-9 else float("nan")
    print(f"{name:20} {base:+11.4f} {inc:+13.4f} {ret:10.1f}")
print(f"{'funding_z (ref)':20} {_ic(fz, fwd):+11.4f}")
