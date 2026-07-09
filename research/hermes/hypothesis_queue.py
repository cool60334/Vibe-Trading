"""Foundry hypothesis queue: collect 4 sources, dedupe, static pre-filter.

Only cheap string-level filtering here so 1C does not burn LLM calls on
literally-repeated or already-buried/dead-class ideas. Numerical/semantic dedup
is 1A (nearest_correlate)."""
from __future__ import annotations

from research.hermes.hypothesis import SOURCE_ZOO, Hypothesis, is_python_expr


def _priority(h: Hypothesis) -> tuple:
    """Rank a hypothesis for collision resolution: an executable-Python
    description always outranks a non-parseable (LaTeX/NL) one. Within that,
    a non-zoo source outranks zoo — zoo descriptions are the LaTeX-prone
    source (agy 5b) even on the rare occasion one happens to parse as Python
    syntax coincidentally, so it's still the less-trustworthy pick for 1C."""
    return (is_python_expr(h.description), h.source != SOURCE_ZOO)


def dedupe(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Drop duplicate fingerprints; on collision keep the executable-Python one
    (agy 5b) so 1C receives runnable code, not LaTeX/NL."""
    best: dict = {}
    order: list = []
    for h in hypotheses:
        fp = h.fingerprint
        if fp not in best:
            best[fp] = h
            order.append(fp)
        elif _priority(h) > _priority(best[fp]):
            best[fp] = h
    return [best[fp] for fp in order]
