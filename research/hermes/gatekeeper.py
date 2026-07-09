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
