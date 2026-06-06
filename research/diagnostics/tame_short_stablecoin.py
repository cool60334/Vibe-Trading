"""
Tame the short side of the sign(stablecoin_z) rule.

The only catastrophic episode (2022-12 window: BTC +58%, rule -46%) comes from
taking a FULL short when z<0 during a sharp rally. We cap short exposure to -k
(k in 0..1) while keeping full long (+1). k=1 is the original symmetric rule,
k=0 is long-only.

Discipline: the 2022-12 blowup lives in the IS period (< 2025-01-01), so choosing
k to control that tail is an IS-side risk decision. OOS (>= 2025-01-01) stays held
out to confirm the bear-market alpha survives the cap. We do NOT pick k by
maximising OOS — we show the whole trade-off curve.

Offline; mutates nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_RESEARCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RESEARCH))
from diagnostics.robust_stablecoin_strategy import daily_frame, COST  # noqa: E402

SPLIT = pd.Timestamp("2025-01-01", tz="UTC")
KS = [0.0, 0.25, 0.5, 0.75, 1.0]


def pnl_series(d: pd.DataFrame, position: pd.Series) -> pd.Series:
    pos = position.shift(1).fillna(0.0)
    turn = pos.diff().abs().fillna(pos.abs())
    return (pos * d["ret"] - turn * COST).dropna()


def sharpe(p: pd.Series) -> float:
    if len(p) < 10 or p.std(ddof=0) == 0:
        return float("nan")
    return np.sqrt(365) * p.mean() / p.std(ddof=0)


def maxdd(p: pd.Series) -> float:
    eq = (1 + p).cumprod()
    return (eq / eq.cummax() - 1).min()


def worst_6m(p: pd.Series) -> float:
    g = p.groupby(pd.Grouper(freq="6MS")).apply(lambda s: (1 + s).prod() - 1 if len(s) >= 20 else np.nan)
    return g.min()


def position_for_k(z: pd.Series, k: float) -> pd.Series:
    pos = np.sign(z).astype(float)
    pos[pos < 0] = -k                      # cap short exposure
    return pos


def main() -> None:
    d = daily_frame()
    z = d["z"]
    is_d = d[d.index < SPLIT]
    oos_d = d[d.index >= SPLIT]

    print(f"IS {is_d.index.min().date()}..{(SPLIT - pd.Timedelta(days=1)).date()} ({len(is_d)}d)   "
          f"OOS {SPLIT.date()}..{d.index.max().date()} ({len(oos_d)}d)\n")
    print(f"{'short_cap':<10}{'IS_sh':>8}{'IS_worst6m':>12}{'IS_mdd':>9}"
          f"{'  ':>3}{'OOS_sh':>8}{'OOS_ret':>9}{'  ':>3}{'full_sh':>9}{'full_ret':>10}")

    for k in KS:
        pos = position_for_k(z, k)
        p_is = pnl_series(is_d, pos.reindex(is_d.index))
        p_oos = pnl_series(oos_d, pos.reindex(oos_d.index))
        p_full = pnl_series(d, pos)
        tag = f"-{k:g}"
        print(f"{tag:<10}{sharpe(p_is):>+8.2f}{worst_6m(p_is):>+12.1%}{maxdd(p_is):>+9.1%}"
              f"{'  ':>3}{sharpe(p_oos):>+8.2f}{ (1+p_oos).prod()-1:>+9.1%}"
              f"{'  ':>3}{sharpe(p_full):>+9.2f}{(1+p_full).prod()-1:>+10.1%}")

    bh = pnl_series(d, pd.Series(1.0, index=d.index))
    bh_oos = pnl_series(oos_d, pd.Series(1.0, index=oos_d.index))
    print(f"\nbuy&hold   full_sh={sharpe(bh):+.2f}  full_ret={(1+bh).prod()-1:+.1%}   "
          f"OOS_sh={sharpe(bh_oos):+.2f}  OOS_ret={(1+bh_oos).prod()-1:+.1%}")
    print("\nshort_cap=-1.0 is the original symmetric rule; -0.0 is long-only.")


if __name__ == "__main__":
    main()
