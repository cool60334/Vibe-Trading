"""Interval-correct intraday OI / positioning factor builders for the recon.

All hour-anchored windows scale by lib.timeframe.bars_per_hour so the same
window_h means the same wall-clock span at 15m / 30m / 1H. Pure functions; no
network, no production-pipeline dependency.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lib.timeframe import bars_per_hour


def _rolling_z(s: pd.Series, window_bars: int) -> pd.Series:
    m = s.rolling(window_bars, min_periods=window_bars // 2).mean()
    sd = s.rolling(window_bars, min_periods=window_bars // 2).std()
    # A zero-std (constant) window makes the z-score undefined, not infinite —
    # inf would survive dropna() and crash downstream lstsq (incremental_ic).
    return ((s - m) / sd).replace([np.inf, -np.inf], np.nan)


def ls_factors(oi_df: pd.DataFrame, interval: str, window_h: int = 36) -> dict[str, pd.Series]:
    """Short-window L/S positioning z-scores (the live-feed-deployable subset)."""
    w = window_h * bars_per_hour(interval)
    out: dict[str, pd.Series] = {}
    if "global_ls_accounts" in oi_df:
        out["global_ls_z_s"] = _rolling_z(oi_df["global_ls_accounts"], w)
    if "toptrader_ls_positions" in oi_df:
        out["toptrader_ls_z_s"] = _rolling_z(oi_df["toptrader_ls_positions"], w)
    if "global_ls_z_s" in out and "toptrader_ls_z_s" in out:
        out["ls_divergence_s"] = out["global_ls_z_s"] - out["toptrader_ls_z_s"]
    return out


def oi_velocity_factors(oi_df: pd.DataFrame, close: pd.Series, interval: str) -> dict[str, pd.Series]:
    """OI-velocity factors — RESEARCH-ONLY (archive-only, no live feed -> not
    intraday-deployable). Magnitude form of oi_price_div (not sign)."""
    bph = bars_per_hour(interval)
    oi = oi_df["oi"]
    mom = oi.pct_change(1 * bph)
    return {
        "oi_mom_1h": mom,
        "oi_accel": mom.diff(1 * bph),
        "oi_price_div": oi.pct_change(1 * bph) * close.pct_change(1 * bph),
    }


# Factors with a live 5-min feed (oi_metrics._LIVE_LS_COLS) — the only intraday-deployable set.
DEPLOYABLE_FACTORS = frozenset({"global_ls_z_s", "toptrader_ls_z_s", "ls_divergence_s"})


def build_intraday_oi_factors(oi_df: pd.DataFrame, close: pd.Series, interval: str) -> dict[str, pd.Series]:
    f = ls_factors(oi_df, interval)
    f.update(oi_velocity_factors(oi_df, close, interval))
    return f


def causal_1h_control(factor_1h: pd.Series, intraday_index: pd.Index) -> pd.Series:
    """The 1H factor reindexed onto the intraday grid WITHOUT look-ahead.

    A left-labeled 1H bar stamped at hour H spans [H, H+1h) and is only known at
    H+1h (its close). Shift by one 1H bar so the value first becomes available at
    H+1h, then forward-fill onto the intraday grid. An intraday bar inside hour H
    therefore sees hour (H-1)'s value, never hour H's.
    """
    return factor_1h.shift(1).reindex(intraday_index, method="ffill")
