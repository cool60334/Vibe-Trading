"""Foundry hypothesis descriptor + normalised string fingerprint (Phase 1B, v2).

A Hypothesis says WHAT to try; it carries no computation (codegen + IC live in
1C/1A). The fingerprint is a whitespace/case-normalised STRING hash — NOT an AST
hash: agy's 1B review showed AST variable-normalisation wrongly merges factors
with different input columns (close/… vs volume/…) and fails entirely on zoo
LaTeX and multi-line LLM code. Cross-representation and semantic dedup are 1A's
numerical job (nearest_correlate) + code_sha256 after 1C codegen."""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field

from research.hermes.errors import HermesGuardError

SOURCE_ZOO = "zoo"
SOURCE_LLM = "llm"
SOURCE_DERIVED = "derived"
SOURCE_ACADEMIC = "academic"
_SOURCES = {SOURCE_ZOO, SOURCE_LLM, SOURCE_DERIVED, SOURCE_ACADEMIC}


class HypothesisValidationError(HermesGuardError, ValueError):
    """Raised when a Hypothesis violates its schema invariants."""


def string_fingerprint(text: str) -> str:
    """sha256 of the case-folded, whitespace-collapsed string."""
    key = " ".join(text.split()).lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def is_python_expr(text: str) -> bool:
    """True if text parses as Python (an executable descriptor, not LaTeX/NL)."""
    try:
        ast.parse(text, mode="exec")
        return True
    except SyntaxError:
        return False


@dataclass(frozen=True)
class Hypothesis:
    id: str
    description: str            # Python expr, LaTeX, or NL description
    source: str                 # zoo | llm | derived | academic
    dead_classes: tuple = ()    # constitution class tags (filled by adapters, Task 3/4)
    fingerprint: str = field(default="", compare=False)

    def __post_init__(self):
        if self.source not in _SOURCES:
            raise HypothesisValidationError(f"source must be one of {_SOURCES}, got {self.source!r}")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", string_fingerprint(self.description))
