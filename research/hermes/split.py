"""Foundry train/val split — locked to pre-oos_start data only.

The pipeline's walk-forward OOS window (index >= oos_start) is reserved and
must never be seen during factor discovery; final_holdout.py is the only
path allowed to touch it. Foundry carves train/val ONLY from pre-oos rows,
chronologically (no shuffle — crypto is autocorrelated).
"""
from __future__ import annotations

import pandas as pd


class OOSLeakError(AssertionError):
    """Raised in strict mode when the input already contains OOS rows."""


def foundry_split(
    df: pd.DataFrame,
    oos_start: str,
    val_frac: float = 0.2,
    strict: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (train, val), both strictly before oos_start, val chronologically last."""
    if not 0 < val_frac < 1:
        raise ValueError(f"val_frac must be in (0,1), got {val_frac}")
    cutoff = pd.Timestamp(oos_start)
    pre = df[df.index < cutoff]
    if strict and len(pre) != len(df):
        raise OOSLeakError(
            f"{len(df) - len(pre)} rows >= oos_start {oos_start} leaked into split input"
        )
    if pre.empty:
        raise ValueError(f"no rows before oos_start {oos_start}")
    n_val = round(val_frac * len(pre))
    split_at = len(pre) - n_val
    return pre.iloc[:split_at], pre.iloc[split_at:]
