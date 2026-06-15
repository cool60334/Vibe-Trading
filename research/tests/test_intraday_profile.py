# research/tests/test_intraday_profile.py
"""Sub-hour profile + intraday factor wiring — run from research/ (pytest tests/)."""
import sys
from pathlib import Path

import pytest

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from pipeline.stage0a_features import _INDICATOR_CATEGORY


def test_intraday_factors_have_categories():
    assert _INDICATOR_CATEGORY["mom_4"] == "momentum"
    assert _INDICATOR_CATEGORY["mom_8"] == "momentum"
    assert _INDICATOR_CATEGORY["mom_16"] == "momentum"
    assert _INDICATOR_CATEGORY["rvol_ratio_8_32"] == "volatility"
    assert _INDICATOR_CATEGORY["range_expansion_16"] == "volatility"
    assert _INDICATOR_CATEGORY["volume_zscore_8"] == "volume"
