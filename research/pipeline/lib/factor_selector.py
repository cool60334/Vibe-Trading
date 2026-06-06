"""
research/pipeline/lib/factor_selector.py
────────────────────────────────────────
Deterministic factor candidate selection from a Stage 0a evidence manifest.

This is the engine behind the *deterministic* path of Stage 0 (factor
discovery). It replaces the previous swarm-primary model — which was
unreliable because LLMs sometimes failed to emit a parseable ```json fence —
with a pure-function rule based on the IC / IR statistics that Stage 0a
already produced.

Design (see `openspec/changes/stage0-deterministic-discovery/design.md`):

  1. For each evidence entry, find the horizon with maximum |IC|.
  2. Drop entries where `|IC@top| < min_abs_ic` or `|IR| < min_abs_ir`.
  3. Drop entries whose `feature_key` is not in the feature store columns.
  4. Sort surviving entries by `|IC@top|` descending; keep the top
     `max_candidates`.
  5. Convert each entry into a fully-populated `FactorCandidate` with:
       - `expected_ic_sign` = sign(IC@top)
       - `horizons_h`       = [top_h] ∪ {h : IC[h]·IC[top_h] > 0 and |IC[h]| > min_abs_ic/2}
       - `economic_logic`   = deterministic placeholder string (Stage 0
                              enrichment mode may overwrite later)
       - `category`         = pass-through from evidence entry

All functions in this module are pure (no I/O) so they can be unit tested
without any pytest fixtures beyond plain dicts. Stage 0 is responsible for
loading evidence JSON / reading parquet columns / writing manifests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable

# ── Path bootstrap ─────────────────────────────────────────────────────────────
# Lives at <repo>/research/pipeline/lib/factor_selector.py.
_THIS = Path(__file__).resolve()
_RESEARCH_DIR = _THIS.parents[2]          # research/
_REPO_ROOT = _RESEARCH_DIR.parent         # repo root
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"

for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from schemas import FactorCandidate  # noqa: E402

# ─── Defaults ────────────────────────────────────────────────────────────────

#: Minimum |IC| at the top horizon required to retain a factor.
DEFAULT_MIN_ABS_IC: float = 0.05

#: Minimum |IR| required to retain a factor.
DEFAULT_MIN_ABS_IR: float = 0.10

#: Maximum number of candidates returned after ranking.
DEFAULT_MAX_CANDIDATES: int = 6

#: Allowed `FactorCandidate.category` literal values. Categories outside this
#: set get coerced to ``"momentum"`` (a reasonable default) to keep schema
#: validation green when the evidence pool grows beyond the canonical set.
_ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {"funding", "basis", "oi", "momentum", "volatility", "stablecoin", "whale", "skew"}
)

#: Mapping for non-canonical category names that show up in evidence to the
#: closest allowed `FactorCandidate.category` literal.
_CATEGORY_ALIASES: dict[str, str] = {
    "trend": "momentum",
    "volume": "oi",
}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _coerce_category(raw_category: str | None) -> str:
    """Map an evidence category string to a valid FactorCandidate category.

    Args:
        raw_category: Category string from the evidence entry; may be None.

    Returns:
        A string member of ``_ALLOWED_CATEGORIES``. Unknown values fall back
        to ``"momentum"``.
    """
    if raw_category is None:
        return "momentum"
    if raw_category in _ALLOWED_CATEGORIES:
        return raw_category
    if raw_category in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[raw_category]
    return "momentum"


def _top_horizon_and_ic(ic_by_horizon: dict[Any, Any]) -> tuple[int, float] | None:
    """Return (top_horizon, ic_at_top) by maximum absolute IC, ignoring None.

    Evidence JSON serialises horizon keys as strings; this function tolerates
    either str or int keys and returns the horizon as int.

    Args:
        ic_by_horizon: Mapping horizon → IC; values may be None.

    Returns:
        ``(top_horizon_int, ic_float)`` or ``None`` if no valid IC values
        remain after filtering Nones.
    """
    cleaned: dict[int, float] = {}
    for h, ic in (ic_by_horizon or {}).items():
        if ic is None:
            continue
        try:
            cleaned[int(h)] = float(ic)
        except (TypeError, ValueError):
            continue
    if not cleaned:
        return None
    top_h = max(cleaned, key=lambda h: abs(cleaned[h]))
    return top_h, cleaned[top_h]


def _related_horizons(
    ic_by_horizon: dict[Any, Any],
    top_h: int,
    top_ic: float,
    min_abs_ic: float,
) -> list[int]:
    """Return horizons with same sign as top_ic and |IC| > min_abs_ic / 2.

    Always includes ``top_h``. Sorted ascending for stable output.

    Args:
        ic_by_horizon: Mapping horizon → IC.
        top_h:         The horizon with maximum |IC|.
        top_ic:        IC at top_h (used for sign comparison).
        min_abs_ic:    Threshold; horizons must have |IC| > this/2 to be
                       included alongside the top horizon.

    Returns:
        Sorted list of int horizons.
    """
    half_threshold = abs(min_abs_ic) / 2.0
    keep: set[int] = {int(top_h)}
    sign = 1.0 if top_ic >= 0 else -1.0
    for h, ic in (ic_by_horizon or {}).items():
        if ic is None:
            continue
        try:
            ic_f = float(ic)
            h_i = int(h)
        except (TypeError, ValueError):
            continue
        if h_i == int(top_h):
            continue
        if ic_f == 0.0:
            continue
        same_sign = (ic_f * sign) > 0
        if same_sign and abs(ic_f) > half_threshold:
            keep.add(h_i)
    return sorted(keep)


def _placeholder_economic_logic(
    feature_key: str, top_h: int, top_ic: float, ir: float | None
) -> str:
    """Return the deterministic placeholder text for `economic_logic`.

    Stage 0 enrichment mode (when `--use-swarm` is on) may overwrite this
    field with a swarm-generated explanation; the placeholder stays in place
    on enrichment failure so the manifest is always self-describing.

    Args:
        feature_key: Column name in the feature store.
        top_h:       Horizon (hours) with maximum |IC|.
        top_ic:      IC value at the top horizon.
        ir:          Information ratio; may be None.

    Returns:
        A single-line human-readable sentence containing the IC / IR / horizon
        figures so downstream readers can audit the selection without
        cross-referencing the evidence file.
    """
    ir_str = f"{ir:+.3f}" if isinstance(ir, (int, float)) else "n/a"
    return (
        f"(deterministic selection) {feature_key} "
        f"top |IC|={top_ic:+.4f}@{top_h}h, IR={ir_str}. "
        f"Selected by IC/IR threshold pass; awaiting swarm enrichment for "
        f"economic rationale."
    )


# ─── Ranking ─────────────────────────────────────────────────────────────────


def _rank_evidence_entries(
    evidence_entries: Iterable[dict[str, Any]],
    *,
    feature_store_cols: set[str],
    min_abs_ic: float,
    min_abs_ir: float,
) -> list[tuple[dict[str, Any], int, float]]:
    """Filter + sort evidence entries by |IC| at the top horizon.

    Args:
        evidence_entries:   Iterable of evidence entry dicts (as found in
                            ``evidence_<sym>.json["evidence"]``).
        feature_store_cols: Set of column names present in the feature store
                            parquet for the symbol.
        min_abs_ic:         Minimum |IC| at the top horizon.
        min_abs_ir:         Minimum |IR|.

    Returns:
        List of ``(entry, top_horizon, top_ic)`` tuples, sorted by
        ``|top_ic|`` descending. Entries failing any filter are excluded.
    """
    ranked: list[tuple[dict[str, Any], int, float]] = []
    for entry in evidence_entries:
        feature_key = entry.get("feature_key")
        if not feature_key or feature_key not in feature_store_cols:
            continue

        ic_pair = _top_horizon_and_ic(entry.get("ic_by_horizon") or {})
        if ic_pair is None:
            continue
        top_h, top_ic = ic_pair
        if abs(top_ic) < min_abs_ic:
            continue

        ir_raw = entry.get("ir")
        try:
            ir = float(ir_raw) if ir_raw is not None else 0.0
        except (TypeError, ValueError):
            ir = 0.0
        if abs(ir) < min_abs_ir:
            continue

        ranked.append((entry, top_h, top_ic))

    ranked.sort(key=lambda triple: abs(triple[2]), reverse=True)
    return ranked


# ─── Candidate construction ──────────────────────────────────────────────────


def _build_candidate_from_entry(
    entry: dict[str, Any], top_h: int, top_ic: float, *, min_abs_ic: float
) -> FactorCandidate:
    """Build a FactorCandidate from one already-ranked evidence entry.

    Args:
        entry:       The evidence entry dict.
        top_h:       Horizon with maximum |IC| (precomputed).
        top_ic:      IC value at the top horizon.
        min_abs_ic:  Threshold reused to find same-sign related horizons.

    Returns:
        A validated FactorCandidate object.
    """
    feature_key = str(entry["feature_key"])
    category = _coerce_category(entry.get("category"))
    ir_raw = entry.get("ir")
    try:
        ir = float(ir_raw) if ir_raw is not None else None
    except (TypeError, ValueError):
        ir = None

    horizons = _related_horizons(
        entry.get("ic_by_horizon") or {}, top_h, top_ic, min_abs_ic
    )
    expected_sign: str = "+" if top_ic >= 0 else "-"

    return FactorCandidate(
        name=feature_key,
        formula=f"feature_store column '{feature_key}' (no transform)",
        feature_key=feature_key,
        data_source=None,
        transform=None,
        expected_ic_sign=expected_sign,  # type: ignore[arg-type]
        economic_logic=_placeholder_economic_logic(feature_key, top_h, top_ic, ir),
        horizons_h=horizons,
        category=category,  # type: ignore[arg-type]
    )


# ─── Public API ──────────────────────────────────────────────────────────────


def select_candidates_from_evidence(
    evidence: dict[str, Any] | list[dict[str, Any]],
    feature_store_cols: set[str],
    *,
    min_abs_ic: float = DEFAULT_MIN_ABS_IC,
    min_abs_ir: float = DEFAULT_MIN_ABS_IR,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[FactorCandidate]:
    """Pick top-K factors from a Stage 0a evidence manifest, deterministically.

    Accepts either:
      - A full evidence manifest dict (with a top-level ``"evidence"`` key
        whose value is the list of entries — the format Stage 0a writes), OR
      - A bare list of evidence entries (useful for tests).

    Args:
        evidence:            Evidence manifest dict or list of entries.
        feature_store_cols:  Set of column names in features_<sym>.parquet.
        min_abs_ic:          Minimum |IC| at the top horizon required to keep
                             a factor. Default: 0.05.
        min_abs_ir:          Minimum |IR| required. Default: 0.10.
        max_candidates:      Hard cap on returned list length. Default: 6.

    Returns:
        List of FactorCandidate objects, length ∈ ``[0, max_candidates]``,
        sorted by |IC@top| descending. Returns an empty list if no factor
        passes both thresholds (which is a legitimate "no edge here" signal
        — callers MUST NOT treat empty as a runtime error).
    """
    if isinstance(evidence, dict):
        raw_entries = evidence.get("evidence", []) or []
    else:
        raw_entries = list(evidence)

    ranked = _rank_evidence_entries(
        raw_entries,
        feature_store_cols=set(feature_store_cols),
        min_abs_ic=min_abs_ic,
        min_abs_ir=min_abs_ir,
    )

    candidates: list[FactorCandidate] = []
    for entry, top_h, top_ic in ranked[: max(0, int(max_candidates))]:
        candidates.append(
            _build_candidate_from_entry(entry, top_h, top_ic, min_abs_ic=min_abs_ic)
        )
    return candidates
