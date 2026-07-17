"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014), normal-returns form.

Pure functions; no IO. Used by Stage 4 to haircut the best train Sharpe by the
breadth of the parameter search (multiple-testing / survivorship correction).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import scipy.stats as st

# Bars per year per pipeline interval — used to de-annualise Sharpe to per-bar.
_BARS_PER_YEAR = {"15m": 35040, "30m": 17520, "1H": 8760, "4H": 2190, "1D": 365}


def bars_per_year(interval: str) -> int:
    """Bars per year for a pipeline interval; defaults to 1H (8760) if unknown."""
    return _BARS_PER_YEAR.get(interval, 8760)


def bars_in_window(start_date: str, end_date: str, interval: str) -> int:
    """Approx bar count between two ISO dates at *interval* (clamped at 0)."""
    d0 = date.fromisoformat(start_date[:10])
    d1 = date.fromisoformat(end_date[:10])
    days = max((d1 - d0).days, 0)
    return int(days * bars_per_year(interval) / 365)


def deflated_sharpe(
    best_sr_per_bar: float,
    trial_srs_per_bar: "np.ndarray | list[float]",
    T: int,
    expected_sr: float = 0.0,
    n_trials: "int | None" = None,
) -> float:
    """Probability the best trial's true per-bar Sharpe exceeds the expected
    maximum Sharpe of N trials under the null (skew=0, kurt=3).

    All Sharpes MUST be per-bar (de-annualised). ``T`` = train-window bar count.
    Returns 1.0 when deflation is undefined (N<2, T<2, zero trial variance) so
    the gate never spuriously fails. Result is a probability in [0, 1].

    ``n_trials`` separates the two things N was doing at once. The multiple-testing
    DEBT is a count -- how many times we cast into this pool -- and it survives
    anything we later learn about the measurements. The trial SR series is a
    VARIANCE sample, and it is only usable while the trials are drawn from one
    distribution. Those two can legitimately diverge: when a batch of past trials
    turns out to have been measured on a different scale (e.g. net-of-cost Sharpe,
    whose spread tracks turnover rather than search noise), its variance must be
    dropped while its count must not. Defaults to the finite sample count, which
    is the old behaviour.

    foundry_dsr's docstring already promised this split ("count comes from the
    ledger ... but the trial SR distribution is kept homogeneous"). It was never
    implementable while n came from srs.size.
    """
    srs = np.asarray(list(trial_srs_per_bar), dtype=float)
    srs = srs[np.isfinite(srs)]
    if n_trials is None:
        n_trials = srs.size
    elif n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    if srs.size < 2 or n_trials < 2 or T < 2:
        return 1.0

    var_srs = float(np.var(srs, ddof=1))
    if var_srs <= 1e-30:
        return 1.0

    gamma = 0.5772156649  # Euler-Mascheroni
    n = n_trials
    max_z = (1 - gamma) * st.norm.ppf(1 - 1.0 / n) + gamma * st.norm.ppf(
        1 - 1.0 / (n * np.e)
    )
    expected_max_sr = expected_sr + np.sqrt(var_srs) * max_z

    sr_std = np.sqrt((1.0 + 0.5 * best_sr_per_bar**2) / (T - 1))
    return float(st.norm.cdf((best_sr_per_bar - expected_max_sr) / sr_std))
