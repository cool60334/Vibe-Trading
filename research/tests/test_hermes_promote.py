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
        formula="f", rationale="r", gross_ic=0.03, ic_nonoverlap=0.02, ir=0.4,
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


def test_promote_refuses_a_strategy_depending_on_a_foundry_factor():
    # The bridge only feeds RESEARCH. Production recomputes factors from the
    # library code, which has no Foundry factor in it -> the factor would go
    # stale immediately and pause the trader. Refuse, don't dead-end.
    from research.hermes.promote import assert_promotable_factor_names, PromoteRefused

    assert_promotable_factor_names(["funding_z", "basis_rel"], "eth")      # library-only: fine

    with pytest.raises(PromoteRefused, match="foundry_zoo_mom"):
        assert_promotable_factor_names(["funding_z", "foundry_zoo_mom"], "eth")


def test_promote_candidate_refuses_foundry_factor_early(tmp_path):
    # The guard fires BEFORE evidence card / candidate parquet checks, so even
    # with an empty manifests_dir (no fixtures), promotion of a foundry_ factor
    # is refused immediately with a clear error message.
    with pytest.raises(PromoteRefused, match="strategy depends on non-inducted or blacklisted Foundry factor"):
        promote_candidate("foundry_zoo_mom", "eth", manifests_dir=tmp_path, confirm=True)


def test_promote_guard_allows_an_inducted_factor_for_that_symbol(tmp_path, monkeypatch):
    import pytest
    from research.hermes import promote as pm
    from research.hermes.promote import assert_promotable_factor_names, PromoteRefused

    # eth has foundry_x inducted; btc does not
    from research.lib.inducted_factors import inducted_dir
    d = inducted_dir("eth", root=tmp_path); d.mkdir(parents=True)
    (d / "foundry_x.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")
    (d / "foundry_x.meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pm, "inducted_names",
                        lambda symbol: __import__("research.lib.inducted_factors",
                        fromlist=["inducted_names"]).inducted_names(symbol, root=tmp_path))

    assert_promotable_factor_names(["funding_z", "foundry_x"], "eth")     # inducted -> allowed

    with pytest.raises(PromoteRefused, match="foundry_x"):
        assert_promotable_factor_names(["foundry_x"], "btc")             # not inducted for btc


def test_promote_guard_refuses_a_blacklisted_inducted_factor(tmp_path, monkeypatch):
    # An inducted-but-blacklisted (emergency kill-switch) factor is NaN-filled
    # daily by stage0a -- a strategy depending on it must not pass promotion,
    # or the trader deploys on an all-NaN signal.
    from research.hermes import promote as pm
    from research.hermes.promote import assert_promotable_factor_names, PromoteRefused
    from research.lib.inducted_factors import inducted_dir

    d = inducted_dir("eth", root=tmp_path); d.mkdir(parents=True)
    (d / "foundry_x.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")
    (d / "foundry_x.meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pm, "inducted_names",
                        lambda symbol: __import__("research.lib.inducted_factors",
                        fromlist=["inducted_names"]).inducted_names(symbol, root=tmp_path))

    bl = tmp_path / "bl.txt"; bl.write_text("foundry_x\n", encoding="utf-8")
    monkeypatch.setenv("INDUCTED_BLACKLIST_FILE", str(bl))

    with pytest.raises(PromoteRefused, match="foundry_x"):
        assert_promotable_factor_names(["foundry_x"], "eth")             # inducted but blacklisted
