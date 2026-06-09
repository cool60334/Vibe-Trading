"""
research/pipeline/lib/archetype_router.py
──────────────────────────────────────────
Deterministic archetype selection from a list of stage-1 factor entries.

Given a list of FactorEntry objects (or duck-typed equivalents with the same
interface), ``pick_archetypes`` returns up to three ``ArchetypePlan`` objects
that describe how those factors should be scaffolded into a strategy YAML in
stage 2.

Design (see backlog_stage2_archetype_factory.md):

  Three archetypes are considered in order:

  1. ``single_factor``   — always emitted when ≥ 1 factor is present.
                           Uses the top-|IC| factor with role ``"signal"``.

  2. ``trend_with_gate`` — emitted only when ≥ 1 positive-IC factor AND
                           ≥ 1 negative-IC factor are present.
                           Pairs the top-|IC| positive factor (role
                           ``"trend"``) with the top-|IC| negative factor
                           (role ``"gate"``).

  3. ``consensus_all``   — emitted only when 2 ≤ n ≤ 3 usable factors are
                           present.  All factors carry role ``"consensus"``.

  The result list is capped at 3 plans.

IC sign is determined by the ``_factor_is_trend`` helper: find the horizon
with the largest |IC|, check its sign.  Positive → trend; negative → gate /
contrarian.

All functions here are **pure** (no network, no subprocess, no filesystem
access) so they can be unit-tested without any external fixtures.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Sequence

# ─── Maximum number of plans returned ────────────────────────────────────────

#: Hard cap on the number of ArchetypePlan objects returned by pick_archetypes.
MAX_PLANS: int = 3


# ─── Data structures ─────────────────────────────────────────────────────────


@dataclasses.dataclass
class ArchetypePlan:
    """A proposed archetype scaffold for a set of factors.

    Attributes:
        archetype: One of ``"single_factor"``, ``"trend_with_gate"``, or
                   ``"consensus_all"``.
        factors:   List of ``(factor, role)`` tuples where ``factor`` is the
                   original FactorEntry (or duck-typed object) and ``role`` is
                   one of ``"signal"``, ``"trend"``, ``"gate"``,
                   ``"consensus"``.
    """

    archetype: str
    factors: list[tuple[Any, str]]


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _factor_is_trend(factor: Any) -> bool:
    """Return True when the factor's dominant IC is positive (trend direction).

    Ported from ``research/pipeline/stage2_strategies.py::_factor_is_trend``
    so this module stays pure and self-contained (no circular imports).

    The sign is determined by the IC at the horizon with the largest absolute
    value.  Factors with no IC data (empty dict or all-None values) default to
    ``False`` (contrarian / gate direction).

    Args:
        factor: Any object with an ``ic_by_horizon`` attribute (``dict[int,
                float | None]``).

    Returns:
        ``True`` for positive dominant IC (trend), ``False`` for negative or
        absent IC (contrarian / gate).
    """
    ics = {
        h: v
        for h, v in (getattr(factor, "ic_by_horizon", None) or {}).items()
        if v is not None
    }
    if not ics:
        return False
    best_h = max(ics, key=lambda h: abs(ics[h]))
    return ics[best_h] > 0


def _top_abs_ic(factor: Any) -> float:
    """Return the absolute IC at the top horizon, or 0.0 if unavailable.

    Used to rank factors by signal strength when selecting representatives for
    ``single_factor`` and ``trend_with_gate`` plans.

    Args:
        factor: Any object with an ``ic_by_horizon`` attribute.

    Returns:
        ``|IC|`` at the most informative horizon, or ``0.0``.
    """
    ics = {
        h: v
        for h, v in (getattr(factor, "ic_by_horizon", None) or {}).items()
        if v is not None
    }
    if not ics:
        return 0.0
    best_h = max(ics, key=lambda h: abs(ics[h]))
    return abs(ics[best_h])


# ─── Archetype builders ──────────────────────────────────────────────────────


def _build_single_factor(factors: Sequence[Any]) -> ArchetypePlan | None:
    """Build a ``single_factor`` plan using the top-|IC| factor.

    Args:
        factors: Non-empty sequence of factor objects.

    Returns:
        An ``ArchetypePlan`` with one ``(factor, "signal")`` entry, or
        ``None`` if ``factors`` is empty.
    """
    if not factors:
        return None
    top = max(factors, key=_top_abs_ic)
    return ArchetypePlan(archetype="single_factor", factors=[(top, "signal")])


def _build_trend_with_gate(factors: Sequence[Any]) -> ArchetypePlan | None:
    """Build a ``trend_with_gate`` plan pairing the best positive and negative IC factors.

    Args:
        factors: Sequence of factor objects.

    Returns:
        An ``ArchetypePlan`` or ``None`` when mixed-sign pairs are not present.
    """
    positives = [f for f in factors if _factor_is_trend(f)]
    negatives = [f for f in factors if not _factor_is_trend(f)]

    if not positives or not negatives:
        return None

    trend_factor = max(positives, key=_top_abs_ic)
    gate_factor = max(negatives, key=_top_abs_ic)

    return ArchetypePlan(
        archetype="trend_with_gate",
        factors=[(trend_factor, "trend"), (gate_factor, "gate")],
    )


def _build_consensus_all(factors: Sequence[Any]) -> ArchetypePlan | None:
    """Build a ``consensus_all`` plan when there are 2–3 usable factors.

    All factors receive the role ``"consensus"``.

    Args:
        factors: Sequence of factor objects.

    Returns:
        An ``ArchetypePlan`` or ``None`` when the count is outside [2, 3].
    """
    n = len(factors)
    if n < 2 or n > 3:
        return None
    return ArchetypePlan(
        archetype="consensus_all",
        factors=[(f, "consensus") for f in factors],
    )


# ─── Public API ──────────────────────────────────────────────────────────────


def pick_archetypes(factors: Sequence[Any]) -> list[ArchetypePlan]:
    """Deterministically select up to three archetype plans from a factor list.

    Router rules (applied in order; result capped at ``MAX_PLANS`` = 3):

    1. ``single_factor``   — emitted if ≥ 1 factor present; uses top-|IC|
                             factor with role ``"signal"``.
    2. ``trend_with_gate`` — emitted if ≥ 1 positive-IC AND ≥ 1 negative-IC
                             factor present.
    3. ``consensus_all``   — emitted if 2 ≤ n ≤ 3 factors.

    This function is **pure**: it performs no I/O, no network calls, and no
    subprocess invocations.

    Args:
        factors: List (or any sequence) of FactorEntry objects or duck-typed
                 objects with ``ic_by_horizon: dict[int, float | None]`` and
                 ``name: str`` attributes.  The list is not mutated.

    Returns:
        A list of ``ArchetypePlan`` objects, length in ``[0, MAX_PLANS]``.
        Returns an empty list when ``factors`` is empty.
    """
    factors = list(factors)  # snapshot — do not mutate caller's list

    if not factors:
        return []

    plans: list[ArchetypePlan] = []

    # Rule 1 — single_factor (always when ≥ 1 factor)
    sf = _build_single_factor(factors)
    if sf is not None:
        plans.append(sf)

    # Rule 2 — trend_with_gate (requires mixed IC signs)
    twg = _build_trend_with_gate(factors)
    if twg is not None:
        plans.append(twg)

    # Rule 3 — consensus_all (requires 2 ≤ n ≤ 3)
    ca = _build_consensus_all(factors)
    # Dedup: with exactly 2 mixed-sign factors, consensus_all collapses to the
    # SAME trading logic as trend_with_gate. Stage 2 builds entry conditions per
    # factor purely from each factor's IC sign (see stage2_strategies._entry_condition),
    # so a 2-factor consensus AND = the trend>=80 AND gate<=20 pair that
    # trend_with_gate emits — only the archetype label differs. Emitting both
    # produces two byte-identical strategies (e.g. btc_s2 ≡ btc_s3) that waste a
    # stage-3/4 backtest each. When trend_with_gate already fired for n == 2,
    # drop consensus_all. (n == 3 consensus_all uses logic=any over all 3 factors
    # while trend_with_gate uses only 2, so it stays distinct.)
    if ca is not None and not (len(factors) == 2 and twg is not None):
        plans.append(ca)

    # Hard cap (no-op today; guards against future archetype additions)
    return plans[:MAX_PLANS]
