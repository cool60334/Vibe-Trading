"""Factor induction (轉正): move a vetted Foundry factor's compute() code into
the production factor library. Human-gated (--confirm), re-validated hard.

Like promote.py, this is a privileged production write and is NOT exported from
research.hermes.__init__; it runs only when a human invokes it.
"""
from __future__ import annotations

import hashlib

from research.hermes.candidate_store import load_candidate_code
from research.hermes.errors import HermesGuardError
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.hermes.sandbox_ast import check_source, UnsafeCodeError


class InductRefused(HermesGuardError, RuntimeError):
    """Induction is not allowed (bad verdict / sha / AST / PIT / determinism / divergence / no confirm)."""


def verify_gates(factor_id: str, symbol: str, manifests_dir) -> str:
    """Load the stored code and run the cheap gates (verdict, sha, AST). Returns
    the code text. Raises InductRefused on any violation."""
    cards = {c.factor_id: c for c in load_cards(symbol, manifests_dir)}
    card = cards.get(factor_id)
    if card is None:
        raise InductRefused(f"no evidence card for {symbol}:{factor_id}")
    if card.verdict != VERDICT_CANDIDATE:
        raise InductRefused(f"{symbol}:{factor_id} verdict={card.verdict!r}, not a candidate")

    code, meta = load_candidate_code(factor_id, symbol, manifests_dir)
    actual = hashlib.sha256(code.encode()).hexdigest()
    if meta.get("code_sha256") and meta["code_sha256"] != actual:
        raise InductRefused(f"{symbol}:{factor_id} code sha mismatch (tamper?)")
    try:
        check_source(code)
    except UnsafeCodeError as exc:
        raise InductRefused(f"{symbol}:{factor_id} failed AST allowlist: {exc}") from exc
    return code
