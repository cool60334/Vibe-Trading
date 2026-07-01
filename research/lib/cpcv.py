"""Combinatorial Purged Cross-Validation (Bailey & López de Prado).

Pure functions; no IO. See docs/superpowers/specs/2026-06-26-cpcv-validation-design.md.
"""
from __future__ import annotations

from datetime import date, timedelta
from itertools import combinations

import numpy as np
import pandas as pd


def make_blocks(start_date: str, end_date: str, n_blocks: int) -> list[tuple[str, str]]:
    """Split [start_date, end_date] into n_blocks contiguous equal-day (start, end)
    ISO ranges. Adjacent blocks share a boundary date (block[i].end == block[i+1].start)."""
    if n_blocks < 2:
        raise ValueError(f"n_blocks must be >= 2, got {n_blocks}")
    d0 = date.fromisoformat(start_date[:10])
    d1 = date.fromisoformat(end_date[:10])
    total = (d1 - d0).days
    if total < n_blocks:
        raise ValueError("date range too short for n_blocks")
    edges = [d0 + timedelta(days=round(total * i / n_blocks)) for i in range(n_blocks + 1)]
    return [(edges[i].isoformat(), edges[i + 1].isoformat()) for i in range(n_blocks)]


def combinatorial_splits(n_blocks: int, k_test: int) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """All C(n_blocks, k_test) (train_block_ids, test_block_ids) index tuples."""
    if not 1 <= k_test < n_blocks:
        raise ValueError(f"require 1 <= k_test < n_blocks, got k={k_test}, n={n_blocks}")
    out: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    all_ids = set(range(n_blocks))
    for test in combinations(range(n_blocks), k_test):
        train = tuple(sorted(all_ids - set(test)))
        out.append((train, tuple(test)))
    return out


def pooled_sharpe(block_returns: dict[int, pd.Series], block_ids, bars_per_year: float) -> float:
    """Annualised Sharpe of the concatenated per-bar returns of `block_ids`.
    nan when <2 pooled bars or zero std (undefined / flat)."""
    parts = [block_returns[i] for i in block_ids if i in block_returns and len(block_returns[i])]
    if not parts:
        return float("nan")
    pooled = pd.concat(parts)
    if len(pooled) < 2:
        return float("nan")
    std = float(pooled.std())
    if std == 0.0:
        return float("nan")
    return float(pooled.mean() / std * (bars_per_year ** 0.5))


def purge_boundary_bars(
    block_returns: dict[int, pd.Series], train_ids, test_ids, purge_bars: int, embargo_bars: int
) -> dict[int, pd.Series]:
    """Copy of the TRAIN blocks' returns with boundary rows dropped where a train
    block is adjacent to a test block (ids consecutive => adjacency = id±1):
      - train id precedes a test id (id+1 in test) -> drop TAIL purge_bars (label leak)
      - train id follows a test id  (id-1 in test) -> drop HEAD embargo_bars (serial corr)
      - sandwiched -> both. Non-adjacent train blocks untouched."""
    test_set = set(test_ids)
    out: dict[int, pd.Series] = {}
    for t in train_ids:
        s = block_returns.get(t)
        if s is None:
            continue
        head = embargo_bars if (t - 1) in test_set else 0
        tail = purge_bars if (t + 1) in test_set else 0
        out[t] = s.iloc[head: len(s) - tail] if tail else s.iloc[head:]
    return out


def cpcv_distribution(combo_block_returns, splits, purge_bars, embargo_bars, bars_per_year) -> dict:
    """For each split: pick best combo by purge+embargo pooled TRAIN Sharpe
    (tie-break: lower combo_id, deterministic), then score its pooled TEST Sharpe.
    Flat/undefined TEST -> 0.0 (kept, no survivorship). Split skipped only when no
    combo has a defined TRAIN Sharpe. Returns the distribution summary dict."""
    combo_ids = sorted(combo_block_returns.keys())
    path_sharpes: list[float] = []
    for train_ids, test_ids in splits:
        best_cid = None
        best_key = None  # (train_sharpe, -combo_id); max wins => lower id breaks ties
        for cid in combo_ids:
            purged = purge_boundary_bars(
                combo_block_returns[cid], train_ids, test_ids, purge_bars, embargo_bars
            )
            tr = pooled_sharpe(purged, train_ids, bars_per_year)
            if np.isnan(tr):
                continue
            key = (tr, -cid)
            if best_key is None or key > best_key:
                best_key, best_cid = key, cid
        if best_cid is None:
            continue  # empty train pool -> skip path
        te = pooled_sharpe(combo_block_returns[best_cid], test_ids, bars_per_year)
        path_sharpes.append(0.0 if np.isnan(te) else te)

    arr = np.array(path_sharpes, dtype=float)
    return {
        "n_paths": int(arr.size),
        "path_sharpes": [float(x) for x in path_sharpes],
        "cpcv_mean_sharpe": float(np.mean(arr)) if arr.size else None,
        "cpcv_p05_sharpe": float(np.percentile(arr, 5)) if arr.size else None,
        "pct_paths_positive": float(np.mean(arr > 0)) if arr.size else None,
    }
