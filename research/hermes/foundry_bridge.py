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
