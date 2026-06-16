"""Economic-validation metrics for order-flow factors. Pure functions on
aligned (factor, price) Series. No pipeline dependency.

Key metrics
-----------
decay_profile : Spearman IC of the factor vs k-bar-forward return, for
    k = 1..max_bars.  Tells us how fast predictive power falls off.
half_life_bars : First k where |IC(k)| < |IC(1)| / 2.  If the half-life is
    short (e.g. 1-2 bars at 15m) the signal must be traded as a *taker*;
    if it is long enough that a limit order is likely to fill first, maker
    orders may be viable and the fee math changes substantially.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def _fwd_return(price: pd.Series, k: int) -> pd.Series:
    """k-bar forward return: price[t+k] / price[t] - 1."""
    return price.shift(-k) / price - 1.0


def _ic(factor: pd.Series, fwd: pd.Series, min_n: int = 30) -> float:
    """Spearman rank correlation between factor and fwd return.

    Returns NaN when fewer than `min_n` aligned (non-NaN) rows are available.
    The default min_n=30 guards against spurious IC on tiny real samples;
    pass a smaller value in tests so small fixtures can verify behavior.
    """
    df = pd.concat([factor, fwd], axis=1).dropna()
    if len(df) < min_n:
        return float("nan")
    return float(spearmanr(df.iloc[:, 0], df.iloc[:, 1]).correlation)


def decay_profile(
    factor: pd.Series,
    price: pd.Series,
    max_bars: int,
    min_obs: int = 30,
) -> dict[int, float]:
    """Spearman IC of `factor` vs k-bar-forward return, k = 1..max_bars.

    Parameters
    ----------
    factor:
        Signal Series aligned to `price`.
    price:
        Close-price Series (same index as `factor`).
    max_bars:
        Maximum forward-bar horizon to evaluate.
    min_obs:
        Minimum number of aligned non-NaN rows required to return a real IC
        rather than NaN.  Default 30 (production guard).  Pass a smaller value
        (e.g. min_obs=3) in unit tests with tiny synthetic fixtures.

    Returns
    -------
    dict mapping bar-offset k -> Spearman IC float (NaN when n < min_obs).
    """
    return {
        k: _ic(factor, _fwd_return(price, k), min_n=min_obs)
        for k in range(1, max_bars + 1)
    }


def half_life_bars(profile: dict[int, float]) -> int | None:
    """First k where |IC(k)| drops below half the 1-bar |IC|.

    Returns None when:
    - profile is empty
    - IC(1) is NaN
    - |IC| never falls below half the 1-bar value across all evaluated bars

    A short half-life (e.g. 1-2 bars at 15m intervals) means the edge
    evaporates within ~15-30 min — maker orders may not fill in time and
    taker fees become the binding cost constraint.
    """
    if not profile or np.isnan(profile.get(1, float("nan"))):
        return None
    ic1 = abs(profile[1])
    threshold = ic1 / 2.0
    for k in sorted(profile):
        if abs(profile[k]) < threshold:
            return k
    return None


def quantile_returns(
    factor: pd.Series, price: pd.Series, fwd_bars: int, n_q: int
) -> pd.Series:
    """Mean k-bar-forward return per factor quantile bucket (0..n_q-1).

    Parameters
    ----------
    factor:
        Signal Series aligned to `price`.
    price:
        Close-price Series (same index as `factor`).
    fwd_bars:
        Number of bars ahead to measure the forward return.
    n_q:
        Number of quantile buckets.  Note: if `factor` has many duplicate
        values, ``pd.qcut`` may yield fewer than ``n_q`` buckets.

    Returns
    -------
    pd.Series indexed by quantile label (integer 0..n_q-1), values are mean
    forward returns.  A monotonically increasing series indicates that higher
    factor values predict higher subsequent returns.
    """
    fwd = _fwd_return(price, fwd_bars)
    df = pd.concat([factor.rename("f"), fwd.rename("r")], axis=1).dropna()
    df["q"] = pd.qcut(df["f"], n_q, labels=False, duplicates="drop")
    return df.groupby("q")["r"].mean()


def incremental_ic(factor: pd.Series, control: pd.Series, price: pd.Series, fwd_bars: int) -> float:
    """IC of `factor` residualized on `control` vs forward return.

    Computes the marginal predictive contribution of `factor` beyond what `control`
    already explains. The factor is projected onto the control via OLS (+intercept),
    and the residual is correlated (Spearman) against the fwd_bars-ahead return.

    When `control` is a constant Series (e.g. pd.Series(0.0, ...)), the OLS
    design matrix is rank-deficient but numpy lstsq returns the least-norm solution;
    the fitted values are constant, so the residual equals the demeaned factor.
    Spearman rank correlation is mean-invariant, so the result equals the raw
    standalone factor IC.
    """
    df = pd.concat([factor.rename("f"), control.rename("c")], axis=1).dropna()
    # OLS residual of f on c (+intercept)
    c = df["c"].values
    A = np.vstack([c, np.ones_like(c)]).T
    coef, *_ = np.linalg.lstsq(A, df["f"].values, rcond=None)
    resid_vals = df["f"].values - A @ coef
    # Guard: near-constant residual means the factor is fully explained by the
    # control — no orthogonal component exists, so incremental IC is 0.
    if np.std(resid_vals) < 1e-12:
        return 0.0
    resid = pd.Series(resid_vals, index=df.index)
    return _ic(resid, _fwd_return(price, fwd_bars))


def execution_ic(
    factor: pd.Series,
    price: pd.Series,
    entry_lag_bars: int,
    hold_bars: int,
    min_obs: int = 30,
) -> float:
    """IC of factor vs the return of entering `entry_lag_bars` after the signal
    bar's close and holding `hold_bars`. entry_lag_bars=0 == enter at the signal
    bar's close (the factor-known time). A same-bar lookahead would show a large
    IC drop from lag 0 to lag 1; a real signal persists.

    Parameters
    ----------
    factor:
        Signal Series aligned to `price`.
    price:
        Close-price Series (same index as `factor`).
    entry_lag_bars:
        Number of bars after the signal bar before the trade enters.
        0 = enter immediately at the signal bar's close (tests for lookahead).
        1 = enter one bar later (realistic execution without lookahead).
    hold_bars:
        Number of bars the trade is held before exit.
    min_obs:
        Minimum aligned non-NaN rows required; returns NaN below this.

    Returns
    -------
    Spearman IC float (NaN when n < min_obs).
    """
    entry = price.shift(-entry_lag_bars)
    exit_ = price.shift(-(entry_lag_bars + hold_bars))
    fwd = exit_ / entry - 1.0
    return _ic(factor, fwd, min_n=min_obs)
