# research/lib/factor_gates.py
"""Phase-1 factor validation gates (design spec §6).

Reusable, side-effect-free helpers. The entry-lag gate is NOT here — it reuses
lib.orderflow_eval.execution_ic (already generic). These cover the gates that
did not previously exist: partial IC, turnover, net-of-cost decile spread.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lib.factor_metrics import compute_ic


def partial_ic(
    factor: pd.Series,
    controls: list[pd.Series],
    fwd_ret: pd.Series,
    method: str = "spearman",
) -> float:
    """IC of `factor` residualised against `controls` vs forward return.

    Linearly removes the controls (OLS with intercept) from the factor, then
    computes IC of the residual. A near-zero result means the factor is a
    re-skin of the controls (e.g. funding_z / basis_rel). Spec gate (b).
    """
    cols = {"f": factor, "r": fwd_ret}
    for i, c in enumerate(controls):
        cols[f"c{i}"] = c
    df = pd.concat(cols, axis=1).dropna()
    if len(df) < 50:
        return float("nan")
    X = np.column_stack([np.ones(len(df))] + [df[f"c{i}"].values for i in range(len(controls))])
    beta, *_ = np.linalg.lstsq(X, df["f"].values, rcond=None)
    resid = pd.Series(df["f"].values - X @ beta, index=df.index)
    return compute_ic(resid, df["r"], method=method)


def sign_flip_rate(factor: pd.Series) -> float:
    """Fraction of bars where the factor sign flips (turnover proxy). Spec gate (d).

    High value => the factor oscillates around 0 and fees eat the alpha.
    """
    s = np.sign(factor.dropna())
    if len(s) < 2:
        return float("nan")
    return float((s.diff().abs() > 0).mean())


def decile_spread_net_of_cost(
    factor: pd.Series,
    fwd_ret: pd.Series,
    slippage_bps: float = 7.5,
    q: float = 0.1,
) -> float:
    """|top-decile minus bottom-decile forward return| minus round-trip cost.

    A cheap economic check (spec gate (e)): if this is <= 0 the factor's spread
    does not survive slippage, so Phase 2 is not worth it. `slippage_bps` is the
    one-way cost; round trip = 2x.
    """
    df = pd.concat({"f": factor, "r": fwd_ret}, axis=1).dropna()
    if len(df) < 50:
        return float("nan")
    lo = df["f"].quantile(q)
    hi = df["f"].quantile(1.0 - q)
    top = df.loc[df["f"] >= hi, "r"].mean()
    bot = df.loc[df["f"] <= lo, "r"].mean()
    gross = abs(top - bot)
    cost = 2.0 * slippage_bps / 1e4
    return float(gross - cost)
