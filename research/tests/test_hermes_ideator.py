"""ideator：死因分類、JSON 解析、確定性驗證、prompt 組裝、generate_ideas。
CI 一律用 fake LLM —— 絕不呼叫付費 API。"""
import pytest

from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.ideator import death_category, summarize_deaths


def _card(factor_id, verdict, death_reason=None, formula="x", **kw):
    return EvidenceCard(
        factor_id=factor_id, symbol="eth", source="llm", code_sha256="s",
        generated_at="2026-07-16T00:00:00+00:00", trial_step=1, interval="1H",
        formula=formula, rationale="r", verdict=verdict, death_reason=death_reason, **kw)


@pytest.mark.parametrize("reason,expected", [
    # 這四條字串來自 gatekeeper.evaluate 的真實 reject 分支
    ("redundant: abs_spearman 0.98 vs funding_z", "redundant"),
    ("turnover 0.83 > 0.5", "turnover"),
    ("weak gross_ic -0.0279 < 0.03", "weak_ic"),
    ("DSR 0.12 < 0.5", "dsr"),
    # forge 側
    ("SandboxRunFailed: sandbox run failed (exit 1)", "forge_failed"),
    ("forge failed", "forge_failed"),
    ("LLM repeated identical code after: ValueError: boom", "forge_failed"),
    ("LookaheadError: factor peeks into the future", "forge_failed"),
    (None, "other"),
    ("something nobody predicted", "other"),
])
def test_death_category_maps_real_gatekeeper_strings(reason, expected):
    assert death_category(reason) == expected


def test_summarize_deaths_never_leaks_numbers():
    """spec §4.1：餵給 LLM 的死因絕不含 IC/績效數值，否則 LLM 會逼門檻。"""
    cards = [_card("f1", VERDICT_GRAVEYARD, "weak gross_ic -0.0279 < 0.03",
                   formula="funding_z * oi_z", gross_ic=-0.0279)]
    out = summarize_deaths(cards)
    assert out == [{"formula": "funding_z * oi_z", "category": "weak_ic"}]
    blob = repr(out)
    for leak in ("0.0279", "-0.0279", "0.03"):
        assert leak not in blob


def test_summarize_deaths_skips_candidates():
    cards = [_card("dead", VERDICT_GRAVEYARD, "turnover 0.9 > 0.5", formula="a*b"),
             _card("alive", VERDICT_CANDIDATE, formula="c*d",
                   gross_ic=0.05, ic_nonoverlap=0.04)]
    assert [c["formula"] for c in summarize_deaths(cards)] == ["a*b"]


def test_summarize_deaths_respects_limit_and_is_deterministic():
    cards = [_card(f"f{i}", VERDICT_GRAVEYARD, "weak gross_ic 0.0 < 0.03",
                   formula=f"col{i:02d}") for i in range(10)]
    out = summarize_deaths(cards, limit=3)
    assert len(out) == 3
    assert out == summarize_deaths(cards, limit=3)
