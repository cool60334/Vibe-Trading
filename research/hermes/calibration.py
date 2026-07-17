"""Controls that measure what the statistical gate can actually see.

The gate's minimum detectable alpha was an annualised Sharpe of ~5 -- a level
essentially nothing in any market reaches -- so every negative result the project
produced measured the tools rather than the market. Nobody noticed for the
project's whole life, because the gate was only ever asked to reject things, and
it did that beautifully. Catching dead fish proves the guards work; it says
nothing about whether the net can land a live one.

These are the two questions that must be asked together:
  positive control -- how weak an alpha can this gate still see?
  negative control -- how often does it wave through something with no alpha?

Sensitivity alone is worthless as a target: deleting DSR and raising max_turnover
to 10 would drop the minimum detectable Sharpe to 0.5 and leave a net full of
holes. The negative control is what makes "we fixed it" falsifiable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Shift lengths for the negative control. Every one of these must be prime, must
# not divide a year, must clear the longest rolling window (30d), and must sit
# well off the quarterly/semi-annual/annual cycles.
#
# The first draft used +90 days -- exactly one quarter, against a market with
# quarterly expiry and funding cycles. A control shifted by a whole quarter can
# ALIGN with the periodicity it was meant to destroy, keep real predictive power,
# and quietly understate the rejection rate.
PRIME_SHIFT_DAYS: tuple = (107, 149, 227, 311)


def circular_shift(s: pd.Series, days: int, bars_per_day: int = 24) -> pd.Series:
    """Roll a series forward by `days`, wrapping the tail back to the head.

    Circular, not truncating: the control must keep the full sample length so its
    rejection rate is comparable to a real factor's evaluation.

    Shift, never shuffle. Shuffling destroys the autocorrelation, which sends
    turnover through the roof and turns the control into exactly the too-clean
    noise this whole design exists to avoid. The autocorrelation IS the value here:
    it is what makes the control look like a real factor to the gate.
    """
    return pd.Series(np.roll(s.to_numpy(), days * bars_per_day), index=s.index)


def plant_alpha(fwd: pd.Series, w: float, seed: int = 7,
                smooth_bars: int = 24) -> pd.Series:
    """A factor of known strength: `w` of real foresight, `1-w` of noise.

    `fwd.shift(-1)` because evaluate() applies its own shift(entry_lag=1).

    Smoothing is applied to the WHOLE blend, not just the noise. The signal term
    z(fwd.shift(-1)) is itself a nearly-white, jagged series -- 1H returns barely
    autocorrelate -- so leaving it jagged sends turnover past the physical ceiling
    as w rises, and the control ends up measuring max_turnover instead of DSR.
    Smoothed whole, turnover lands at 0.15-0.17, inside the range real factors
    occupy (0.02-0.39).
    """
    rng = np.random.default_rng(seed)
    truth = fwd.shift(-1).fillna(0.0)
    z = (truth - truth.mean()) / truth.std()
    noise = pd.Series(rng.standard_normal(len(fwd)), index=fwd.index)
    raw = z * w + noise * (1.0 - w)
    return raw.rolling(smooth_bars, min_periods=smooth_bars // 2).mean()
