"""
Robustness test for the stablecoin_supply_z factor with a PARAMETER-LIGHT,
UNTUNED daily rule — the opposite of the IS-tuned percentile/SL/TP strategy whose
OOS Sharpe (0.27) was carried by ~3 lucky trades.

Question: does the factor produce a *broad-based* edge that survives out-of-sample
with a dead-simple rule we never optimised?

Method (no lookahead, costs included):
  - Daily grid. Signal = stablecoin_supply_z resampled to daily (last).
  - Position decided from z on day t, earns day t+1 return (position.shift(1)).
  - Variants tested with FIXED, non-cherry-picked thresholds:
      long_only_0   : long if z > 0 else flat
      long_short_0  : +1 if z > 0 else -1
      long_only_1   : long if z > 1 else flat   (robustness to threshold)
  - Cost = turnover * (taker + slippage) per rebalance.
  - Same rule applied identically to IS (< 2025-01-01) and OOS (>= 2025-01-01).

Robustness checks: Sharpe must be positive in BOTH windows, and OOS profit must
be broad-based (low share from the best 3 days, many profitable months).
Offline; mutates nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_RESEARCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RESEARCH))
from lib.factor_io import load_features  # noqa: E402

OHLCV = _RESEARCH.parent / "runs" / "btc_s1_multi_factor_consensus_sweep_000" / "artifacts" / "ohlcv_BTC-USDT-SWAP.csv"
SPLIT = pd.Timestamp("2025-01-01", tz="UTC")
COST = 0.0005 + 0.001  # taker + slippage, per side of turnover


def daily_frame() -> pd.DataFrame:
    f = load_features("btc")
    if f.index.tz is None:
        f.index = f.index.tz_localize("UTC")
    px = pd.read_csv(OHLCV, parse_dates=["trade_date"]).set_index("trade_date").sort_index()
    if px.index.tz is None:
        px.index = px.index.tz_localize("UTC")
    close = px["close"].reindex(f.index, method="ffill")
    df = pd.DataFrame({"z": f["stablecoin_supply_z"], "close": close})
    d = pd.DataFrame({
        "z": df["z"].resample("1D").last(),
        "close": df["close"].resample("1D").last(),
    }).dropna()
    d["ret"] = d["close"].pct_change()
    return d


def backtest(d: pd.DataFrame, position: pd.Series) -> dict:
    pos = position.shift(1).fillna(0.0)          # enter next day — no lookahead
    turn = pos.diff().abs().fillna(pos.abs())
    pnl = pos * d["ret"] - turn * COST
    pnl = pnl.dropna()
    if pnl.std(ddof=0) == 0 or len(pnl) < 20:
        return {"sharpe": float("nan"), "ret": float("nan"), "n": len(pnl)}
    sharpe = np.sqrt(365) * pnl.mean() / pnl.std(ddof=0)
    eq = (1 + pnl).cumprod()
    mdd = (eq / eq.cummax() - 1).min()
    # broad-based check: share of total log-growth from best 3 days
    top3 = np.sort(pnl.values)[::-1][:3].sum()
    monthly = pnl.groupby(pnl.index.to_period("M")).sum()
    return {
        "sharpe": sharpe,
        "ret": eq.iloc[-1] - 1,
        "mdd": mdd,
        "n": len(pnl),
        "days_in_mkt": int((pos != 0).sum()),
        "top3_day_pnl_share": top3 / pnl.sum() if pnl.sum() != 0 else float("nan"),
        "months_pos": int((monthly > 0).sum()),
        "months_tot": len(monthly),
    }


def variants(d: pd.DataFrame) -> dict[str, pd.Series]:
    z = d["z"]
    return {
        "long_only_z>0": (z > 0).astype(float),
        "long_short_z>0": np.sign(z).replace(0, 0).astype(float),
        "long_only_z>1": (z > 1).astype(float),
        "buy_hold": pd.Series(1.0, index=d.index),
    }


def fmt(r: dict) -> str:
    if r.get("sharpe") != r.get("sharpe"):
        return f"sharpe=  nan  n={r['n']}"
    return (f"sharpe={r['sharpe']:+.2f}  ret={r['ret']:+.1%}  mdd={r['mdd']:+.1%}  "
            f"days_in={r['days_in_mkt']:>4}  top3day={r['top3_day_pnl_share']:>5.0%}  "
            f"months+={r['months_pos']}/{r['months_tot']}")


def main() -> None:
    d = daily_frame()
    is_d = d[d.index < SPLIT]
    oos_d = d[d.index >= SPLIT]
    print(f"daily rows: total={len(d)}  IS={len(is_d)}  OOS={len(oos_d)}")
    print(f"IS  {d.index.min().date()}..{(SPLIT - pd.Timedelta(days=1)).date()}   "
          f"OOS {SPLIT.date()}..{d.index.max().date()}\n")

    for name, full_pos in variants(d).items():
        print(f"── {name}")
        print(f"   IS   {fmt(backtest(is_d, full_pos.reindex(is_d.index)))}")
        print(f"   OOS  {fmt(backtest(oos_d, full_pos.reindex(oos_d.index)))}")


if __name__ == "__main__":
    main()
