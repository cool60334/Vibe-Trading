"""Foundry -> pipeline bridge.

Foundry vets factors but stores only PRE-OOS values (the OOS window is
deliberately reserved), so its frozen values cannot back a walk-forward
backtest. The bridge therefore re-runs each candidate's PERSISTED CODE over the
full span, reconciles the pre-oos slice against what Foundry stored, and hands
the result to the pipeline as an overlay.

It NEVER writes production features_<sym>.parquet — promote.py stays the only
path into what the live trader reads.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FOUNDRY_PREFIX = "foundry_"


def recompute_full_span(code: str, panel: pd.DataFrame, run_sandbox) -> pd.Series:
    """Re-run a forged factor over the WHOLE panel (train + OOS).

    `run_sandbox(code, panel) -> pd.Series` is injected (real DockerSandbox in
    production, a fake in tests) so the bridge never imports docker directly.
    """
    series = run_sandbox(code, panel)
    series = pd.Series(series, index=panel.index) if not isinstance(series, pd.Series) else series
    if not series.index.equals(panel.index):
        series = series.reindex(panel.index)
    return series


def reconciles_pre_oos(recomputed: pd.Series, stored: pd.Series,
                       oos_start: str, atol: float = 1e-8) -> bool:
    """Does the re-run reproduce what Foundry stored on the pre-oos window?

    A mismatch means the code is not reproducible in this environment — a
    non-causal residue that only shows up once future rows exist, or a drifted
    sandbox image. Either way the factor must be dropped, not trusted.

    equal_nan=True is REQUIRED: a rolling factor is NaN through its warm-up
    window, and the numpy default (equal_nan=False) would report every such
    factor as a mismatch and silently kill it. (forge's own pit_check_via_sandbox
    compares with equal_nan=True for exactly this reason.)
    """
    cutoff = pd.Timestamp(oos_start)
    if cutoff.tz is None:
        cutoff = cutoff.tz_localize(recomputed.index.tz)
    left = recomputed[recomputed.index < cutoff]
    right = stored.reindex(left.index)
    if left.empty:
        return False
    return bool(np.allclose(left.to_numpy(dtype="float64"),
                            right.to_numpy(dtype="float64"),
                            atol=atol, rtol=0.0, equal_nan=True))
