"""Pure GO/NO-GO verdict for an intraday OI/positioning factor.

The decisive gate is incremental IC vs the factor's own causal 1H value: if the
intraday factor adds ~nothing beyond the slow 1H signal, it's NO_GO regardless of
raw IC. Per-coin only (positioning is coin-specific — no cross-coin gate).
"""
from __future__ import annotations

import math

MIN_ABS_IC = 0.03            # consistent with the stage-1 screening gate
INTRADAY_PEAK_MAX_H = 24.0   # an edge peaking beyond this is the slow signal, not intraday


def classify_factor(
    decay: dict[float, float],
    incr_vs_1h: float,
    peak_h: float | None,
    min_abs_ic: float = MIN_ABS_IC,
    intraday_peak_max_h: float = INTRADAY_PEAK_MAX_H,
) -> tuple[str, list[str]]:
    """decay: {horizon_hours -> IC}. incr_vs_1h: incremental IC vs own causal 1H.
    peak_h: horizon (hours) of max |IC|."""
    ics = [abs(v) for v in decay.values() if v is not None and not math.isnan(v)]
    max_ic = max(ics) if ics else 0.0
    if max_ic < min_abs_ic:
        return "NO_GO", [f"max |IC| {max_ic:.3f} < {min_abs_ic}"]
    if incr_vs_1h is None or math.isnan(incr_vs_1h) or abs(incr_vs_1h) < min_abs_ic:
        return "NO_GO", [
            f"incremental IC vs own 1H {incr_vs_1h:.3f} ~ 0 — intraday adds nothing beyond the 1H signal"
        ]
    if peak_h is None or peak_h > intraday_peak_max_h:
        return "NO_GO", [f"IC peaks at {peak_h}h — slow signal in disguise, not intraday"]
    return "GO", [
        f"max |IC| {max_ic:.3f}, incremental vs 1H {incr_vs_1h:.3f}, peak {peak_h}h — worth a cost-aware backtest"
    ]
