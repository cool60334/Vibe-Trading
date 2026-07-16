"""Factor induction (轉正): move a vetted Foundry factor's compute() code into
the production factor library. Human-gated (--confirm), re-validated hard.

Like promote.py, this is a privileged production write and is NOT exported from
research.hermes.__init__; it runs only when a human invokes it.
"""
from __future__ import annotations

import hashlib
import pandas as pd

from research.hermes.candidate_store import load_candidate_code, _candidate_path
from research.hermes.errors import HermesGuardError
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.hermes.sandbox_ast import check_source, UnsafeCodeError
from research.hermes.foundry_bridge import recompute_full_span, reconciles_pre_oos
from research.hermes.forge import pit_check_via_sandbox
from research.hermes.pit import LookaheadError


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


def revalidate(code, factor_id, symbol, panel, run_sandbox, oos_start, manifests_dir) -> None:
    """The heavy re-validation gates. Raises InductRefused on any failure.
      - full-span PIT re-check (no future leak in the non-sandboxed prod path)
      - determinism (compute twice -> identical; catches leaked RNG/global state)
      - path-consistency (recompute's pre-oos == the values Foundry stored, i.e.
        what the strategy was backtested on -> live matches the backtest)."""
    first = recompute_full_span(code, panel, run_sandbox)
    try:
        pit_check_via_sandbox(code, panel, first, run_sandbox)
    except LookaheadError as exc:
        raise InductRefused(f"{symbol}:{factor_id} peeks into the future: {exc}") from exc

    second = recompute_full_span(code, panel, run_sandbox)
    if not first.equals(second):
        raise InductRefused(f"{symbol}:{factor_id} is non-deterministic (compute twice differs)")

    cand_path = _candidate_path(symbol, manifests_dir)
    if not cand_path.exists():
        raise InductRefused(
            f"{symbol}:{factor_id} has no stored candidate values at {cand_path} "
            "(cannot verify path-consistency); refusing to induct")
    stored = pd.read_parquet(cand_path)
    if factor_id not in stored.columns:
        raise InductRefused(
            f"{symbol}:{factor_id} is not a column in the stored candidate values "
            "(cannot verify path-consistency); refusing to induct")
    if not reconciles_pre_oos(first, stored[factor_id], oos_start):
        raise InductRefused(
            f"{symbol}:{factor_id} recompute diverges from the stored Foundry values "
            "(the strategy's backtest would not match live); refusing to induct")
