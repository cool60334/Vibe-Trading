"""intrabar_stop_audit.py — diagnose backtest stop-loss optimism (pure).

Reads nothing; operates on already-loaded artifacts. See
docs/superpowers/specs/2026-07-02-intrabar-honest-stop-design.md (v5).
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional

import pandas as pd


@dataclasses.dataclass(frozen=True)
class TradeWindow:
    """One reconstructed trade, as integer bar positions in the run's ohlcv.

    direction : +1 long / -1 short
    entry_idx : first held bar (filled at its open)
    end_idx   : last held bar
    exit_idx  : first flat/opposite bar (exit filled at its open), or None when the
                run reaches the final bar (force-closed at that bar's close).
    """

    direction: int
    entry_idx: int
    end_idx: int
    exit_idx: Optional[int]


def _sign(v: float) -> int:
    if v > 1e-9:
        return 1
    if v < -1e-9:
        return -1
    return 0


def reconstruct_trades(position: pd.Series) -> List[TradeWindow]:
    """Reconstruct trade windows from a single-symbol held-position Series.

    A trade is a maximal run of same-sign consecutive bars. Values are the
    engine's shifted, held target (``positions.csv``), i.e. ``+-size_mult`` or 0.
    """
    signs = [_sign(float(v)) for v in position.to_numpy()]
    n = len(signs)
    windows: List[TradeWindow] = []
    i = 0
    while i < n:
        s = signs[i]
        if s == 0:
            i += 1
            continue
        start = i
        while i + 1 < n and signs[i + 1] == s:
            i += 1
        end = i
        exit_idx = end + 1 if end + 1 < n else None
        windows.append(TradeWindow(direction=s, entry_idx=start, end_idx=end, exit_idx=exit_idx))
        i = end + 1
    return windows
