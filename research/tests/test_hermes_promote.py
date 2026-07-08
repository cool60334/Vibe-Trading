import pandas as pd
import pytest
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.evidence_store import upsert_card
from research.hermes.candidate_store import write_candidate
from research.hermes.promote import promote_candidate, PromoteRefused


def _seed(tmp_path, verdict=VERDICT_CANDIDATE):
    idx = pd.date_range("2024-01-01", periods=5, freq="1h")
    write_candidate(pd.DataFrame({"mom5": [0.1, 0.2, 0.3, 0.4, 0.5]}, index=idx),
                    "eth", manifests_dir=tmp_path)
    card = EvidenceCard(
        factor_id="mom5", symbol="eth", source="llm", code_sha256="c" * 64,
        generated_at="2026-07-08T00:00:00+00:00", trial_step=1, interval="1H",
        formula="f", rationale="r", net_ic=0.03, ic_nonoverlap=0.02, ir=0.4,
        dsr=0.1, pbo=0.3, regime_ic={}, yearly_ic={}, nearest_factor=None,
        nearest_abs_spearman=None,
        verdict=verdict, death_reason=("dead" if verdict == VERDICT_GRAVEYARD else None),
    )
    upsert_card(card, "eth", manifests_dir=tmp_path)


def test_refuses_without_confirm(tmp_path):
    _seed(tmp_path)
    with pytest.raises(PromoteRefused):
        promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=False)


def test_refuses_graveyard_factor(tmp_path):
    _seed(tmp_path, verdict=VERDICT_GRAVEYARD)
    with pytest.raises(PromoteRefused):
        promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=True)


def test_refuses_existing_production_column_without_overwrite(tmp_path, monkeypatch):
    # agy #8: silent overwrite of a live feature is a real-money hazard
    _seed(tmp_path)
    import research.hermes.promote as promo
    monkeypatch.setattr(promo, "_production_feature_names", lambda s, m: {"mom5"})
    with pytest.raises(PromoteRefused):
        promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=True)


def test_confirmed_promote_writes_column_and_records_ledger(tmp_path, monkeypatch):
    _seed(tmp_path)
    calls, events = {}, []
    import research.hermes.promote as promo
    monkeypatch.setattr(promo, "_production_feature_names", lambda s, m: set())  # no clash
    def fake_append(symbol, key, series, manifests_dir=None, **kw):
        calls["symbol"], calls["key"], calls["n"] = symbol, key, len(series)
    def fake_event(manifests_dir, *, kind, symbol, strategy_id=None, detail=None):
        events.append((kind, symbol, detail))
    monkeypatch.setattr(promo, "append_feature_column", fake_append)
    monkeypatch.setattr(promo, "append_event", fake_event)
    promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=True)
    assert calls == {"symbol": "eth", "key": "mom5", "n": 5}
    assert events and events[0][0] == "promote"          # ledger audit trail (agy #9)


def test_promote_not_exported_from_package():
    # agy #5: a production-mutating gate must not be in the package namespace
    import research.hermes as pkg
    assert not hasattr(pkg, "promote_candidate")
