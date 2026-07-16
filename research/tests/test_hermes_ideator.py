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


from research.hermes.ideator import IdeationParseError, parse_ideas


def test_parse_ideas_reads_a_fenced_json_block():
    resp = '''好的，以下是我的想法：
```json
[{"id": "funding_vol", "description": "funding volatility", "fields": ["funding_rate_raw"]}]
```
希望有幫助。'''
    assert parse_ideas(resp) == [
        {"id": "funding_vol", "description": "funding volatility",
         "fields": ["funding_rate_raw"]}]


def test_parse_ideas_falls_back_to_bare_json_without_a_fence():
    """已知高頻故障：LLM 常常不吐 fence（stage0/2 swarm 的老問題）。"""
    resp = '[{"id": "a", "description": "d", "fields": ["funding_z", "oi_z"]}]'
    assert parse_ideas(resp)[0]["id"] == "a"


def test_parse_ideas_handles_a_plain_python_fence():
    resp = '```\n[{"id": "a", "description": "d", "fields": ["funding_z", "oi_z"]}]\n```'
    assert parse_ideas(resp)[0]["id"] == "a"


def test_parse_ideas_survives_control_chars_in_long_chinese_json():
    """既有教訓：LLM 吐長中文 JSON 必須 json.loads(strict=False)。"""
    resp = '[{"id": "a", "description": "資金費率\tz 分數與大戶部位背離", "fields": ["funding_z", "toptrader_ls_z"]}]'
    assert "資金費率" in parse_ideas(resp)[0]["description"]


def test_parse_ideas_accepts_an_object_wrapping_the_list():
    resp = '{"ideas": [{"id": "a", "description": "d", "fields": ["funding_z", "oi_z"]}]}'
    assert parse_ideas(resp)[0]["id"] == "a"


def test_parse_ideas_raises_on_truncated_json():
    """max_tokens 截斷是真實風險（見 plan Global Constraints）。"""
    with pytest.raises(IdeationParseError):
        parse_ideas('[{"id": "a", "description": "d", "fields": ["fund')


def test_parse_ideas_raises_when_there_is_no_json_at_all():
    with pytest.raises(IdeationParseError):
        parse_ideas("抱歉，我無法完成這個請求。")


def test_parse_ideas_raises_when_entries_are_not_objects():
    with pytest.raises(IdeationParseError):
        parse_ideas('["just a string"]')


def test_parse_ideas_skips_a_markdown_checklists_false_empty_array():
    """回歸測試：`- [ ] ...` 待辦清單裡的 `[ ]` 是合法的空 JSON array，早期版本
    一掃到就當作解析成功回傳 []，導致後面真正的想法陣列從沒被讀到，
    IdeationParseError 也從沒觸發（正是本模組要防的 llm_raw=[] 老 bug）。"""
    resp = '''我的想法如下：
- [ ] funding volatility idea
- [ ] positioning divergence idea

[{"id": "funding_vol", "description": "funding volatility", "fields": ["funding_rate_raw"]}]'''
    ideas = parse_ideas(resp)
    assert ideas == [
        {"id": "funding_vol", "description": "funding volatility",
         "fields": ["funding_rate_raw"]}]


from research.hermes.ideator import validate_ideas

_PANEL = ["funding_z", "oi_z", "toptrader_ls_z", "close", "volume"]


def test_validate_accepts_a_well_formed_idea():
    ideas = [{"id": "a", "description": "d", "fields": ["funding_z", "oi_z"]}]
    accepted, rejected = validate_ideas(ideas, _PANEL)
    assert accepted == ideas
    assert rejected == []


def test_validate_accepts_a_single_field_idea():
    """spec §3.2：非單調的單欄時序變換（如 rolling_std(funding,168)）實測
    max|spearman| 只有 0.617，過得了 0.7 閘。不可封殺。"""
    ideas = [{"id": "fvol", "description": "rolling volatility of funding",
              "fields": ["funding_z"]}]
    accepted, rejected = validate_ideas(ideas, _PANEL)
    assert len(accepted) == 1
    assert rejected == []


def test_validate_rejects_a_hallucinated_column():
    """在花 3 次 forge call 撞 KeyError 之前就死。"""
    ideas = [{"id": "bad", "description": "d", "fields": ["liquidation_z", "funding_z"]}]
    accepted, rejected = validate_ideas(ideas, _PANEL)
    assert accepted == []
    assert rejected[0]["id"] == "bad"
    assert "liquidation_z" in rejected[0]["reason"]


def test_validate_rejects_missing_or_empty_fields():
    accepted, rejected = validate_ideas(
        [{"id": "nofields", "description": "d"},
         {"id": "empty", "description": "d", "fields": []}], _PANEL)
    assert accepted == []
    assert {r["id"] for r in rejected} == {"nofields", "empty"}


def test_validate_rejects_missing_id_or_description():
    accepted, rejected = validate_ideas(
        [{"description": "d", "fields": ["funding_z"]},
         {"id": "nodesc", "fields": ["funding_z"]}], _PANEL)
    assert accepted == []
    assert len(rejected) == 2


def test_validate_rejects_duplicate_ids():
    ideas = [{"id": "dup", "description": "one", "fields": ["funding_z"]},
             {"id": "dup", "description": "two", "fields": ["oi_z"]}]
    accepted, rejected = validate_ideas(ideas, _PANEL)
    assert len(accepted) == 1 and accepted[0]["description"] == "one"
    assert rejected[0]["id"] == "dup"


def test_validate_rejects_non_list_fields():
    accepted, rejected = validate_ideas(
        [{"id": "a", "description": "d", "fields": "funding_z"}], _PANEL)
    assert accepted == []
    assert "list" in rejected[0]["reason"]
