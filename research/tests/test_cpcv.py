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
