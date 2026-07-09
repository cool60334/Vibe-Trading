"""EvidenceCard: the contract for one Foundry factor's verdict + evidence.

Schema-first (agy 1E): every metric the statistical gatekeeper (1A) will emit
is named here before 1A exists. A card is either a promotable CANDIDATE or a
GRAVEYARD entry with a death_reason. JSON is nan-safe: non-finite floats
serialise to null so json.dumps(..., allow_nan=False) never emits invalid JSON.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field, fields
from typing import Optional

from research.hermes.errors import HermesGuardError

VERDICT_CANDIDATE = "candidate"
VERDICT_GRAVEYARD = "graveyard"
_VERDICTS = {VERDICT_CANDIDATE, VERDICT_GRAVEYARD}
# metrics a promotable candidate must carry: gross_ic/ic_nonoverlap/pbo are the
# quality gate itself and must be present *and finite* (None or nan/inf =>
# rejected at construction -- a candidate can't clear the gate on an undefined
# number). ir/dsr are excluded from this gate: they may legitimately be
# non-finite (undefined for low trial counts) without blocking candidacy, and
# are allowed to sanitize to null on to_dict() without issue.
_CORE_METRICS = ("gross_ic", "ic_nonoverlap", "pbo")


class CardValidationError(HermesGuardError, ValueError):
    """Raised when an EvidenceCard violates its schema invariants."""


def _sanitize(v):
    """Recursively map non-finite floats (nan/inf/-inf) to None so
    json.dumps(..., allow_nan=False) can never emit invalid JSON — including
    NaN nested inside regime_ic/yearly_ic dicts (agy 二審 finding #3)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize(x) for x in v]
    return v


@dataclass(frozen=True)
class EvidenceCard:
    # provenance + context (required — always known at construction)
    factor_id: str
    symbol: str
    source: str                 # zoo | llm | derived | academic
    code_sha256: str
    generated_at: str           # ISO-8601 UTC
    trial_step: int
    interval: str               # "1H" / "1D" — candle the factor was computed on
    formula: str
    rationale: str
    verdict: str
    # metrics + context (default None => forward-compatible: an older card
    # missing a later-added field still loads via from_dict, agy #7)
    gross_ic: Optional[float] = None
    ic_nonoverlap: Optional[float] = None
    ir: Optional[float] = None
    dsr: Optional[float] = None
    pbo: Optional[float] = None
    turnover: Optional[float] = None        # raw turnover behind gross_ic (agy #6)
    n_samples: Optional[int] = None         # sample count behind the IC (agy #6)
    regime_ic: dict = field(default_factory=dict)   # {"bull":..,"bear":..,"chop":..}
    yearly_ic: dict = field(default_factory=dict)   # {"2022":.., ...}
    nearest_factor: Optional[str] = None
    nearest_abs_spearman: Optional[float] = None
    death_reason: Optional[str] = None

    def __post_init__(self):
        if self.verdict not in _VERDICTS:
            raise CardValidationError(f"verdict must be one of {_VERDICTS}, got {self.verdict!r}")
        if self.verdict == VERDICT_GRAVEYARD and not self.death_reason:
            raise CardValidationError("graveyard card requires a death_reason")
        if self.verdict == VERDICT_CANDIDATE:
            missing = [
                m for m in _CORE_METRICS
                if (v := getattr(self, m)) is None or (isinstance(v, float) and not math.isfinite(v))
            ]
            if missing:
                raise CardValidationError(f"candidate card missing/non-finite core metrics: {missing}")

    def to_dict(self) -> dict:
        return {k: _sanitize(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceCard":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
