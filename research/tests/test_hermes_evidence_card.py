import json
import math
import pytest
from research.hermes.evidence_card import EvidenceCard, CardValidationError, VERDICT_CANDIDATE, VERDICT_GRAVEYARD


def _candidate_kwargs(**over):
    base = dict(
        factor_id="eth_mom5_ll01", symbol="eth", source="llm",
        code_sha256="a" * 64, generated_at="2026-07-08T00:00:00+00:00", trial_step=1,
        interval="1H", formula="close.pct_change(5)", rationale="5-bar momentum",
        net_ic=0.031, ic_nonoverlap=0.028, ir=0.42, dsr=0.11, pbo=0.34,
        turnover=0.12, n_samples=8760,
        regime_ic={"bull": 0.05, "bear": 0.01, "chop": 0.02},
        yearly_ic={"2022": 0.04, "2023": 0.02},
        nearest_factor="funding_z", nearest_abs_spearman=0.41,
        verdict=VERDICT_CANDIDATE, death_reason=None,
    )
    base.update(over)
    return base


def test_candidate_card_round_trips_through_json():
    card = EvidenceCard(**_candidate_kwargs())
    blob = json.dumps(card.to_dict())          # must not raise (nan-safe)
    back = EvidenceCard.from_dict(json.loads(blob))
    assert back == card


def test_nan_metric_serialises_as_null_not_invalid_json():
    card = EvidenceCard(**_candidate_kwargs(dsr=float("nan")))
    blob = json.dumps(card.to_dict(), allow_nan=False)   # strict: bare NaN would raise
    assert '"dsr": null' in blob
    assert EvidenceCard.from_dict(json.loads(blob)).dsr is None


def test_nested_nan_in_regime_ic_is_sanitised():
    # agy 二審: top-level-only sanitising left nested NaN -> allow_nan=False crashed
    card = EvidenceCard(**_candidate_kwargs(regime_ic={"bull": float("nan"), "bear": 0.01}))
    blob = json.dumps(card.to_dict(), allow_nan=False)   # must NOT raise
    assert EvidenceCard.from_dict(json.loads(blob)).regime_ic == {"bull": None, "bear": 0.01}


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_inf_metric_serialises_as_null(bad):
    card = EvidenceCard(**_candidate_kwargs(ir=bad))
    blob = json.dumps(card.to_dict(), allow_nan=False)   # must NOT raise
    assert EvidenceCard.from_dict(json.loads(blob)).ir is None


def test_from_dict_tolerates_missing_new_field():
    # forward-compat: an old card lacking a later-added field must still load
    d = _candidate_kwargs()
    del d["turnover"]                                     # simulate pre-turnover card
    card = EvidenceCard.from_dict(d)
    assert card.turnover is None


def test_graveyard_card_requires_death_reason():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(verdict=VERDICT_GRAVEYARD, death_reason=None))


def test_candidate_card_requires_core_metrics():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(net_ic=None))


def test_candidate_card_requires_ic_nonoverlap():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(ic_nonoverlap=None))


def test_candidate_card_requires_pbo():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(pbo=None))


def test_candidate_card_rejects_non_finite_core_metric_at_construction():
    # net_ic is a quality-gate metric: nan must be rejected immediately, not
    # silently accepted then blow up later on from_dict() reload.
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(net_ic=float("nan")))


def test_unknown_verdict_rejected():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(verdict="maybe"))
