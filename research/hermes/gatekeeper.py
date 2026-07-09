# research/hermes/gatekeeper.py
"""Talos Factor Foundry statistical gatekeeper (Phase 1A, agy-v2).

Deterministic pass/fail on a sandbox-computed factor. Signal quality = Gross IC
(spearman vs raw h-horizon return); cost gating = Net IR/Sharpe from a correctly
aligned 1-period rebalanced net-return stream (NOT overlapping h-period returns).
Turnover from position weights with an absolute ceiling; entry lag enforced
INSIDE evaluate (never trust the caller); regime labels ffill'd from daily;
DSR trial distribution restricted to homogeneous same-interval ledger trials.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def factor_to_weights(factor: pd.Series, span: int = 168) -> pd.Series:
    """Map factor -> target weights in [-1,1] via causal EMA z-score.

    EMA (not SMA) softens window-start instability (agy #5). std==0 runs (a
    constant/discrete signal) are flat by definition — no variance to measure
    extremity against — so z is set to 0 (not NaN) for the duration of the run."""
    mu = factor.ewm(span=span, adjust=False, min_periods=span // 4).mean()
    sd = factor.ewm(span=span, adjust=False, min_periods=span // 4).std(bias=True)
    z = (factor - mu) / sd
    # agy-3 #5-1: replace ONLY the exactly-flat (sd==0) runs with 0.0; warmup
    # NaN (sd is NaN there) must stay NaN — .where(sd>0,0) wrongly zeroed warmup,
    # handing the strategy a premature 0 position + a fake turnover spike.
    z = z.mask(sd == 0, 0.0)
    return z.clip(-1.0, 1.0)


def turnover_of(weights: pd.Series) -> pd.Series:
    """Per-bar turnover = |w_t - w_{t-1}|. NaN weights (EMA warmup / flat gaps)
    are treated as flat (0.0) BEFORE diff so the first real entry from a NaN
    warmup is charged turnover instead of being a free position (agy-3 #1)."""
    return weights.fillna(0.0).diff().abs()


def gross_ic(factor: pd.Series, fwd_ret: pd.Series) -> float:
    """Spearman IC of the factor vs RAW forward return (drops NaN pairs)."""
    paired = pd.concat([factor, fwd_ret], axis=1).dropna()
    if len(paired) < 20:
        return float("nan")
    return float(spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]).statistic)


def net_ir(weights: pd.Series, ret_1period: pd.Series, cost_frac: float) -> float:
    """Per-bar IR/Sharpe of the net return of a 1-period rebalanced position.

    strat_ret_t = weights_t * ret1_{t+1} - turnover_t * cost_frac.
    ret_1period MUST be a TRAILING single-bar return (e.g. close.pct_change(),
    NOT pre-shifted forward) — this function shifts it forward internally.
    Passing an already-forward return double-shifts and misaligns by one bar.
    A single-bar series also avoids h-period overlap (agy #1c: overlap would
    autocorrelate and inflate Sharpe). Cost is paid when the position is set
    at t; the position earns the next bar's return."""
    fwd1 = ret_1period.shift(-1)                       # weights_t earn ret_{t+1}
    tau = turnover_of(weights)
    strat = (weights * fwd1) - tau.fillna(0.0) * cost_frac
    strat = strat.dropna()
    sd = strat.std(ddof=0)
    if len(strat) < 20 or sd <= 0:
        return float("nan")
    return float(strat.mean() / sd)


def nonoverlap_ic(factor: pd.Series, fwd_ret: pd.Series, horizon_bars: int) -> float:
    """IC on non-overlapping subsample (every horizon_bars-th row) so a long
    horizon's overlapping windows don't inflate significance (agy C-4).

    Threshold is 10, not gross_ic's 20: plan spec's own required test
    (n=300, horizon_bars=24) only survives striding with 13 rows, so 20 is
    infeasible here. 10 is the practical floor below which Spearman's
    significance cutoff is so high that |rho| near 1 is expected from pure
    chance rather than signal (agy consult, 2026-07-09)."""
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be >= 1")
    paired = pd.concat([factor, fwd_ret], axis=1).dropna().iloc[::horizon_bars]
    if len(paired) < 10:
        return float("nan")
    return float(spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]).statistic)


def regime_ic(factor: pd.Series, fwd_ret: pd.Series, daily_regime: pd.Series) -> dict:
    """IC within each regime. daily_regime is DAILY (compute_regime output); it is
    ffill'd onto the factor index so hourly factors keep all rows (agy #2)."""
    labels = daily_regime.reindex(factor.index, method="ffill")
    out: dict = {}
    for label in ("bull", "bear", "neutral"):
        mask = labels == label
        if mask.sum() >= 20:
            out[label] = gross_ic(factor[mask], fwd_ret[mask])
    return out


def yearly_ic(factor: pd.Series, fwd_ret: pd.Series) -> dict:
    out: dict = {}
    for year, idx in factor.groupby(factor.index.year).groups.items():
        if len(idx) >= 20:
            out[str(year)] = gross_ic(factor.loc[idx], fwd_ret.reindex(idx))
    return out
