import json

from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE
from research.hermes.evidence_store import upsert_card, load_cards, EVIDENCE_SUBDIR


def _card(factor_id, gross_ic=0.03):
    return EvidenceCard(
        factor_id=factor_id, symbol="eth", source="llm", code_sha256="b" * 64,
        generated_at="2026-07-08T00:00:00+00:00", trial_step=1, interval="1H",
        formula="f", rationale="r", gross_ic=gross_ic, ic_nonoverlap=0.02, ir=0.4,
        dsr=0.1, pbo=0.3, regime_ic={}, yearly_ic={}, nearest_factor=None,
        nearest_abs_spearman=None, verdict=VERDICT_CANDIDATE, death_reason=None,
    )


def test_upsert_appends_then_updates_by_factor_id(tmp_path):
    upsert_card(_card("a"), "eth", manifests_dir=tmp_path)
    upsert_card(_card("b"), "eth", manifests_dir=tmp_path)
    upsert_card(_card("a", gross_ic=0.099), "eth", manifests_dir=tmp_path)  # update a
    cards = load_cards("eth", manifests_dir=tmp_path)
    assert {c.factor_id for c in cards} == {"a", "b"}                     # no dup
    assert next(c for c in cards if c.factor_id == "a").gross_ic == 0.099   # updated


def test_written_json_is_valid_and_under_evidence_subdir(tmp_path):
    path = upsert_card(_card("a"), "eth", manifests_dir=tmp_path)
    assert EVIDENCE_SUBDIR in path.parts
    json.loads(path.read_text(encoding="utf-8"))          # valid JSON (nan-safe)
    assert not list(path.parent.glob("*.tmp"))            # atomic, no leftover


def test_load_missing_returns_empty(tmp_path):
    assert load_cards("eth", manifests_dir=tmp_path) == []
