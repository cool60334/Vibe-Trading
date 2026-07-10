import pytest

from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO, SOURCE_LLM
from research.hermes.hypothesis_queue import dedupe


def test_dedupe_keeps_executable_on_collision():
    latex = Hypothesis("zoo_a", "close.shift(5)", SOURCE_ZOO)
    py = Hypothesis("llm_a", "close.shift(5)", SOURCE_LLM)        # same fingerprint
    # both same fingerprint; keep the executable one regardless of order
    out = dedupe([latex, py])
    assert len(out) == 1 and out[0].id == "llm_a"
    out2 = dedupe([py, latex])
    assert len(out2) == 1 and out2[0].id == "llm_a"


def test_dedupe_distinct_fingerprints_all_kept():
    a = Hypothesis("a", "close.shift(5)", SOURCE_ZOO)
    b = Hypothesis("b", "close.shift(9)", SOURCE_ZOO)
    assert len(dedupe([a, b])) == 2


def test_dedupe_tiebreak_deterministic_nonzoo_nonzoo_both_executable():
    # Both non-zoo AND both executable-Python -> _priority ties on both of the
    # first two axes. The winner must still be order-independent (id tiebreak).
    a = Hypothesis("hyp_a", "close.shift(5)", SOURCE_LLM)
    b = Hypothesis("hyp_b", "close.shift(5)", SOURCE_LLM)        # same fingerprint
    out_ab = dedupe([a, b])
    out_ba = dedupe([b, a])
    assert len(out_ab) == 1 and len(out_ba) == 1
    assert out_ab[0].id == out_ba[0].id == "hyp_b"  # greater id wins, both orders agree


def test_dedupe_tiebreak_deterministic_zoo_zoo_both_nonexecutable():
    # Both zoo AND both non-parseable (LaTeX-like) -> ties on both axes again.
    latex = r"\frac{close}{shift(5)}"
    a = Hypothesis("zoo_a2", latex, SOURCE_ZOO)
    b = Hypothesis("zoo_b2", latex, SOURCE_ZOO)                  # same fingerprint
    out_ab = dedupe([a, b])
    out_ba = dedupe([b, a])
    assert len(out_ab) == 1 and len(out_ba) == 1
    assert out_ab[0].id == out_ba[0].id == "zoo_b2"


def test_dedupe_merges_dead_classes_on_collision_zoo_loses():
    # zoo hypothesis is tagged dead_classes; the non-zoo duplicate is untagged
    # and wins selection on the source axis. The ban tag must not be dropped.
    zoo = Hypothesis(
        "zoo_x", "close.shift(5)", SOURCE_ZOO,
        dead_classes=("intraday_ohlcv_price_derived",),
    )
    llm = Hypothesis("llm_x", "close.shift(5)", SOURCE_LLM)      # untagged, wins on source axis
    out = dedupe([zoo, llm])
    assert len(out) == 1
    winner = out[0]
    assert winner.id == "llm_x"  # non-zoo still wins selection
    assert winner.dead_classes == ("intraday_ohlcv_price_derived",)  # tag preserved via union


def test_dedupe_merges_dead_classes_partial_overlap():
    a = Hypothesis("a1", "close.shift(5)", SOURCE_LLM, dead_classes=("class_x",))
    b = Hypothesis("b1", "close.shift(5)", SOURCE_LLM, dead_classes=("class_y", "class_x"))
    out = dedupe([a, b])
    assert len(out) == 1
    assert set(out[0].dead_classes) == {"class_x", "class_y"}


def test_dedupe_preserves_fingerprint_after_dead_classes_merge():
    zoo = Hypothesis("zoo_y", "close.shift(7)", SOURCE_ZOO, dead_classes=("tag1",))
    llm = Hypothesis("llm_y", "close.shift(7)", SOURCE_LLM)
    out = dedupe([zoo, llm])
    assert len(out) == 1
    assert out[0].fingerprint == llm.fingerprint  # replace() didn't corrupt the precomputed fingerprint


def test_filter_removes_graveyard_and_dead_classes(tmp_path, monkeypatch):
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO, SOURCE_LLM, string_fingerprint
    from research.hermes import hypothesis_queue as hq
    from research.hermes.hypothesis_queue import filter_static, DEAD_CLASSES

    buried = Hypothesis("z1", "close / close.shift(3) - 1", SOURCE_ZOO)
    fresh = Hypothesis("z2", "volume / volume.shift(3) - 1", SOURCE_ZOO)
    banned = Hypothesis("z3", "some microstructure thing", SOURCE_LLM,
                        dead_classes=("intraday_ohlcv_price_derived",))

    monkeypatch.setattr(hq, "_graveyard_fingerprints",
                        lambda sym, md: {string_fingerprint("close / close.shift(3) - 1")})
    out = filter_static([buried, fresh, banned], symbol="eth", manifests_dir=tmp_path)
    assert [h.id for h in out] == ["z2"]           # buried + dead-class removed


def test_dead_classes_constant_covers_constitution():
    from research.hermes.hypothesis_queue import DEAD_CLASSES
    assert "intraday_ohlcv_price_derived" in DEAD_CLASSES
    assert "binance_orderflow" in DEAD_CLASSES


def test_graveyard_fingerprints_real_evidence_store_round_trip(tmp_path):
    """Real integration: write cards via the real evidence_store.upsert_card,
    read them back through the real (unmocked) _graveyard_fingerprints, and
    confirm graveyard formulas are fingerprinted while non-graveyard formulas
    are excluded. The only other test covering _graveyard_fingerprints
    monkeypatches it away entirely, leaving the real evidence-store
    read/verdict-filter/formula round-trip with zero coverage."""
    from research.hermes.evidence_card import EvidenceCard, VERDICT_GRAVEYARD, VERDICT_CANDIDATE
    from research.hermes.evidence_store import upsert_card
    from research.hermes.hypothesis import string_fingerprint
    from research.hermes.hypothesis_queue import _graveyard_fingerprints

    symbol = "eth"
    dead_formula = "close / close.shift(3) - 1"
    alive_formula = "volume / volume.shift(3) - 1"

    dead_card = EvidenceCard(
        factor_id="dead_1",
        symbol=symbol,
        source="zoo",
        code_sha256="a" * 64,
        generated_at="2026-07-01T00:00:00Z",
        trial_step=1,
        interval="1H",
        formula=dead_formula,
        rationale="buried in a prior trial",
        verdict=VERDICT_GRAVEYARD,
        death_reason="ic below gate",
    )
    alive_card = EvidenceCard(
        factor_id="alive_1",
        symbol=symbol,
        source="zoo",
        code_sha256="b" * 64,
        generated_at="2026-07-01T00:00:00Z",
        trial_step=1,
        interval="1H",
        formula=alive_formula,
        rationale="promotable candidate",
        verdict=VERDICT_CANDIDATE,
        gross_ic=0.05,
        ic_nonoverlap=0.04,
        pbo=0.2,
    )
    upsert_card(dead_card, symbol, tmp_path)
    upsert_card(alive_card, symbol, tmp_path)

    fps = _graveyard_fingerprints(symbol, tmp_path)
    assert string_fingerprint(dead_formula) in fps
    assert string_fingerprint(alive_formula) not in fps


def test_zoo_adapter_reads_meta_and_maps_dead_class(tmp_path):
    from research.hermes.hypothesis_queue import hypotheses_from_zoo
    zoo = tmp_path / "zoo"; zoo.mkdir()
    (zoo / "micro.py").write_text(
        "__alpha_meta__ = {'id': 'gtja_micro', 'theme': ['microstructure'],\n"
        " 'formula_latex': 'foo'}\n"
        "raise RuntimeError('compute must NOT run')\n", encoding="utf-8")
    (zoo / "mom.py").write_text(
        "__alpha_meta__ = {'id': 'q_roc5', 'theme': ['momentum'], 'formula_latex': 'bar'}\n",
        encoding="utf-8")
    hyps = {h.id: h for h in hypotheses_from_zoo(zoo)}
    assert set(hyps) == {"zoo_gtja_micro", "zoo_q_roc5"}
    assert "intraday_ohlcv_price_derived" in hyps["zoo_gtja_micro"].dead_classes  # theme mapped
    assert hyps["zoo_q_roc5"].dead_classes == ()


def test_zoo_adapter_real_zoo_directory_sanity():
    """Regression guard on the real 452+ alpha zoo (agent/src/factors/zoo): the
    naive non-greedy-regex approach to meta extraction silently truncates on
    academic/*.py files whose formula_latex contains literal braces (e.g.
    r'\\mathrm{zscore}_{x}...' in carhart_mom.py), so this exercises the
    ast-based extractor against real files, not just the synthetic fixture
    above."""
    from pathlib import Path as _Path

    from research.hermes.hypothesis import SOURCE_ZOO
    from research.hermes.hypothesis_queue import hypotheses_from_zoo

    repo_root = _Path(__file__).resolve().parents[2]
    zoo_dir = repo_root / "agent" / "src" / "factors" / "zoo"
    assert zoo_dir.is_dir(), f"real zoo dir missing at {zoo_dir}"

    hyps = hypotheses_from_zoo(zoo_dir)
    assert len(hyps) >= 450, f"expected ~450+ zoo hypotheses, got {len(hyps)}"
    assert all(h.source == SOURCE_ZOO for h in hyps)
    assert len({h.id for h in hyps}) == len(hyps)  # ids unique

    # the LaTeX-brace file that breaks a non-greedy regex must survive intact,
    # not get truncated at the first literal '}' inside \mathrm{zscore}...
    carhart = next(h for h in hyps if h.id == "zoo_academic_carhart_mom")
    assert r"\mathrm{zscore}" in carhart.description
    assert carhart.description.count("close") >= 2  # both close_t terms present, not truncated

    # at least one real gtja191 microstructure-themed factor got dead-class tagged
    assert any("intraday_ohlcv_price_derived" in h.dead_classes for h in hyps)
def test_llm_adapter_namespaces_ids():
    from research.hermes.hypothesis_queue import hypotheses_from_llm
    hyps = hypotheses_from_llm([{"id": "ts_mom_2", "description": "close.shift(2)"}])
    assert len(hyps) == 1
    assert hyps[0].id == "llm_ts_mom_2"
    assert hyps[0].source == SOURCE_LLM


def test_llm_adapter_requires_id_and_description():
    from research.hermes.hypothesis_queue import hypotheses_from_llm
    with pytest.raises(KeyError):
        hypotheses_from_llm([{"description": "no id here"}])
    with pytest.raises(KeyError):
        hypotheses_from_llm([{"id": "no_description_here"}])


def test_llm_adapter_does_not_collide_with_academic_id_after_prefixing():
    """Regression: an LLM-proposed raw id equal to an academic seed id (e.g.
    'acad_ts_mom') must NOT collide with the academic hypothesis after
    namespacing — dedupe() only collapses on fingerprint, not id, so an
    unprefixed collision would silently let two DIFFERENT hypotheses survive
    under the same Hypothesis.id in the final queue."""
    from research.hermes.hypothesis_queue import hypotheses_from_academic, hypotheses_from_llm

    academic = hypotheses_from_academic()
    acad_hit = next(h for h in academic if h.id == "acad_ts_mom")

    # Same raw id string as the academic hypothesis, but a different formula.
    llm = hypotheses_from_llm([{"id": "acad_ts_mom", "description": "close.shift(3)"}])
    assert len(llm) == 1
    assert llm[0].id == "llm_acad_ts_mom"
    assert llm[0].id != acad_hit.id
    assert llm[0].description != acad_hit.description


def test_build_queue_assembles_dedupes_filters(tmp_path, monkeypatch):
    from research.hermes import hypothesis_queue as hq
    from research.hermes.hypothesis import Hypothesis, SOURCE_ACADEMIC
    monkeypatch.setattr(hq, "hypotheses_from_zoo", lambda d: [
        Hypothesis("zoo_a", "close / close.shift(5) - 1", "zoo")])
    monkeypatch.setattr(hq, "hypotheses_from_derivation", lambda bases: [
        Hypothesis("der_a", "close / close.shift(5) - 1", "derived")])   # dupe of zoo_a
    monkeypatch.setattr(hq, "hypotheses_from_academic", lambda: [
        Hypothesis("acad_a", "close.rolling(20).mean()", SOURCE_ACADEMIC)])
    monkeypatch.setattr(hq, "_graveyard_fingerprints", lambda s, m: set())
    q = {h.id for h in hq.build_queue("eth", tmp_path, zoo_dir=tmp_path, llm_raw=[],
                                      derived_bases=["close"])}
    assert "acad_a" in q and len(q & {"zoo_a", "der_a"}) == 1    # one of the dupes kept


# ── selection-bias fix: derived hypotheses must not be picked using evidence_<sym>.json ──
#
# stage0a_features computes evidence_<sym>.json's IC over the FULL history,
# including the reserved walk-forward OOS window and the final holdout. Picking
# the top-K features from it biased WHICH hypotheses Foundry tries, even after
# the evaluation window was locked to pre-oos. 1B now takes the ranked base
# feature names as data (1D ranks them on the pre-oos panel it already holds),
# keeping 1B a pure descriptor layer with no IO and no compute.

def test_derivation_is_pure_and_takes_base_features():
    from research.hermes.hypothesis_queue import hypotheses_from_derivation
    out = hypotheses_from_derivation(["funding_z"])
    assert {h.id for h in out} == {"der_funding_z_zscore", "der_funding_z_rank"}
    assert all(h.source == "derived" for h in out)
    assert hypotheses_from_derivation([]) == []


def test_build_queue_never_reads_evidence_json(tmp_path, monkeypatch):
    from research.hermes import hypothesis_queue as hq
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO
    monkeypatch.setattr(hq, "hypotheses_from_zoo", lambda d: [Hypothesis("zoo_a", "a", SOURCE_ZOO)])
    monkeypatch.setattr(hq, "hypotheses_from_academic", lambda: [])
    monkeypatch.setattr(hq, "_graveyard_fingerprints", lambda s, m: set())
    # evidence must never be consulted — blow up if anything tries
    def boom(*a, **k):
        raise AssertionError("build_queue must not read evidence_<sym>.json (OOS-contaminated IC)")
    monkeypatch.setattr(hq, "load_evidence", boom, raising=False)

    q = hq.build_queue(symbol="eth", manifests_dir=tmp_path, zoo_dir=tmp_path,
                       llm_raw=[], derived_bases=["funding_z"])
    ids = {h.id for h in q}
    assert "zoo_a" in ids
    assert "der_funding_z_zscore" in ids     # derived came from the caller, not the manifest
