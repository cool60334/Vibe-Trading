"""Point-in-time guard: a feature value at row t must not depend on rows > t.

Perturbation harness (stronger than AST): corrupt all rows >= perturb_from,
recompute, and assert every value BEFORE perturb_from is unchanged. Any
future leak — negative shift, bfill/backfill, .iloc[t+1] — shifts an earlier
value and is caught. Reused idea from agent/tests/factors/test_lookahead.py,
adapted to single-symbol OHLCV research frames.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.hermes.errors import HermesGuardError

PROBE_FROM_DEFAULT = 101   # rows [0, perturb_from) must be invariant
PERTURB_GAP = 10           # first corrupted row = probe_end + gap


class LookaheadError(HermesGuardError, AssertionError):
    """Raised when a feature's past values change after the future is corrupted."""


def assert_no_lookahead(
    compute_fn,
    df: pd.DataFrame,
    perturb_from: int | None = None,
    atol: float = 1e-9,
    rtol: float = 1e-9,
) -> None:
    """Return None if causal; raise LookaheadError if the feature peeks ahead.

    compute_fn(df) -> pd.Series aligned to df.index.
    """
    n = len(df)
    if perturb_from is None:
        perturb_from = min(PROBE_FROM_DEFAULT, n - PERTURB_GAP - 1)
    if perturb_from <= 0 or perturb_from >= n:
        raise ValueError(f"perturb_from {perturb_from} out of range for n={n}")

    if not all(pd.api.types.is_numeric_dtype(dt) for dt in df.dtypes):
        raise ValueError("assert_no_lookahead requires an all-numeric df; got dtypes: " + str(dict(df.dtypes)))

    base = np.asarray(compute_fn(df), dtype="float64")[:perturb_from]

    corrupt = df.copy()
    corrupt.iloc[perturb_from:] = 1e10
    # alternate NaN into half the corrupted block so both sentinels are exercised
    corrupt.iloc[perturb_from + PERTURB_GAP:] = np.nan
    after = np.asarray(compute_fn(corrupt), dtype="float64")[:perturb_from]

    if not np.allclose(base, after, atol=atol, rtol=rtol, equal_nan=True):
        drift = np.nanmax(np.abs(base - after))
        raise LookaheadError(
            f"feature peeks into the future: max drift {drift:.3e} in rows "
            f"[0,{perturb_from}) after corrupting rows >= {perturb_from}"
        )
