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
