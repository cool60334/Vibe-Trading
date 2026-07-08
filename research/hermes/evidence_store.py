"""Per-symbol Foundry evidence store: foundry_evidence/foundry_evidence_<sym>.json.

Holds a list of EvidenceCard dicts. upsert = load -> replace-by-factor_id ->
atomic write-back (JSON analogue of the parquet no-append lesson; nightly
iterations must not clobber). Isolated from production evidence_<sym>.json.
"""
from __future__ import annotations

import json
from pathlib import Path

from research.hermes.evidence_card import EvidenceCard
from research.lib.factor_io import _atomic_write_text, _symbol_short

EVIDENCE_SUBDIR = "foundry_evidence"


def _store_path(symbol: str, manifests_dir: Path) -> Path:
    return Path(manifests_dir) / EVIDENCE_SUBDIR / f"foundry_evidence_{_symbol_short(symbol)}.json"


def load_cards(symbol: str, manifests_dir) -> list[EvidenceCard]:
    path = _store_path(symbol, Path(manifests_dir))
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [EvidenceCard.from_dict(d) for d in raw]


def upsert_card(card: EvidenceCard, symbol: str, manifests_dir) -> Path:
    path = _store_path(symbol, Path(manifests_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    cards = {c.factor_id: c for c in load_cards(symbol, manifests_dir)}
    cards[card.factor_id] = card
    payload = [c.to_dict() for c in cards.values()]
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True,
                                        ensure_ascii=False, allow_nan=False))
    return path
