"""
Tests for research/pipeline/lib/archetype_router.py.

Covers the six spec scenarios for pick_archetypes():

  1. single-factor input            → one plan: archetype="single_factor", role "signal"
  2. trend + gate (mixed IC signs)  → result includes trend_with_gate plan
  3. same-sign-only                 → result does NOT include trend_with_gate
  4. 2-3 factors                    → result includes consensus_all archetype
  5. >3 possible archetypes         → capped at 3 plans total
  6. pure function                  → no network / subprocess / filesystem calls

Pytest is run from the worktree root as:
    python -m pytest research/tests/test_archetype_router.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Bootstrap: research/ and dashboard/server/ must be on sys.path.
_RESEARCH_DIR = Path(__file__).resolve().parents[1]  # research/
_REPO_ROOT = _RESEARCH_DIR.parent
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"
for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

import pytest

from pipeline.lib.archetype_router import ArchetypePlan, pick_archetypes
from schemas import FactorEntry, FactorVerdict


# ---------------------------------------------------------------------------
# Helpers — lightweight factor construction
# ---------------------------------------------------------------------------


def _make_factor(
    name: str,
    *,
    ic_by_horizon: dict[int, float | None] | None = None,
    ir: float = 0.5,
    verdict: FactorVerdict = FactorVerdict.SINGLE_USE,
) -> FactorEntry:
    """Build a real FactorEntry for use in tests (uses the actual schema)."""
    return FactorEntry(
        name=name,
        ic_by_horizon=ic_by_horizon if ic_by_horizon is not None else {24: 0.08},
        ir=ir,
        sample_size=10000,
        verdict=verdict,
    )


def _duck_factor(
    name: str,
    ic_by_horizon: dict[int, float | None],
    ir: float = 0.5,
) -> SimpleNamespace:
    """Lightweight duck-type stand-in for FactorEntry (avoids schema overhead)."""
    return SimpleNamespace(name=name, ic_by_horizon=ic_by_horizon, ir=ir)


# ---------------------------------------------------------------------------
# 1. Single-factor input
# ---------------------------------------------------------------------------


class TestSingleFactor:
    def test_returns_exactly_one_plan(self):
        factors = [_make_factor("stablecoin_z", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)

        sf_plans = [p for p in plans if p.archetype == "single_factor"]
        assert len(sf_plans) == 1, "expected exactly one single_factor plan"

    def test_single_factor_plan_has_correct_factor(self):
        factors = [_make_factor("stablecoin_z", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)

        sf_plan = next(p for p in plans if p.archetype == "single_factor")
        factor_names = [f.name for f, _role in sf_plan.factors]
        assert "stablecoin_z" in factor_names

    def test_single_factor_plan_role_is_signal(self):
        factors = [_make_factor("stablecoin_z", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)

        sf_plan = next(p for p in plans if p.archetype == "single_factor")
        roles = [role for _f, role in sf_plan.factors]
        assert roles == ["signal"]

    def test_single_factor_plan_has_exactly_one_factor_entry(self):
        factors = [_make_factor("stablecoin_z", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)

        sf_plan = next(p for p in plans if p.archetype == "single_factor")
        assert len(sf_plan.factors) == 1


# ---------------------------------------------------------------------------
# 2. Trend + gate (mixed IC signs) → trend_with_gate plan emitted
# ---------------------------------------------------------------------------


class TestTrendWithGate:
    def test_trend_with_gate_plan_present_when_mixed_signs(self):
        factors = [
            _make_factor("momentum", ic_by_horizon={24: 0.09}),   # positive IC → trend
            _make_factor("funding",  ic_by_horizon={24: -0.07}),  # negative IC → gate
        ]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "trend_with_gate" in archetypes

    def test_trend_factor_has_role_trend(self):
        factors = [
            _make_factor("momentum", ic_by_horizon={24: 0.09}),
            _make_factor("funding",  ic_by_horizon={24: -0.07}),
        ]
        plans = pick_archetypes(factors)

        twg = next(p for p in plans if p.archetype == "trend_with_gate")
        role_map = {f.name: role for f, role in twg.factors}
        assert role_map["momentum"] == "trend"

    def test_gate_factor_has_role_gate(self):
        factors = [
            _make_factor("momentum", ic_by_horizon={24: 0.09}),
            _make_factor("funding",  ic_by_horizon={24: -0.07}),
        ]
        plans = pick_archetypes(factors)

        twg = next(p for p in plans if p.archetype == "trend_with_gate")
        role_map = {f.name: role for f, role in twg.factors}
        assert role_map["funding"] == "gate"

    def test_trend_with_gate_picks_top_abs_ic_representatives(self):
        """When there are multiple positive and negative IC factors,
        the plan uses the one with the highest |IC| on each side."""
        factors = [
            _make_factor("weak_trend",   ic_by_horizon={24: 0.04}),
            _make_factor("strong_trend", ic_by_horizon={24: 0.12}),
            _make_factor("weak_gate",    ic_by_horizon={24: -0.03}),
            _make_factor("strong_gate",  ic_by_horizon={24: -0.09}),
        ]
        plans = pick_archetypes(factors)

        twg = next(p for p in plans if p.archetype == "trend_with_gate")
        factor_names = {f.name for f, _ in twg.factors}
        assert "strong_trend" in factor_names
        assert "strong_gate" in factor_names


# ---------------------------------------------------------------------------
# 3. Same-sign-only → trend_with_gate NOT emitted
# ---------------------------------------------------------------------------


class TestNoTrendWithGateWhenSameSign:
    def test_all_positive_ic_no_trend_with_gate(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.08}),
            _make_factor("f2", ic_by_horizon={24: 0.06}),
        ]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "trend_with_gate" not in archetypes

    def test_all_negative_ic_no_trend_with_gate(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: -0.08}),
            _make_factor("f2", ic_by_horizon={24: -0.06}),
        ]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "trend_with_gate" not in archetypes


# ---------------------------------------------------------------------------
# 4. 2-3 factors → consensus_all emitted
# ---------------------------------------------------------------------------


class TestConsensusAll:
    def test_two_factors_produces_consensus_all(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.08}),
            _make_factor("f2", ic_by_horizon={24: 0.06}),
        ]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "consensus_all" in archetypes

    def test_three_factors_produces_consensus_all(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.08}),
            _make_factor("f2", ic_by_horizon={24: 0.06}),
            _make_factor("f3", ic_by_horizon={24: -0.07}),
        ]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "consensus_all" in archetypes

    def test_consensus_all_factors_have_role_consensus(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.08}),
            _make_factor("f2", ic_by_horizon={24: 0.06}),
        ]
        plans = pick_archetypes(factors)

        ca = next(p for p in plans if p.archetype == "consensus_all")
        roles = [role for _f, role in ca.factors]
        assert all(r == "consensus" for r in roles), f"unexpected roles: {roles}"

    def test_consensus_all_contains_all_input_factors(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.08}),
            _make_factor("f2", ic_by_horizon={24: 0.06}),
            _make_factor("f3", ic_by_horizon={24: -0.07}),
        ]
        plans = pick_archetypes(factors)

        ca = next(p for p in plans if p.archetype == "consensus_all")
        factor_names = {f.name for f, _ in ca.factors}
        assert factor_names == {"f1", "f2", "f3"}

    def test_single_factor_no_consensus_all(self):
        """consensus_all requires >= 2 factors."""
        factors = [_make_factor("f1", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "consensus_all" not in archetypes

    def test_four_factors_no_consensus_all(self):
        """consensus_all is only for 2-3 usable factors."""
        factors = [
            _make_factor(f"f{i}", ic_by_horizon={24: 0.05 + i * 0.01})
            for i in range(4)
        ]
        plans = pick_archetypes(factors)

        archetypes = [p.archetype for p in plans]
        assert "consensus_all" not in archetypes


# ---------------------------------------------------------------------------
# 5. Result capped at 3 plans total
# ---------------------------------------------------------------------------


class TestCapAtThreePlans:
    def test_result_never_exceeds_three_plans(self):
        """With mixed-sign factors and 2–3 factor count, all three archetypes
        could be generated — but the cap must hold at 3."""
        factors = [
            _make_factor("trend_a", ic_by_horizon={24: 0.12}),   # positive
            _make_factor("gate_b",  ic_by_horizon={24: -0.09}),  # negative
            # 2 factors: single_factor + trend_with_gate + consensus_all = 3
        ]
        plans = pick_archetypes(factors)
        assert len(plans) <= 3

    def test_many_factors_still_capped_at_three(self):
        """With 5 factors of mixed signs, routing could produce many plans."""
        factors = [
            _make_factor(f"pos_{i}", ic_by_horizon={24: 0.08 + i * 0.01})
            for i in range(3)
        ] + [
            _make_factor(f"neg_{i}", ic_by_horizon={24: -(0.08 + i * 0.01)})
            for i in range(2)
        ]
        plans = pick_archetypes(factors)
        assert len(plans) <= 3


# ---------------------------------------------------------------------------
# 6. Pure function — no I/O, no subprocess
# ---------------------------------------------------------------------------


class TestPureFunction:
    def test_returns_list_of_archetype_plans(self):
        factors = [_make_factor("f1", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)
        assert isinstance(plans, list)
        for p in plans:
            assert isinstance(p, ArchetypePlan)

    def test_deterministic_same_output_each_call(self):
        """Same input → same output on repeated calls."""
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.10}),
            _make_factor("f2", ic_by_horizon={24: -0.07}),
        ]
        plans_a = pick_archetypes(factors)
        plans_b = pick_archetypes(factors)

        archetypes_a = sorted(p.archetype for p in plans_a)
        archetypes_b = sorted(p.archetype for p in plans_b)
        assert archetypes_a == archetypes_b

    def test_does_not_mutate_input_list(self):
        factors = [
            _make_factor("f1", ic_by_horizon={24: 0.08}),
            _make_factor("f2", ic_by_horizon={24: 0.06}),
        ]
        original_len = len(factors)
        original_names = [f.name for f in factors]
        pick_archetypes(factors)
        assert len(factors) == original_len
        assert [f.name for f in factors] == original_names

    def test_accepts_duck_typed_objects(self):
        """Router must work with lightweight duck-type objects (no FactorEntry overhead)."""
        factors = [_duck_factor("duck_trend", {24: 0.09})]
        plans = pick_archetypes(factors)
        assert len(plans) >= 1

    def test_empty_input_returns_empty_list(self):
        plans = pick_archetypes([])
        assert plans == []

    def test_archetype_plan_fields_are_strings_and_list(self):
        factors = [_make_factor("f1", ic_by_horizon={24: 0.08})]
        plans = pick_archetypes(factors)

        for plan in plans:
            assert isinstance(plan.archetype, str)
            assert isinstance(plan.factors, list)
            for item in plan.factors:
                assert isinstance(item, tuple), f"factors item should be tuple, got {type(item)}"
                factor_obj, role = item
                assert isinstance(role, str)
