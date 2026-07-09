"""Foundry hypothesis queue: collect 4 sources, dedupe, static pre-filter.

Only cheap string-level filtering here so 1C does not burn LLM calls on
literally-repeated or already-buried/dead-class ideas. Numerical/semantic dedup
is 1A (nearest_correlate)."""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from research.hermes.evidence_card import VERDICT_GRAVEYARD
from research.hermes.evidence_store import load_cards
from research.hermes.hypothesis import (
    SOURCE_ACADEMIC, SOURCE_DERIVED, SOURCE_LLM, SOURCE_ZOO,
    Hypothesis, is_python_expr, string_fingerprint,
)
from research.lib.factor_io import load_evidence

# Constitution dead classes (already-buried families; never re-propose).
# See talos-design.md §5 + memory project_intraday_ohlcv_class_dead / orderflow_poc.
DEAD_CLASSES = frozenset({"intraday_ohlcv_price_derived", "binance_orderflow"})


def _priority(h: Hypothesis) -> tuple:
    """Rank a hypothesis for collision resolution: an executable-Python
    description always outranks a non-parseable (LaTeX/NL) one. Within that,
    a non-zoo source outranks zoo — zoo descriptions are the LaTeX-prone
    source (agy 5b) even on the rare occasion one happens to parse as Python
    syntax coincidentally, so it's still the less-trustworthy pick for 1C.
    Final element is the id: a stable, order-independent tiebreak so that
    hypotheses tying on both prior axes (e.g. two non-zoo/executable dupes)
    still resolve identically regardless of input order."""
    return (is_python_expr(h.description), h.source != SOURCE_ZOO, h.id)


def dedupe(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Drop duplicate fingerprints; on collision keep the executable-Python one
    (agy 5b) so 1C receives runnable code, not LaTeX/NL.

    dead_classes tags are unioned across all colliding hypotheses and attached
    to the surviving one — a zoo duplicate tagged dead_classes must not have
    that ban signal silently dropped just because a non-zoo duplicate won the
    executability/source tiebreak (Task 3's dead-class filter runs downstream
    of this queue and needs the tag on whichever hypothesis survives)."""
    best: dict = {}
    dead: dict = {}
    order: list = []
    for h in hypotheses:
        fp = h.fingerprint
        if fp not in best:
            best[fp] = h
            order.append(fp)
        elif _priority(h) > _priority(best[fp]):
            best[fp] = h
        dead.setdefault(fp, set()).update(h.dead_classes)

    result = []
    for fp in order:
        winner = best[fp]
        merged = dead.get(fp, set())
        if merged - set(winner.dead_classes):
            winner = replace(winner, dead_classes=tuple(sorted(merged)))
        result.append(winner)
    return result


def _graveyard_fingerprints(symbol: str, manifests_dir) -> set:
    """String fingerprints of buried factors from the symbol's evidence store.
    Best-effort, same-representation only (agy 3): cross-representation dedup is
    1A numerical + post-1C code_sha256."""
    return {
        string_fingerprint(c.formula)
        for c in load_cards(symbol, manifests_dir)
        if c.verdict == VERDICT_GRAVEYARD
    }


def filter_static(hypotheses: list[Hypothesis], symbol: str, manifests_dir) -> list[Hypothesis]:
    """Drop hypotheses that are already-buried (graveyard fingerprint match) or
    tagged into a constitution dead class. Cheap string-level filtering only —
    numerical/semantic dedup is 1A's job."""
    dead_fp = _graveyard_fingerprints(symbol, manifests_dir)
    out: list = []
    for h in hypotheses:
        if h.fingerprint in dead_fp:
            continue
        if set(h.dead_classes) & DEAD_CLASSES:
            continue
        out.append(h)
    return out


# zoo theme -> constitution dead class (agy 4)
_THEME_DEAD_CLASS = {"microstructure": "intraday_ohlcv_price_derived"}


def _dead_classes_for_themes(themes) -> tuple:
    return tuple(sorted({_THEME_DEAD_CLASS[t] for t in (themes or []) if t in _THEME_DEAD_CLASS}))


def _extract_alpha_meta(source: str) -> dict | None:
    """Parse `source` and pull the module-level `__alpha_meta__ = {...}` dict
    without executing any code (agy 2c/4).

    Uses ast.parse + a module-level Assign walk (not a brace-matching regex):
    zoo files under agent/src/factors/zoo/academic/*.py embed raw LaTeX with
    literal `{`/`}` inside formula_latex strings (e.g. r'\\mathrm{zscore}_{x}...'),
    which breaks a non-greedy `\\{.*?\\}` regex — it stops at the first `}` it
    finds, truncating the dict literal mid-string and raising SyntaxError from
    ast.literal_eval on the truncated text. Walking the real AST and
    literal-evaling only the matched assignment's value subtree handles
    arbitrary nesting/braces-in-strings correctly, and never executes the
    module body (parsing is not executing)."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__alpha_meta__" for t in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def hypotheses_from_zoo(zoo_dir) -> list[Hypothesis]:
    """Adapter: read `__alpha_meta__` from every zoo factor file (452+ under
    agent/src/factors/zoo/**) without executing `compute()` or any module-level
    side effects, and map each factor's theme(s) to constitution dead classes."""
    out: list = []
    for path in sorted(Path(zoo_dir).rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "__alpha_meta__" not in text:          # cheap skip before parsing
            continue
        meta = _extract_alpha_meta(text)
        if not meta or "id" not in meta:
            continue
        out.append(Hypothesis(
            id=f"zoo_{meta['id']}",
            description=str(meta.get("formula_latex", meta["id"])),
            source=SOURCE_ZOO,
            dead_classes=_dead_classes_for_themes(meta.get("theme")),
        ))
    return out


# Academic seed hypotheses (agy 5b): a small, hand-picked set of well-known
# executable-Python factor descriptors, distinct from the zoo (which sources
# its LaTeX/NL descriptions from agent/src/factors/zoo/**).
_ACADEMIC_SEED = (
    ("acad_ts_mom", "close / close.shift(20) - 1"),
    ("acad_lo_vol", "-1 * close.pct_change().rolling(20).std()"),
)

# Cheap transforms applied to each high-IC feature_key when deriving variants.
_DERIVE_TRANSFORMS = ("zscore", "rank")


def hypotheses_from_evidence_derivation(symbol: str, manifests_dir, top_k: int = 5) -> list[Hypothesis]:
    """Derive variant descriptors from the top-IR^ features in evidence_<sym>.json.

    Rows key on 'feature_key' (agy 5a — not 'factor'/'name').

    ^ "top" here means "first top_k rows of the evidence list", not a re-sort
    by any field on this side. stage0a_features.sort_evidence_by_ic() already
    sorts entries by descending max|IC| across horizons before dump_evidence()
    persists evidence_<sym>.json (research/pipeline/stage0a_features.py), so
    the file's row order already is the intended high-IC ranking. Re-sorting
    here (e.g. by the 'ir' field, which is mean-IR-across-horizons — a
    different metric from the file's max|IC| sort key) would silently
    override that intended ranking with a different one. Missing/absent
    evidence (no stage0a run yet for this symbol) degrades to an empty list.
    """
    try:
        ev = load_evidence(symbol, manifests_dir=manifests_dir)
    except FileNotFoundError:
        return []
    rows = ev.get("evidence", []) if isinstance(ev, dict) else ev
    out: list = []
    for row in list(rows)[:top_k]:
        base = row.get("feature_key")
        if not base:
            continue
        for t in _DERIVE_TRANSFORMS:
            out.append(Hypothesis(
                id=f"der_{base}_{t}",
                description=f"{t}({base})",
                source=SOURCE_DERIVED,
            ))
    return out


def hypotheses_from_academic() -> list[Hypothesis]:
    """Fixed seed set of academic factor descriptors (agy 5b)."""
    return [Hypothesis(id=i, description=d, source=SOURCE_ACADEMIC) for i, d in _ACADEMIC_SEED]


def hypotheses_from_llm(raw: list) -> list[Hypothesis]:
    """Wrap LLM-proposed ideas into Hypothesis objects (actual LLM generation
    happens in 1C — this is just the adapter). `raw` entries must carry 'id'
    and 'description' (fail fast via KeyError on malformed LLM output);
    'dead_classes' is optional.

    ids are namespaced with an `llm_` prefix, consistent with the other three
    adapters (zoo_/der_/acad_) — an LLM-proposed raw id is caller-supplied and
    can otherwise collide verbatim with an academic/zoo/derived id. dedupe()
    only collapses on fingerprint (description content), not id, so an
    unprefixed collision would let two distinct hypotheses survive under the
    same Hypothesis.id into the final queue."""
    return [
        Hypothesis(
            id=f"llm_{r['id']}",
            description=r["description"],
            source=SOURCE_LLM,
            dead_classes=tuple(r.get("dead_classes", ())),
        )
        for r in raw
    ]


def build_queue(symbol: str, manifests_dir, zoo_dir, llm_raw: list | None = None) -> list[Hypothesis]:
    """Assemble the full Foundry hypothesis queue: zoo + evidence-derivation +
    academic + LLM sources, then dedupe (string-fingerprint collision) and
    filter_static (graveyard + constitution dead classes)."""
    collected = (
        hypotheses_from_zoo(zoo_dir)
        + hypotheses_from_evidence_derivation(symbol, manifests_dir)
        + hypotheses_from_academic()
        + hypotheses_from_llm(llm_raw or [])
    )
    return filter_static(dedupe(collected), symbol, manifests_dir)
