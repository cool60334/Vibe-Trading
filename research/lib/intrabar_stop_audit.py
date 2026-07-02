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


@dataclasses.dataclass(frozen=True)
class AuditResult:
    n_trades: int
    n_breached: int
    breached_pct: float
    n_optimistic: int
    mean_breach_depth_pct: Optional[float]
    max_breach_depth_pct: Optional[float]


def _slip(price: float, direction: int, rate: float) -> float:
    """CryptoEngine.apply_slippage convention: price*(1 + direction*rate)."""
    return price * (1.0 + direction * rate)


def audit_trades(
    windows: List[TradeWindow],
    ohlcv: pd.DataFrame,
    stop_pct: float,
    slippage_rate: float,
    exit_reasons: Optional[List[str]] = None,
) -> AuditResult:
    """Audit reconstructed trades for intrabar stop breaches (see spec v5).

    ohlcv: DataFrame with columns open/high/low/close, integer-positional (.iloc)
    aligned to the same bars the windows index into.
    """
    pct = stop_pct / 100.0
    o = ohlcv["open"].to_numpy()
    h = ohlcv["high"].to_numpy()
    lo = ohlcv["low"].to_numpy()
    c = ohlcv["close"].to_numpy()

    n_trades = len(windows)
    n_breached = 0
    n_optimistic = 0
    depths: List[float] = []

    for k, w in enumerate(windows):
        reason = exit_reasons[k] if exit_reasons and k < len(exit_reasons) else "signal"

        # Stop level off the SIGNAL bar close (the bar before entry), not the fill.
        if w.entry_idx > 0:
            stop_ref = float(c[w.entry_idx - 1])
        else:
            stop_ref = float(o[w.entry_idx])
        stop = stop_ref * (1 - pct) if w.direction == 1 else stop_ref * (1 + pct)

        # Scan the run's own held bars [entry_idx .. end_idx].
        breach_bar: Optional[int] = None
        for b in range(w.entry_idx, w.end_idx + 1):
            if w.direction == 1 and lo[b] <= stop:
                breach_bar = b
                break
            if w.direction == -1 and h[b] >= stop:
                breach_bar = b
                break
        if breach_bar is None:
            continue

        n_breached += 1

        # Depth: deepest adverse excursion past the stop over the held window.
        if w.direction == 1:
            worst = float(min(lo[w.entry_idx:w.end_idx + 1]))
            depth = (stop - worst) / stop * 100.0
        else:
            worst = float(max(h[w.entry_idx:w.end_idx + 1]))
            depth = (worst - stop) / stop * 100.0
        depths.append(depth)

        # Optimism: was the real exit a better directional price than the honest stop?
        if w.direction == 1:
            honest_px = min(stop, float(o[breach_bar]))
        else:
            honest_px = max(stop, float(o[breach_bar]))
        honest_exit = _slip(honest_px, -w.direction, slippage_rate)

        if reason == "signal" and w.exit_idx is not None:
            actual_px = float(o[w.exit_idx])
        else:
            actual_px = float(c[w.end_idx])   # force-close / liquidation fills at close
        actual_exit = _slip(actual_px, -w.direction, slippage_rate)

        # Better directional price for the actual exit => backtest was optimistic.
        if w.direction * honest_exit < w.direction * actual_exit:
            n_optimistic += 1

    breached_pct = (n_breached / n_trades * 100.0) if n_trades else 0.0
    return AuditResult(
        n_trades=n_trades,
        n_breached=n_breached,
        breached_pct=breached_pct,
        n_optimistic=n_optimistic,
        mean_breach_depth_pct=(sum(depths) / len(depths)) if depths else None,
        max_breach_depth_pct=(max(depths)) if depths else None,
    )
