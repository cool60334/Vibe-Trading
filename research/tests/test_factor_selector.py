"""
Tests for research/pipeline/lib/factor_selector.py.

Covers the five scenarios in
``openspec/changes/stage0-deterministic-discovery/specs/factor-discovery/spec.md``
under the *Deterministic candidate selector* requirement:

  (a) high IC factor selected
  (b) low IC factor filtered
  (c) feature_key not in feature store dropped
  (d) max_candidates truncates
  (e) zero factors pass — empty list, no raise

Plus extra coverage for None-IC handling, related-horizon expansion, and
category coercion (so we don't regress on quirky evidence inputs).

Pytest is run from research/ as:
    cd research && python -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap: research/ and dashboard/server/ must be on sys.path.
_RESEARCH_DIR = Path(__file__).resolve().parents[1]  # research/
_REPO_ROOT = _RESEARCH_DIR.parent
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"
for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

import pytest

from pipeline.lib.factor_selector import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_ABS_IC,
    DEFAULT_MIN_ABS_IR,
    _coerce_category,
    _rank_evidence_entries,
    _related_horizons,
    _top_horizon_and_ic,
    select_candidates_from_evidence,
)
from schemas import FactorCandidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_entry(
    feature_key: str,
    *,
    category: str = "momentum",
    ic_by_horizon: dict[str, float | None] | None = None,
    ir: float | None = 0.5,
    sample_size: int = 35000,
) -> dict:
    """Build a single evidence entry dict in the on-disk format."""
    return {
        "feature_key": feature_key,
        "category": category,
        "ic_by_horizon": ic_by_horizon
        if ic_by_horizon is not None
        else {"8": 0.0, "24": 0.0, "72": 0.0, "168": 0.0},
        "ic_eval_transform": None,
        "ir": ir,
        "sample_size": sample_size,
    }


@pytest.fixture
def evidence_dict() -> dict:
    """A realistic mixed evidence manifest covering all scenarios."""
    return {
        "caveat": "Multiple-testing inflation possible.",
        "generated_at": "2026-06-02T00:00:00Z",
        "symbol": "eth",
        "evidence": [
            # High IC trend factor (passes thresholds)
            _make_entry(
                "stablecoin_supply_z",
                category="stablecoin",
                ic_by_horizon={
                    "8": 0.032,
                    "24": 0.042,
                    "72": 0.074,
                    "168": 0.091,
                },
                ir=4.43,
            ),
            # High |IC| contrarian factor (passes)
            _make_entry(
                "atr_14",
                category="volatility",
                ic_by_horizon={
                    "8": 0.007,
                    "24": -0.020,
                    "72": -0.053,
                    "168": -0.097,
                },
                ir=-0.32,
            ),
            # Low IC — should be filtered
            _make_entry(
                "basis_rel",
                category="basis",
                ic_by_horizon={
                    "8": -0.019,
                    "24": -0.004,
                    "72": 0.010,
                    "168": 0.017,
                },
                ir=-0.84,
            ),
            # feature_key not in feature_store_cols — should be filtered
            _make_entry(
                "imaginary_factor",
                category="momentum",
                ic_by_horizon={"8": 0.10, "24": 0.10, "72": 0.10, "168": 0.10},
                ir=1.0,
            ),
            # IR below threshold but IC high — should be filtered
            _make_entry(
                "low_ir_factor",
                category="momentum",
                ic_by_horizon={"8": 0.0, "24": 0.0, "72": 0.10, "168": 0.0},
                ir=0.05,
            ),
        ],
    }


@pytest.fixture
def feature_store_cols() -> set[str]:
    """Columns present in features_eth.parquet (matches real schema)."""
    return {"stablecoin_supply_z", "atr_14", "basis_rel", "low_ir_factor", "rsi_14"}


# ---------------------------------------------------------------------------
# select_candidates_from_evidence — 5 spec scenarios
# ---------------------------------------------------------------------------


class TestSelectCandidatesFromEvidence:
    """Public API: select_candidates_from_evidence(...)."""

    # (a) High IC factor selected
    def test_high_ic_factor_is_selected(self, evidence_dict, feature_store_cols):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        keys = [c.feature_key for c in out]
        assert "stablecoin_supply_z" in keys

        sc = next(c for c in out if c.feature_key == "stablecoin_supply_z")
        assert sc.expected_ic_sign == "+"
        assert 168 in sc.horizons_h
        # All four horizons share +sign and exceed min_abs_ic/2 = 0.025
        assert sc.horizons_h == [8, 24, 72, 168]

    # (b) Low IC factor filtered out
    def test_low_ic_factor_is_filtered(self, evidence_dict, feature_store_cols):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        keys = [c.feature_key for c in out]
        # basis_rel max |IC| = 0.019 < default 0.05
        assert "basis_rel" not in keys

    # (c) feature_key not in feature store is dropped
    def test_feature_key_not_in_store_is_dropped(
        self, evidence_dict, feature_store_cols
    ):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        keys = [c.feature_key for c in out]
        assert "imaginary_factor" not in keys

    # (d) max_candidates truncates
    def test_max_candidates_truncates(self):
        many_entries = []
        store_cols = set()
        # 20 entries all passing thresholds, ranked by |IC|
        for i in range(20):
            ic = 0.10 + i * 0.001  # ascending, last is biggest
            key = f"factor_{i:02d}"
            store_cols.add(key)
            many_entries.append(
                _make_entry(
                    key,
                    ic_by_horizon={"168": ic, "8": 0.0, "24": 0.0, "72": 0.0},
                    ir=1.0,
                )
            )
        out = select_candidates_from_evidence(
            {"evidence": many_entries}, store_cols, max_candidates=6
        )
        assert len(out) == 6
        # Should be the top 6 by |IC| descending → factor_19 .. factor_14
        top_keys = [c.feature_key for c in out]
        assert top_keys == [
            "factor_19",
            "factor_18",
            "factor_17",
            "factor_16",
            "factor_15",
            "factor_14",
        ]

    # (e) Zero factors pass — empty list, no raise
    def test_zero_factors_pass_returns_empty(self, feature_store_cols):
        flat = {
            "evidence": [
                _make_entry(
                    "atr_14",
                    ic_by_horizon={
                        "8": 0.01,
                        "24": 0.01,
                        "72": 0.01,
                        "168": 0.01,
                    },
                    ir=0.0,
                )
            ]
        }
        out = select_candidates_from_evidence(flat, feature_store_cols)
        assert out == []  # no raise, just empty

    # ───────────────────────── extra coverage ─────────────────────────

    def test_accepts_bare_list_of_entries(self, feature_store_cols):
        entries = [
            _make_entry(
                "stablecoin_supply_z",
                category="stablecoin",
                ic_by_horizon={
                    "8": 0.032,
                    "24": 0.042,
                    "72": 0.074,
                    "168": 0.091,
                },
                ir=4.43,
            )
        ]
        out = select_candidates_from_evidence(entries, feature_store_cols)
        assert len(out) == 1

    def test_none_ic_values_are_ignored(self, feature_store_cols):
        entry = _make_entry(
            "stablecoin_supply_z",
            category="stablecoin",
            ic_by_horizon={"8": None, "24": None, "72": -0.10, "168": None},
            ir=1.0,
        )
        out = select_candidates_from_evidence(
            {"evidence": [entry]}, feature_store_cols
        )
        assert len(out) == 1
        assert out[0].horizons_h == [72]
        assert out[0].expected_ic_sign == "-"

    def test_economic_logic_placeholder_contains_ic_and_horizon(
        self, evidence_dict, feature_store_cols
    ):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        sc = next(c for c in out if c.feature_key == "stablecoin_supply_z")
        assert "deterministic" in sc.economic_logic.lower()
        assert "168h" in sc.economic_logic
        assert "0.09" in sc.economic_logic  # IC value formatted

    def test_returned_objects_are_validated_factor_candidates(
        self, evidence_dict, feature_store_cols
    ):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        for c in out:
            assert isinstance(c, FactorCandidate)

    def test_sort_order_by_abs_ic_descending(self, evidence_dict, feature_store_cols):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        keys = [c.feature_key for c in out]
        # atr_14 |IC|=0.097 > stablecoin_supply_z |IC|=0.091
        assert keys[0] == "atr_14"
        assert keys[1] == "stablecoin_supply_z"

    def test_low_ir_is_filtered(self, evidence_dict, feature_store_cols):
        out = select_candidates_from_evidence(evidence_dict, feature_store_cols)
        keys = [c.feature_key for c in out]
        # low_ir_factor IC=0.10 but IR=0.05 < default 0.10
        assert "low_ir_factor" not in keys


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class TestTopHorizonAndIc:
    def test_picks_max_abs(self):
        assert _top_horizon_and_ic({"8": 0.01, "72": -0.05}) == (72, -0.05)

    def test_handles_string_and_int_keys(self):
        # Real evidence uses string keys; helper must coerce to int.
        assert _top_horizon_and_ic({"168": 0.09})[0] == 168

    def test_all_none_returns_none(self):
        assert _top_horizon_and_ic({"8": None, "72": None}) is None

    def test_empty_returns_none(self):
        assert _top_horizon_and_ic({}) is None


class TestRelatedHorizons:
    def test_includes_top_and_same_sign(self):
        ic = {"8": 0.05, "24": 0.06, "72": -0.03, "168": 0.08}
        out = _related_horizons(ic, top_h=168, top_ic=0.08, min_abs_ic=0.05)
        # half_threshold = 0.025; +sign horizons {8 (0.05), 24 (0.06), 168 (0.08)}
        assert out == [8, 24, 168]

    def test_excludes_opposite_sign(self):
        ic = {"8": -0.10, "168": 0.08}
        out = _related_horizons(ic, top_h=168, top_ic=0.08, min_abs_ic=0.05)
        assert out == [168]

    def test_excludes_below_half_threshold(self):
        ic = {"8": 0.01, "168": 0.08}
        out = _related_horizons(ic, top_h=168, top_ic=0.08, min_abs_ic=0.05)
        # half_threshold = 0.025; 0.01 < 0.025
        assert out == [168]


class TestCoerceCategory:
    def test_passes_through_allowed(self):
        assert _coerce_category("funding") == "funding"
        assert _coerce_category("stablecoin") == "stablecoin"

    def test_maps_trend_to_momentum(self):
        assert _coerce_category("trend") == "momentum"

    def test_maps_volume_to_oi(self):
        assert _coerce_category("volume") == "oi"

    def test_unknown_falls_back_to_momentum(self):
        assert _coerce_category("garbage") == "momentum"

    def test_none_falls_back_to_momentum(self):
        assert _coerce_category(None) == "momentum"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_constants_match_design():
    assert DEFAULT_MIN_ABS_IC == 0.05
    assert DEFAULT_MIN_ABS_IR == 0.10
    assert DEFAULT_MAX_CANDIDATES == 6


def test_rank_evidence_entries_is_pure(evidence_dict, feature_store_cols):
    """The ranker must not mutate the input dict."""
    before_len = len(evidence_dict["evidence"])
    _rank_evidence_entries(
        evidence_dict["evidence"],
        feature_store_cols=feature_store_cols,
        min_abs_ic=DEFAULT_MIN_ABS_IC,
        min_abs_ir=DEFAULT_MIN_ABS_IR,
    )
    assert len(evidence_dict["evidence"]) == before_len
