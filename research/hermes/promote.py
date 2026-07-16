"""Manual promote gate — the ONLY Foundry path that writes production features.

CRITICAL SAFETY CONSTRAINT
--------------------------
This module must NEVER be imported or called by any automatic chain — no stage,
no foundry runner, no cron, no dashboard button. Promotion moves a vetted
candidate feature into production features_<sym>.parquet (read by the live
trader). The real enforcement is two-fold: confirm defaults to False (nothing
happens on accidental call), and this module is deliberately NOT exported from
research/hermes/__init__ (so a stray `import research.hermes` cannot surface it).
It runs only when a human invokes it with confirm=True, exactly like
research/pipeline/final_holdout.py is the only path allowed to touch the final
holdout window.

    python -m research.hermes.promote --symbol eth --factor mom5 --confirm
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from research.hermes.candidate_store import _candidate_path
from research.hermes.errors import HermesGuardError
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.hermes.foundry_bridge import FOUNDRY_PREFIX
from research.lib.factor_io import (
    append_feature_column, load_features_meta, _default_manifests_dir, _symbol_short,
)
from research.lib.inducted_factors import inducted_names, _blacklisted
from research.lib.research_ledger import append_event


class PromoteRefused(HermesGuardError, RuntimeError):
    """Raised when a promotion is not allowed (no confirm / not a candidate / clash / missing)."""


def assert_promotable_factor_names(names, symbol) -> None:
    """Refuse a strategy that depends on a Foundry factor NOT yet inducted for
    this symbol, or on one that IS inducted but currently kill-switch
    blacklisted. An inducted factor's code is in the production library, so
    the trader can recompute it -> the strategy is safe to deploy -- unless
    it's blacklisted, in which case stage0a NaN-fills it daily and a strategy
    depending on it would deploy on an all-NaN signal."""
    inducted = inducted_names(symbol)
    blacklisted = _blacklisted()
    offenders = [n for n in names
                 if str(n).startswith(FOUNDRY_PREFIX) and (n not in inducted or n in blacklisted)]
    if offenders:
        raise PromoteRefused(
            f"strategy depends on non-inducted or blacklisted Foundry factor(s) "
            f"{offenders} for {symbol}: induct them first (research.hermes.induct) "
            "so production can recompute them, or the trader would pause/deploy "
            "on stale or NaN factor data."
        )


def _production_feature_names(symbol: str, manifests_dir: Path) -> set:
    """Existing production feature column names (empty if none yet)."""
    try:
        meta = load_features_meta(symbol, manifests_dir=manifests_dir)
    except FileNotFoundError:
        return set()
    return set(meta.get("feature_names", []))


def promote_candidate(factor_id: str, symbol: str, manifests_dir=None,
                       confirm: bool = False, overwrite: bool = False) -> None:
    if not confirm:
        raise PromoteRefused(
            f"promotion of {symbol}:{factor_id} refused: pass confirm=True "
            "(this writes PRODUCTION features read by the live trader)"
        )
    # agy #1: resolve default BEFORE any Path(manifests_dir) — Path(None) crashes.
    mdir = Path(manifests_dir) if manifests_dir is not None else _default_manifests_dir()

    assert_promotable_factor_names([factor_id], symbol)

    cards = {c.factor_id: c for c in load_cards(symbol, mdir)}
    card = cards.get(factor_id)
    if card is None:
        raise PromoteRefused(f"no evidence card for {symbol}:{factor_id}")
    if card.verdict != VERDICT_CANDIDATE:
        raise PromoteRefused(f"{symbol}:{factor_id} verdict={card.verdict!r}, not promotable")

    # agy #8: refuse to silently overwrite a live production feature.
    if factor_id in _production_feature_names(symbol, mdir) and not overwrite:
        raise PromoteRefused(
            f"{symbol}:{factor_id} already a production feature; pass overwrite=True to replace"
        )

    cand_path = _candidate_path(symbol, mdir)
    if not cand_path.exists():
        raise PromoteRefused(f"candidate parquet missing: {cand_path}")
    df = pd.read_parquet(cand_path)
    if factor_id not in df.columns:
        raise PromoteRefused(f"column {factor_id!r} not in candidate parquet {cand_path}")

    print(f"[promote] WRITING PRODUCTION feature {symbol}:{factor_id} "
          f"({len(df)} rows) — live trader will read this.")
    append_feature_column(symbol, factor_id, df[factor_id], manifests_dir=mdir)
    # agy #9: durable audit trail for a privileged production write.
    append_event(mdir, kind="promote", symbol=_symbol_short(symbol),
                 detail={"factor_id": factor_id, "code_sha256": card.code_sha256,
                         "overwrite": overwrite})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Manually promote a Foundry candidate to production.")
    p.add_argument("--symbol", required=True)
    p.add_argument("--factor", required=True)
    p.add_argument("--confirm", action="store_true",
                   help="required — without it the promotion is refused")
    p.add_argument("--overwrite", action="store_true",
                   help="allow replacing an existing production feature of the same name")
    args = p.parse_args(argv)
    symbol = _symbol_short(args.symbol)          # agy #10: normalise case up front
    try:
        promote_candidate(args.factor, symbol, manifests_dir=None,
                           confirm=args.confirm, overwrite=args.overwrite)
    # agy #2: append_feature_column raises ValueError (low coverage/all-NaN) +
    # FileNotFoundError (no production parquet yet) — catch alongside guard errors.
    except (HermesGuardError, ValueError, FileNotFoundError) as exc:
        print(f"[promote] REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
