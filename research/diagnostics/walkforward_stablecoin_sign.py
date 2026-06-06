"""
Walk-forward / period-stability validation of the ZERO-parameter sign(stablecoin_z)
long/short daily rule.

Because the rule has no tunable parameters, classic train/validate walk-forward is
moot (nothing to fit). The honest test is period stability: slice the full daily
history into consecutive windows and check the rule earns *consistently* across
them — not just in one lucky stretch. We benchmark each window against buy&hold so
we can see whether the edge holds in both up and down periods.

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


def pnl_series(d: pd.DataFrame, position: pd.Series) -> pd.Series:
    pos = position.shift(1).fillna(0.0)            # no lookahead
    turn = pos.diff().abs().fillna(pos.abs())
    return (pos * d["ret"] - turn * COST).dropna()


def sharpe(p: pd.Series) -> float:
    if len(p) < 10 or p.std(ddof=0) == 0:
        return float("nan")
    return np.sqrt(365) * p.mean() / p.std(ddof=0)


def ret(p: pd.Series) -> float:
    return (1 + p).prod() - 1


def main() -> None:
    d = daily_frame()
    sig = np.sign(d["z"])                            # +1 / -1 rule, parameter-free
    strat = pnl_series(d, sig)
    bh = pnl_series(d, pd.Series(1.0, index=d.index))

    # ── Consecutive 6-month windows ──────────────────────────────────────────
    print("=== Consecutive 6-month windows: sign(z) L/S vs buy&hold ===")
    print(f"{'window':<20}{'days':>5}{'strat_sh':>10}{'strat_ret':>11}{'bh_sh':>9}{'bh_ret':>10}")
    win = pd.Grouper(freq="6MS")
    rows = []
    for (label, sp), (_, bp) in zip(strat.groupby(win), bh.groupby(win)):
        if len(sp) < 20:
            continue
        ss, sr, bs, br = sharpe(sp), ret(sp), sharpe(bp), ret(bp)
        rows.append((ss, sr, bs, br))
        beat = "  <-- beats B&H" if (ss == ss and bs == bs and ss > bs) else ""
        print(f"{str(label.date()):<20}{len(sp):>5}{ss:>+10.2f}{sr:>+11.1%}{bs:>+9.2f}{br:>+10.1%}{beat}")

    ss_arr = np.array([r[0] for r in rows])
    sr_arr = np.array([r[1] for r in rows])
    bs_arr = np.array([r[2] for r in rows])
    nwin = len(rows)
    print(f"\nwindows: {nwin}")
    print(f"strat sharpe>0 : {(ss_arr > 0).sum()}/{nwin}")
    print(f"strat ret>0    : {(sr_arr > 0).sum()}/{nwin}")
    print(f"strat beats B&H: {(ss_arr > bs_arr).sum()}/{nwin}")
    print(f"strat sharpe  median={np.nanmedian(ss_arr):+.2f}  min={np.nanmin(ss_arr):+.2f}  max={np.nanmax(ss_arr):+.2f}")

    # ── Rolling 365-day sharpe stability ─────────────────────────────────────
    roll = strat.rolling(365)
    rs = np.sqrt(365) * roll.mean() / roll.std(ddof=0)
    rs = rs.dropna()
    print("\n=== Rolling 365d sharpe (stability) ===")
    print(f"observations   : {len(rs)}")
    print(f"fraction >0    : {(rs > 0).mean():.0%}")
    print(f"min / median / max : {rs.min():+.2f} / {rs.median():+.2f} / {rs.max():+.2f}")

    print("\n=== Full-period sign(z) L/S ===")
    print(f"sharpe={sharpe(strat):+.2f}  ret={ret(strat):+.1%}  "
          f"vs buy&hold sharpe={sharpe(bh):+.2f} ret={ret(bh):+.1%}")


if __name__ == "__main__":
    main()
