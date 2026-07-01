"""Tests for the CPCV lib (pure, no IO).
Run: cd research && python -m pytest tests/test_cpcv.py -v
"""
from __future__ import annotations

import sys
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.cpcv import (  # noqa: E402
    combinatorial_splits,
    make_blocks,
    pooled_sharpe,
    purge_boundary_bars,
)


class TestMakeBlocks:
    def test_contiguous_non_overlapping_cover(self):
        blocks = make_blocks("2022-01-01", "2023-01-01", 4)
        assert len(blocks) == 4
        assert blocks[0][0] == "2022-01-01"
        assert blocks[-1][1] == "2023-01-01"
        # each block's end == next block's start (contiguous, no gaps/overlap)
        for i in range(len(blocks) - 1):
            assert blocks[i][1] == blocks[i + 1][0]

    def test_raises_on_bad_n(self):
        with pytest.raises(ValueError):
            make_blocks("2022-01-01", "2023-01-01", 1)


class TestCombinatorialSplits:
    def test_count_is_c_n_k(self):
        splits = combinatorial_splits(10, 3)
        assert len(splits) == comb(10, 3) == 120

    def test_train_test_disjoint_and_cover(self):
        for train, test in combinatorial_splits(6, 2):
            assert set(train).isdisjoint(test)
            assert set(train) | set(test) == set(range(6))
            assert len(test) == 2

    def test_raises_when_k_ge_n(self):
        with pytest.raises(ValueError):
            combinatorial_splits(3, 3)


def _const_returns(n, val, start_id):
    idx = pd.RangeIndex(start_id, start_id + n)
    return pd.Series([val] * n, index=idx, dtype=float)


class TestPooledSharpe:
    def test_annualised_mean_over_std(self):
        r = pd.Series([0.01, -0.01, 0.02, -0.02, 0.03], dtype=float)
        br = {0: r}
        got = pooled_sharpe(br, [0], bars_per_year=8760)
        exp = r.mean() / r.std() * (8760 ** 0.5)
        assert abs(got - exp) < 1e-9

    def test_pools_multiple_blocks(self):
        br = {0: pd.Series([0.01, 0.01]), 1: pd.Series([0.02, 0.02])}
        got = pooled_sharpe(br, [0, 1], bars_per_year=8760)
        pooled = pd.concat([br[0], br[1]])
        assert abs(got - pooled.mean() / pooled.std() * (8760 ** 0.5)) < 1e-9

    def test_nan_on_too_few_or_zero_std(self):
        assert np.isnan(pooled_sharpe({0: pd.Series([0.01])}, [0], 8760))       # <2
        assert np.isnan(pooled_sharpe({0: _const_returns(5, 0.0, 0)}, [0], 8760))  # zero std


class TestPurgeBoundaryBars:
    def _matrix(self):
        # 4 blocks, 10 bars each, ids 0..3
        return {b: _const_returns(10, 0.01, b * 10) for b in range(4)}

    def test_train_precedes_test_drops_tail(self):
        # train {0,1}, test {2}: block 1 precedes test 2 -> drop tail of block 1
        purged = purge_boundary_bars(self._matrix(), (0, 1), (2,), purge_bars=3, embargo_bars=3)
        assert len(purged[1]) == 7        # tail 3 dropped
        assert len(purged[0]) == 10       # block 0 not adjacent to a test block
        assert list(purged[1].index) == list(range(10, 17))  # kept the HEAD

    def test_train_follows_test_drops_head(self):
        # train {2,3}, test {1}: block 2 follows test 1 -> drop head of block 2
        purged = purge_boundary_bars(self._matrix(), (2, 3), (1,), purge_bars=3, embargo_bars=4)
        assert len(purged[2]) == 6        # head 4 (embargo) dropped
        assert list(purged[2].index) == list(range(24, 30))  # kept the TAIL
        assert len(purged[3]) == 10

    def test_sandwiched_drops_both(self):
        # train {1}, test {0,2}: block 1 follows test 0 AND precedes test 2
        purged = purge_boundary_bars(self._matrix(), (1,), (0, 2), purge_bars=2, embargo_bars=2)
        assert len(purged[1]) == 6        # head 2 + tail 2 dropped
