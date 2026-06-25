"""Full classification gate for the cross-venue premium factor, on REAL
full-coverage ccxt Coinbase data (the data source the runner should use instead
of the truncated Massive free tier).

Per (coin, factor in depeg_z/fiat_prem_z):
  absIC      — Spearman IC vs 24h forward return
  partIC     — partial IC controlling funding_z + basis_rel (orthogonality)
  IC_train   — IC on first 70% (in-sample)
  IC_oos     — IC on last 30% (holdout stability)
  net_bps    — top-vs-bottom decile spread net of 7.5bps round-trip slippage
  flip       — sign-flip rate (turnover proxy)

Promotion needs: absIC >= ~0.03-0.05, partIC still material (not a funding/basis
clone), IC_oos same sign and not collapsed, net_bps > 0.
"""
import sys
from pathlib import Path

import pandas as pd

_R = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_R))

from lib.okx_data import fetch_candles
from lib.ccxt_data import fetch_ohlcv_ccxt
from lib.derived_factors import cross_venue_premium_factors
from lib.factor_metrics import add_forward_returns, compute_ic
from lib.factor_gates import partial_ic, sign_flip_rate, decile_spread_net_of_cost

DAYS = 730
HORIZON_H = 24
MAN = _R / "manifests"
COINS = ["btc", "eth", "sol"]


def _ccxt_close(exchange, symbol):
    try:
        df = fetch_ohlcv_ccxt(exchange, symbol, DAYS, timeframe="1h")
        if df is not None and not df.empty:
            return df["close"]
    except Exception as exc:  # noqa: BLE001
        print(f"  {exchange} {symbol}: FAILED {exc}")
    return None


def main():
    print("fetch USDT/USD (Coinbase, shared)...")
    r_series = _ccxt_close("coinbase", "USDT/USD")
    if r_series is None:
        print("  -> R=1 approximation")
        r_series = None  # set per-coin to okx index below

    hdr = f"{'coin/factor':16}{'absIC':>8}{'partIC':>8}{'IC_tr':>8}{'IC_oos':>8}{'net_bps':>9}{'flip':>6}"
    print("\n" + hdr)
    print("-" * len(hdr))

    for coin in COINS:
        okx = fetch_candles(f"{coin.upper()}-USDT", days=DAYS, bar="1H")["close"]
        usd = _ccxt_close("coinbase", f"{coin.upper()}/USD")
        if usd is None:
            print(f"{coin}: USD leg unavailable — skip")
            continue
        r = r_series if r_series is not None else pd.Series(1.0, index=okx.index)

        out = cross_venue_premium_factors(okx, usd, r)
        ret = add_forward_returns(pd.DataFrame({"close": okx}), "close", [HORIZON_H], interval="1H")[f"ret_{HORIZON_H}h"]

        controls = []
        try:
            feats = pd.read_parquet(MAN / f"features_{coin}.parquet")
            controls = [feats[c].reindex(okx.index) for c in ("funding_z", "basis_rel") if c in feats.columns]
        except Exception as exc:  # noqa: BLE001
            print(f"  ({coin} controls unavailable: {exc})")

        split = int(len(okx) * 0.7)
        for fac in ("depeg_z", "fiat_prem_z"):
            f = out[fac]
            if f.notna().sum() == 0:
                print(f"{coin}/{fac:10} REFUSED (guard)")
                continue
            absic = compute_ic(f, ret)
            pic = partial_ic(f, controls, ret) if controls else float("nan")
            ic_tr = compute_ic(f.iloc[:split], ret.iloc[:split])
            ic_oos = compute_ic(f.iloc[split:], ret.iloc[split:])
            net = decile_spread_net_of_cost(f, ret, slippage_bps=7.5) * 1e4
            flip = sign_flip_rate(f)
            print(f"{coin}/{fac:10}{absic:>8.3f}{pic:>8.3f}{ic_tr:>8.3f}{ic_oos:>8.3f}{net:>9.1f}{flip:>6.2f}")


if __name__ == "__main__":
    main()
