# Foundry LLM crypto 原生假設生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補上 Foundry `hypotheses_from_llm` 缺失的上游 —— 一個讀 crypto 欄位經濟定義、產出跨欄/時序經濟假說的 ideator，並把 zoo/derived/academic 三條死來源預設關閉。

**Architecture:** 新增 `field_schema.yaml`（31 欄經濟定義，人審）+ `field_schema.py`（載入/對帳）+ `ideator.py`（prompt/parse/validate/generate）。`build_queue` 加 `sources` 開關，`run_foundry` 呼叫 ideator 並傳真 `llm_raw`。架構本來就是兩段式（`Hypothesis.description` → `forge._PROMPT` 翻成 code），ideator 吐白話假說即自動接上第二段。

**Tech Stack:** Python 3.11、pandas、pyyaml、pytest。LLM 走既有 `research/hermes/llm_client.py` 的 `LLMCoder` protocol（`complete(prompt) -> str`）。

Spec：[docs/superpowers/specs/2026-07-16-foundry-llm-ideation-design.md](../specs/2026-07-16-foundry-llm-ideation-design.md)

## Global Constraints

- **測試位置**：`research/tests/`，從 **repo root** 執行 `python -m pytest research/tests/ -q`。**絕不**跟 `agent/tests/` 或 `dashboard/server` 的 pytest 混在同一次呼叫（三個獨立 pytest scope）。
- **CI 絕不呼叫付費 LLM。** 所有測試用 fake LLM（一個有 `.complete(prompt) -> str` 的物件）。付費 + 非決定性會毀 CI。
- **`json.loads(strict=False)`** —— LLM 吐長中文 JSON 必用（既有教訓）。
- **不動 gate 門檻**（`GateConfig` 的 `gross_ic_min` / `redundant_abs_spearman` / `max_turnover` / `dsr_min` 一律不改）。
- **不刪 zoo 程式碼** —— 只在 `sources` 預設值裡不啟用。
- **餵給 LLM 的脈絡絕不含 IC / 績效數值** —— 只有死因分類。這是防自動化 p-hacking 的核心約束。
- 每個 task 結束都 commit。commit message 用英文，結尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 分支：`quant-trading-dashboard`。**不 push**，除非使用者明講。

## File Structure

| 檔案 | 建/改 | 責任 |
|---|---|---|
| `research/hermes/field_schema.yaml` | 建 | 31 欄經濟定義（純資料，人可讀可改） |
| `research/hermes/field_schema.py` | 建 | 載入 YAML + 與 panel 欄位對帳 |
| `research/hermes/ideator.py` | 建 | 死因分類、prompt 組裝、JSON 解析、確定性驗證、`generate_ideas` |
| `research/hermes/hypothesis_queue.py` | 改 | `build_queue(..., sources=...)` |
| `research/hermes/orchestrator.py` | 改 | 呼叫 ideator、傳真 `llm_raw`、`Budget` 預設、summary 欄位、`run_foundry_job` 傳 `sources` |
| `research/tests/test_hermes_field_schema.py` | 建 | Task 1 |
| `research/tests/test_hermes_ideator.py` | 建 | Task 2–5 |
| `research/tests/test_hermes_hypothesis_queue.py` | 改 | Task 6 |
| `research/tests/test_hermes_orchestrator.py` | 改 | Task 7–8 |

---

## Task 1: 欄位經濟定義表 + 對帳

**Files:**
- Create: `research/hermes/field_schema.yaml`
- Create: `research/hermes/field_schema.py`
- Test: `research/tests/test_hermes_field_schema.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `load_field_schema(path: str | Path | None = None) -> dict[str, dict]`
  - `reconcile_schema(schema: dict, panel_columns) -> tuple[dict, list[str], list[str]]`
    回傳 `(usable, undocumented, stale)`。`usable` = 同時在 schema 和 panel 的欄位（給 ideator 用）；`undocumented` = 在 panel 但沒定義；`stale` = 有定義但 panel 沒有。
  - `SCHEMA_PATH: Path` 模組常數

> **⚠️ 人工檢查點（Step 6）：** 這張表是 Claude 從程式碼推出來的，**不是市場經驗**。使用者必須逐欄審過才能進下一個 task。見 spec §8 限制 2。

- [ ] **Step 1: 寫 `field_schema.yaml`**

定義全部從真碼推導（`research/lib/derived_factors.py`、`research/pipeline/stage0a_features.py`），非猜測。`_rolling_z(s, 720)` = 30 天滾動 z（1H 下 720 bar，`min_periods=360`）。

```yaml
# research/hermes/field_schema.yaml
#
# Foundry ideator 的燃料：panel 每一欄的經濟含意。
# 來源：research/lib/derived_factors.py + research/pipeline/stage0a_features.py
# 由 test_hermes_field_schema.py 對帳 features_<sym>.parquet 的實際欄位。
#
# 每欄欄位：
#   what     — 這是什麼（含計算式）
#   positive — 正值代表什麼
#   notes    — 已知陷阱／性質（LLM 必讀）
#
# ── crypto 原生：資金費 ────────────────────────────────────────────────
funding_rate_raw:
  what: "永續合約資金費率，8h 結算值 forward-fill 到 1H 網格"
  positive: "多方付錢給空方 → 多方擁擠"
  notes: >-
    階梯函數：同一個值會連續重複 8 根 bar。在這上面做 1H 差分，
    大多數 bar 得到 0，只有結算點跳動 —— 頻率錯配陷阱。
    本 repo 實證為 contrarian 訊號（負 IC）。非平穩。
funding_z:
  what: "funding_rate_raw 的 30 天滾動 z 分數 (window=720h, min_periods=360)"
  positive: "資金費率相對過去 30 天異常高"
  notes: "現役 alpha。z 分數這個變換已被佔用，別重做。"
funding_mom:
  what: "funding_rate_raw - funding_rate_raw.shift(24)，24h 動能"
  positive: "資金費率在過去 24h 上升"
  notes: "動能／差分這個變換已被佔用，別重做。"

# ── crypto 原生：基差 ──────────────────────────────────────────────────
basis_rel:
  what: "(永續價 - 現貨價) / 現貨價"
  positive: "期貨溢價 —— 多方願付溢價持有槓桿多單"
  notes: "現役 alpha。套利力量會把它壓回 0 附近，量級是 bps 級。"
basis_z:
  what: "basis_rel 的 30 天滾動 z 分數 (window=720h)"
  positive: "基差相對過去 30 天異常高"
  notes: "z 分數變換已被佔用。"
basis_mom:
  what: "basis_rel - basis_rel.shift(24)，24h 動能"
  positive: "基差在過去 24h 擴大"
  notes: "動能變換已被佔用。"

# ── crypto 原生：未平倉合約 (OI) ───────────────────────────────────────
oi_change_24h:
  what: "未平倉合約量的 24h 變化率"
  positive: "倉位在累積"
  notes: "資料源是 Binance daily metrics archive（5 分鐘快照聚合到 1H）。"
oi_z:
  what: "OI 的 30 天滾動 z 分數 (window=720h)"
  positive: "OI 相對過去 30 天異常高"
  notes: "z 分數變換已被佔用。"
oi_mom:
  what: "OI 的 72h 變化率 oi.pct_change(72)"
  positive: "OI 在過去 72h 增加"
  notes: "動能變換已被佔用（72h 版）。"
oi_price_divergence:
  what: "oi.pct_change(24) * close.pct_change(24) —— 兩個變化率的『乘積』"
  positive: "OI 與價格『同向』變動（同漲或同跌）"
  notes: >-
    名字騙人：它是同向共振的乘積，不是背離。負值才是背離
    （OI 增但價跌 / OI 減但價漲）。

# ── crypto 原生：多空持倉比 ────────────────────────────────────────────
global_ls_acct_z:
  what: "Binance 全市場多空『帳戶數』比的 30 天滾動 z (window=720h)"
  positive: "散戶帳戶偏多的程度相對過去 30 天異常高"
  notes: "散戶情緒代理（按帳戶數，一人一票）。缺口小時保持 NaN，未 ffill。"
toptrader_ls_z:
  what: "Binance 大戶多空『持倉加權』比的 30 天滾動 z (window=720h)"
  positive: "大戶部位偏多的程度相對過去 30 天異常高"
  notes: >-
    聰明錢代理（按持倉量加權，較反映 PnL）。刻意不用帳戶數版本。
    缺口小時保持 NaN，未 ffill。
ls_divergence:
  what: "global_ls_acct_z - toptrader_ls_z（散戶 z 減大戶 z）"
  positive: "散戶比大戶更偏多 —— 典型的反向訊號設定"
  notes: >-
    ⚠️ 與 global_ls_acct_z / toptrader_ls_z 三者線性相依（rank-2）。
    絕對不要把三個一起放進同一個因子 —— 那是線性重複。

# ── crypto 原生：穩定幣／跨場溢價 ──────────────────────────────────────
stablecoin_supply_z:
  what: "穩定幣總供給量的滾動 z 分數"
  positive: "場外資金（穩定幣）正在流入"
  notes: >-
    來自『日頻』序列 ffill 到 1H：24h 階梯函數，一天內 24 根 bar 同值。
    1H 差分只在日界跳動 —— 頻率錯配陷阱。BTC 上 IC +0.104。
depeg:
  what: "USDT/USD 匯率 - 1（純 USDT 脫鉤，已與法幣溢價解耦）"
  positive: "USDT 相對 USD 溢價"
  notes: "已 wire 但薄。長時間近乎常數（USDT 本來就該貼著 1）。"
depeg_z:
  what: "depeg 的 30 天滾動 z 分數"
  positive: "USDT 脫鉤程度相對過去 30 天異常高"
  notes: "z 分數變換已被佔用。"
fiat_prem:
  what: "(OKX USDT 現貨價 × USDT/USD 匯率) / Massive USD 現貨價 - 1，離岸對美國法幣溢價（depeg 已扣除）"
  positive: "離岸（OKX）比美國場內貴"
  notes: >-
    已被 REJECT 過（研究結論）。有資料品質護欄：|溢價|>5% 直接丟棄、
    覆蓋率<50% 整欄拒為 NaN。可能整欄是 NaN。
fiat_prem_z:
  what: "fiat_prem 的 30 天滾動 z 分數"
  positive: "法幣溢價相對過去 30 天異常高"
  notes: "母體已被 REJECT。可能整欄是 NaN。"

# ── OHLCV 技術指標（股票血統，crypto perp 上多半死）────────────────────
rsi_14:
  what: "14 期 RSI 相對強弱指標"
  positive: "近期漲幅相對跌幅大（超買區）"
  notes: "股票血統技術指標。crypto perp 上 IC 普遍 <0.03。"
macd_diff:
  what: "MACD 柱狀圖（DIF - DEA）"
  positive: "短期動能強過長期"
  notes: "股票血統。"
roc_10:
  what: "10 期價格變化率"
  positive: "價格在過去 10 根 bar 上漲"
  notes: "股票血統。價格動能這條路已被佔用。"
stoch_k:
  what: "隨機指標 %K"
  positive: "收盤價接近近期區間高點"
  notes: "股票血統。"
ema_cross_9_21:
  what: "EMA9 與 EMA21 的交叉訊號"
  positive: "短期均線在長期均線之上（多頭排列）"
  notes: "股票血統趨勢類。"
sma_cross_10_30:
  what: "SMA10 與 SMA30 的交叉訊號"
  positive: "短期均線在長期均線之上"
  notes: "股票血統趨勢類。與 ema_cross_9_21 高度相關。"
adx_14:
  what: "14 期 ADX 趨勢強度"
  positive: "趨勢強（不分方向）"
  notes: "無方向性 —— 適合當條件式因子的『閘』而非方向訊號。"
atr_14:
  what: "14 期真實波幅均值"
  positive: "波動大"
  notes: "無方向性。價格尺度相依（非平穩）。適合當閘。"
bb_width_20:
  what: "20 期布林通道寬度"
  positive: "波動擴張"
  notes: "無方向性。適合當閘。"
rolling_std_20:
  what: "20 期報酬滾動標準差"
  positive: "波動大"
  notes: "無方向性。價格波動度這條路已被佔用。"
obv:
  what: "能量潮（On-Balance Volume），依漲跌方向累加成交量"
  positive: "買盤累積"
  notes: >-
    ⚠️ 累積和 —— 非平穩、會無限漂移。直接對它算 Spearman IC 大部分
    只是在量測時間趨勢。IC 評估時被強制套 zscore_720h 變換。
mfi_14:
  what: "14 期資金流量指標（成交量加權的 RSI）"
  positive: "資金流入（超買區）"
  notes: "股票血統。"
volume_zscore_20:
  what: "成交量的 20 期 z 分數"
  positive: "成交量相對近期異常大"
  notes: "無方向性。適合當閘。"
```

- [ ] **Step 2: 寫失敗測試**

```python
# research/tests/test_hermes_field_schema.py
"""field_schema 的載入 + 對帳。純資料，不碰 LLM。"""
from pathlib import Path

import pandas as pd
import pytest

from research.hermes.field_schema import SCHEMA_PATH, load_field_schema, reconcile_schema

_REQUIRED_KEYS = {"what", "positive", "notes"}


def test_schema_loads_and_every_entry_has_required_keys():
    schema = load_field_schema()
    assert schema, "field_schema.yaml is empty"
    for name, entry in schema.items():
        missing = _REQUIRED_KEYS - set(entry)
        assert not missing, f"{name} missing keys {missing}"
        for k in _REQUIRED_KEYS:
            assert str(entry[k]).strip(), f"{name}.{k} is blank"


def test_reconcile_splits_usable_undocumented_and_stale():
    schema = {"a": {"what": "A", "positive": "p", "notes": "n"},
              "gone": {"what": "G", "positive": "p", "notes": "n"}}
    usable, undocumented, stale = reconcile_schema(schema, ["a", "newcol"])
    assert set(usable) == {"a"}
    assert undocumented == ["newcol"]
    assert stale == ["gone"]


def test_reconcile_is_deterministic_and_sorted():
    schema = {"b": {"what": "B", "positive": "p", "notes": "n"},
              "a": {"what": "A", "positive": "p", "notes": "n"}}
    usable, undocumented, stale = reconcile_schema(schema, ["z", "a", "b", "y"])
    assert list(usable) == ["a", "b"]
    assert undocumented == ["y", "z"]
    assert stale == []


@pytest.mark.parametrize("symbol", ["eth"])
def test_schema_covers_the_real_feature_panel(symbol):
    """schema 必須跟 features_<sym>.parquet 的真實欄位完全對上。
    這條擋的是 spec §8 限制 3：stage0a 新增欄位時 schema 會腐爛。"""
    panel_path = Path("research/manifests") / f"features_{symbol}.parquet"
    if not panel_path.exists():
        pytest.skip(f"{panel_path} not present in this checkout")
    cols = list(pd.read_parquet(panel_path).columns)
    _usable, undocumented, stale = reconcile_schema(load_field_schema(), cols)
    assert not undocumented, (
        f"panel has undocumented columns {undocumented}; "
        f"add them to {SCHEMA_PATH.name} so the ideator can use them")
    assert not stale, (
        f"{SCHEMA_PATH.name} documents columns the panel no longer has: {stale}")
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_field_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.hermes.field_schema'`

- [ ] **Step 4: 寫最小實作**

```python
# research/hermes/field_schema.py
"""Panel 欄位的經濟定義表：Foundry ideator 的燃料。

zoo 那 456 個股票因子只碰 close/volume，從沒碰過 panel 的 crypto 原生欄位。
ideator 要出 crypto 原生假設，就必須知道 funding_z / basis_rel / oi_z 這些欄位
的『經濟含意』—— 光給欄名，LLM 只能瞎猜命名玩排列組合，那就退化回無腦 data
mining。

自動從 docstring 抽取這條路已驗證不可行：derived_factors.py 的
basis_factors / funding_factors / oi_factors（產出 crypto 核心欄的三個函式）
docstring 全是 None。所以這張表是手寫的資料檔。
"""
from __future__ import annotations

from pathlib import Path

import yaml

SCHEMA_PATH = Path(__file__).with_name("field_schema.yaml")


def load_field_schema(path: "str | Path | None" = None) -> dict:
    """Load the hand-written field schema. Returns {column: {what, positive, notes}}."""
    p = Path(path) if path else SCHEMA_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p} must contain a mapping of column -> definition")
    return data


def reconcile_schema(schema: dict, panel_columns) -> tuple:
    """Split the schema against the panel's ACTUAL columns.

    Returns (usable, undocumented, stale):
      usable       — {column: definition} for columns present in BOTH. Only these
                     are ever offered to the ideator.
      undocumented — panel columns with no definition (sorted).
      stale        — documented columns the panel no longer has (sorted).

    Callers differ deliberately (spec §6): the TEST asserts undocumented/stale are
    both empty (a rotted schema is a repo-consistency bug), but the RUNTIME only
    intersects and warns — stage0a adding a column must not crash that night's
    cron, it just means the column isn't offered to the LLM until someone
    documents it.
    """
    cols = set(panel_columns)
    usable = {k: schema[k] for k in sorted(cols & set(schema))}
    undocumented = sorted(cols - set(schema))
    stale = sorted(set(schema) - cols)
    return usable, undocumented, stale
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest research/tests/test_hermes_field_schema.py -q`
Expected: PASS（4 passed）

若 `test_schema_covers_the_real_feature_panel` 失敗，**照實際 panel 欄位修 YAML**，不要改測試。

- [ ] **Step 6: ⚠️ 停下來給使用者逐欄審 YAML**

**這是硬檢查點，不可跳過。** 向使用者說明：

> `field_schema.yaml` 31 欄定義寫好了。這張表是我從 `derived_factors.py` / `stage0a_features.py` 的程式碼推出來的，不是市場經驗 —— 我對 `depeg_z`、`fiat_prem_z` 這類欄位的理解可能有偏差。
> 這張表是整個 ideator 的燃料，寫錯 = LLM 拿錯誤的經濟直覺出點子。**請逐欄審。**
> 特別想請你確認：(a) `oi_price_divergence` 我標成「乘積＝同向共振，不是背離」對嗎？(b) `funding_rate_raw` 是 contrarian 訊號這點對嗎？(c) `ls_divergence` 三者線性相依的警告夠不夠清楚？

等使用者回覆後才進 Step 7。

- [ ] **Step 7: Commit**

```bash
git add research/hermes/field_schema.yaml research/hermes/field_schema.py research/tests/test_hermes_field_schema.py
git commit -m "$(cat <<'EOF'
feat(hermes): add panel field schema for Foundry ideation

The ideator cannot propose crypto-native hypotheses from column names
alone -- that degrades to blind data mining. This is the economic
definition of every panel column: what it is, what a positive value
means, and the known hazards.

Definitions are derived from derived_factors.py and stage0a_features.py,
not guessed. Auto-extraction was ruled out: basis_factors,
funding_factors and oi_factors all have docstring None.

Notes capture the traps that matter for factor design: funding_rate_raw
is an 8h settlement ffilled to 1H (a step function, so 1H differencing is
mostly zeros), stablecoin_supply_z is a daily series ffilled to 1H, obv
is a non-stationary cumulative sum, and ls_divergence is linearly
dependent on the two L/S z-scores it is built from.

oi_price_divergence is documented against its name: it is
oi.pct_change(24) * close.pct_change(24), a co-movement product, so a
positive value means OI and price move together, not that they diverge.

reconcile_schema deliberately serves two callers: the test asserts exact
coverage (a rotted schema is a repo bug) while the runtime intersects and
warns, so stage0a adding a column never crashes that night's cron.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 死因分類（不洩漏 IC）

**Files:**
- Create: `research/hermes/ideator.py`
- Test: `research/tests/test_hermes_ideator.py`

**Interfaces:**
- Consumes: `research.hermes.evidence_card.EvidenceCard`
- Produces:
  - `death_category(death_reason: str | None) -> str` — 回傳 `"weak_ic"` / `"redundant"` / `"turnover"` / `"dsr"` / `"forge_failed"` / `"other"`
  - `summarize_deaths(cards, limit: int = 30) -> list[dict]` — 回傳 `[{"formula": str, "category": str}, ...]`，**絕不含任何數值**

> **為什麼不含數值（spec §4.1）：** agy 二審指出，把 `IC=0.029 被刷掉` 餵給 LLM，它最省力的路不是找新金融邏輯，而是套個 `log()`/`ewma` 把 IC 硬逼過 0.03 —— 自動化 p-hacking。只給分類，LLM 知道路死了但不知道差多少，就沒得逼門檻。

- [ ] **Step 1: 寫失敗測試**

```python
# research/tests/test_hermes_ideator.py
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_ideator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.hermes.ideator'`

- [ ] **Step 3: 寫最小實作**

```python
# research/hermes/ideator.py
"""Foundry LLM crypto-native hypothesis ideation.

The missing upstream of hypothesis_queue.hypotheses_from_llm(): that adapter has
always existed, but run_foundry passed llm_raw=[], so LLM ideation was never
wired. Meanwhile the queue's first 50 slots were 100% zoo -- 456 equity/A-share
technical alphas touching only close/volume -- while the panel's crypto-native
columns (funding/basis/OI/long-short) were never touched by any hypothesis.

The architecture is already two-stage: Hypothesis.description (a plain-language
economic hypothesis) -> forge._PROMPT turns it into compute(df) code. So this
module only produces descriptions; codegen stays forge's job.
"""
from __future__ import annotations

from research.hermes.evidence_card import VERDICT_GRAVEYARD

# gatekeeper.evaluate's four reject branches, matched on their literal prefixes
# (gatekeeper.py:243-250). Order matters only for readability -- the prefixes are
# mutually exclusive.
_GATE_PREFIXES = (
    ("redundant:", "redundant"),
    ("turnover ", "turnover"),
    ("weak gross_ic ", "weak_ic"),
    ("DSR ", "dsr"),
)
# forge-side deaths: the code never ran clean, so there is no verdict about the
# IDEA -- only about the code. Told apart from gate deaths so the ideator can see
# "your idea was never actually tested" vs "your idea was tested and lost".
_FORGE_MARKERS = ("SandboxRunFailed", "forge failed", "LLM repeated identical code",
                  "LookaheadError", "UnsafeCodeError", "Contract violation")


def death_category(death_reason: "str | None") -> str:
    """Bucket a death_reason into a category. NEVER returns the numbers."""
    if not death_reason:
        return "other"
    for prefix, category in _GATE_PREFIXES:
        if death_reason.startswith(prefix):
            return category
    if any(m in death_reason for m in _FORGE_MARKERS):
        return "forge_failed"
    return "other"


def summarize_deaths(cards, limit: int = 30) -> list:
    """Buried factors as {formula, category} -- descriptions and buckets ONLY.

    Deliberately drops every metric (spec §4.1): feeding "IC 0.029 rejected" to
    the LLM invites it to bolt on a log()/ewma to shove IC past 0.03 rather than
    find new economics. That is automated p-hacking. A category tells the LLM the
    road is dead without telling it by how much.
    """
    out = []
    for c in cards:
        if c.verdict != VERDICT_GRAVEYARD:
            continue
        out.append({"formula": c.formula, "category": death_category(c.death_reason)})
        if len(out) >= limit:
            break
    return out
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/test_hermes_ideator.py -q`
Expected: PASS（13 passed）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/ideator.py research/tests/test_hermes_ideator.py
git commit -m "$(cat <<'EOF'
feat(hermes): bucket factor death reasons without leaking metrics

summarize_deaths hands the ideator what died and why it died, as a
category -- never the number behind it.

agy's review named the failure mode: given "IC 0.029, rejected below
0.03", an LLM's cheapest move is not new economics, it is bolting a
log() or an ewma onto the same formula until IC clears the bar. That is
automated p-hacking, and it would sail through the gate and die in
production. A category says the road is dead without saying by how much,
so there is no bar to chase.

death_category matches gatekeeper.evaluate's four literal reject
prefixes and separates them from forge-side failures, where the code
never ran clean and the idea itself was never actually tested.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: JSON 解析（含 fence / 中文 / 截斷）

**Files:**
- Modify: `research/hermes/ideator.py`
- Test: `research/tests/test_hermes_ideator.py`

**Interfaces:**
- Consumes: Task 2 的 `ideator` 模組
- Produces: `parse_ideas(response: str) -> list[dict]`，壞掉時 raise `IdeationParseError`
- Produces: `class IdeationParseError(HermesGuardError, ValueError)`

> **為什麼要 1 次重試（spec §6）：** LLM 不吐 fence 是本 repo 的**已知高頻故障**（stage0/2 swarm 常這樣）。`json.loads(strict=False)` 也是既有教訓 —— LLM 吐長中文 JSON 時，控制字元會讓 `strict=True` 直接爆掉。

- [ ] **Step 1: 寫失敗測試**（append 到 `test_hermes_ideator.py`）

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_ideator.py -k parse_ideas -q`
Expected: FAIL — `ImportError: cannot import name 'IdeationParseError'`

- [ ] **Step 3: 寫最小實作**（append 到 `ideator.py`）

```python
import json
import re

from research.hermes.errors import HermesGuardError

# mirrors forge._FENCE but also accepts ```json
_FENCE = re.compile(r"```(?:json|python|py)?\s*(.*?)```", re.DOTALL)


class IdeationParseError(HermesGuardError, ValueError):
    """The ideator's response was not usable JSON."""


def _first_json_value(text: str):
    """Find the first balanced [...] or {...} and json-decode it.

    raw_decode from the first bracket, rather than a regex: an idea's description
    can legitimately contain brackets, and a greedy/non-greedy regex mis-cuts on
    those. raw_decode stops exactly at the end of the first complete value, so
    trailing prose after the JSON is simply ignored.
    """
    for i, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            # strict=False: LLMs emit literal tabs/newlines inside long Chinese
            # strings, which strict JSON rejects outright (existing repo lesson).
            value, _end = json.JSONDecoder(strict=False).raw_decode(text, i)
            return value
        except json.JSONDecodeError:
            continue
    raise IdeationParseError(f"no decodable JSON value found in response: {text[:200]!r}")


def parse_ideas(response: str) -> list:
    """LLM response -> list of raw idea dicts. Raises IdeationParseError."""
    m = _FENCE.search(response)
    body = m.group(1) if m else response
    value = _first_json_value(body)
    if isinstance(value, dict):
        # tolerate {"ideas": [...]} — a very common LLM shape
        for key in ("ideas", "hypotheses", "factors"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            raise IdeationParseError(
                f"expected a JSON list of ideas, got an object with keys {sorted(value)}")
    if not isinstance(value, list):
        raise IdeationParseError(f"expected a JSON list of ideas, got {type(value).__name__}")
    if not all(isinstance(x, dict) for x in value):
        raise IdeationParseError("every idea must be a JSON object")
    return value
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/test_hermes_ideator.py -q`
Expected: PASS（21 passed）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/ideator.py research/tests/test_hermes_ideator.py
git commit -m "$(cat <<'EOF'
feat(hermes): parse ideator JSON tolerantly

Three failure modes here are known, not hypothetical.

The LLM often omits the code fence entirely -- the same problem that bit
stage0 and stage2's swarm -- so a missing fence falls back to scanning
the raw body. Long Chinese descriptions carry literal tabs and newlines
inside strings, which strict JSON rejects, so decoding runs with
strict=False. And max_tokens truncation cuts the array mid-string, which
must surface as IdeationParseError rather than a silent empty list, so
the caller's one retry can actually fire.

Uses raw_decode from the first bracket rather than a regex: a
description can legitimately contain brackets, and any regex mis-cuts on
those.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 確定性驗證（擋幻覺欄名）

**Files:**
- Modify: `research/hermes/ideator.py`
- Test: `research/tests/test_hermes_ideator.py`

**Interfaces:**
- Consumes: Task 3
- Produces: `validate_ideas(ideas: list[dict], panel_columns) -> tuple[list[dict], list[dict]]`
  回傳 `(accepted, rejected)`。`rejected` 每筆是 `{"id": str, "reason": str}`。

> **只有一條檢查：`fields ⊆ panel 欄位`。** spec §5.1 說明為什麼沒有第二、第三條：
> - `len(set(fields)) >= 2`（強制跨欄）**已移除** —— 那條的推導前提「單欄變換必被 0.7 dedup 斃」是**錯的**，只對單調變換成立。實測 `rolling_std(funding,168)` 只有 0.617，過得了閘。硬規則會封殺整類有效因子。
> - dead-class 由 fields 判**已移除** —— 對 LLM 來源空轉：sub-1H 微結構因子 LLM 根本算不出來（panel 就是 1H bar），order-flow 欄位不存在於 panel，`fields ⊆ panel` 已經擋掉。
>
> **已知限制（spec §8 限制 1）：** `fields` 是**宣告**，實際 code 是 forge 之後才寫的。LLM 可以宣告兩欄卻寫出只用一欄的 code —— 靜態檢查綁不住。這是**省錢的前置過濾**（幻覺欄名在花 3 次 forge call 之前就死），不是密不透風的保證。最後防線仍是 gate 的 dedup。

- [ ] **Step 1: 寫失敗測試**（append）

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_ideator.py -k validate -q`
Expected: FAIL — `ImportError: cannot import name 'validate_ideas'`

- [ ] **Step 3: 寫最小實作**（append 到 `ideator.py`）

```python
def validate_ideas(ideas: list, panel_columns) -> tuple:
    """Deterministic pre-filter. Returns (accepted, rejected[{id, reason}]).

    ONE substantive rule: every declared field must exist in the panel. A
    hallucinated column (the LLM inventing `liquidation_z`) would otherwise cost
    three forge calls before dying on a KeyError inside the sandbox. Killing it
    here is purely about not paying for a certain failure.

    There is deliberately NO "must span >= 2 columns" rule. That rule was
    proposed on the premise that single-column transforms are always killed by
    the 0.7 Spearman dedup gate -- which is only true for MONOTONIC transforms
    (rank/global-zscore give Spearman exactly 1.0). Measured on real eth pre-oos
    data, rolling_std(funding_rate_raw, 168) scores max |Spearman| 0.617 against
    all 31 panel columns and passes the gate. Banning single-column ideas would
    kill that whole class -- funding volatility is not in the panel and is
    economically meaningful -- for a reason that does not exist.

    Note this is an ADVISORY filter, not a guarantee: `fields` is what the LLM
    DECLARED, while the code is written later by forge. A declaration of two
    columns does not bind the code to use two. The real backstop is the gate.
    """
    cols = set(panel_columns)
    accepted, rejected, seen = [], [], set()
    for i, idea in enumerate(ideas):
        fid = idea.get("id")
        if not fid or not str(fid).strip():
            rejected.append({"id": f"<index {i}>", "reason": "missing 'id'"})
            continue
        fid = str(fid)
        if not str(idea.get("description", "")).strip():
            rejected.append({"id": fid, "reason": "missing 'description'"})
            continue
        if fid in seen:
            rejected.append({"id": fid, "reason": "duplicate id"})
            continue
        fields = idea.get("fields")
        if not isinstance(fields, list):
            rejected.append({"id": fid, "reason": "'fields' must be a list of panel columns"})
            continue
        if not fields:
            rejected.append({"id": fid, "reason": "'fields' is empty"})
            continue
        unknown = sorted({str(f) for f in fields} - cols)
        if unknown:
            rejected.append({"id": fid, "reason": f"unknown panel columns: {unknown}"})
            continue
        seen.add(fid)
        accepted.append(idea)
    return accepted, rejected
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/test_hermes_ideator.py -q`
Expected: PASS（28 passed）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/ideator.py research/tests/test_hermes_ideator.py
git commit -m "$(cat <<'EOF'
feat(hermes): pre-filter ideas against the real panel columns

One substantive rule: every declared field must exist in the panel. A
hallucinated column costs three forge calls before dying on a KeyError
inside the sandbox, so this is about not paying for a certain failure.

There is deliberately no "must span >= 2 columns" rule. It was proposed
on the premise that single-column transforms always die on the 0.7
Spearman dedup gate, which holds only for monotonic transforms -- rank
and global zscore give Spearman exactly 1.0. Measured against all 31
columns on real eth pre-oos data, rolling_std(funding_rate_raw, 168)
scores 0.617 and passes. Funding volatility is not in the panel and is
economically meaningful; banning that class for a reason that does not
survive measurement is not a trade worth making.

This filter is advisory, not binding: fields is what the LLM declared,
while the code is written later by forge. The gate remains the backstop.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: prompt 組裝 + `generate_ideas`

**Files:**
- Modify: `research/hermes/ideator.py`
- Test: `research/tests/test_hermes_ideator.py`

**Interfaces:**
- Consumes: Task 1（`load_field_schema`/`reconcile_schema`）、Task 2–4、`research.hermes.forge.ForgeBudget`
- Produces:
  - `build_ideation_prompt(schema: dict, death_summary: list, n_ideas: int, prior_error: str | None = None) -> str`
  - `generate_ideas(llm, schema, death_summary, n_ideas, budget=None, max_attempts=2) -> tuple[list[dict], list[dict], str | None]`
    回傳 `(accepted_ideas, rejected, failure_reason)`。成功時 `failure_reason is None`。
    `rejected` 是**最後一次嘗試**的 `validate_ideas` 淘汰清單（`{"id","reason"}`），要進 `summary["ideas_rejected"]`（spec §6）—— 若不交出來，那個 summary 欄位就永遠是空的，幻覺欄名的淘汰率無從觀測。

> **設計要點：**
> - prompt **不含任何 IC/績效數值**（Task 2 已保證 `death_summary` 乾淨；prompt 本身也不得加）。
> - prompt 明文禁止**單調變換**（rank/全域 zscore/log/線性縮放）—— 那些 Spearman 恆等於 1.0，是數學恆等式（spec §3.2 結論 1）。
> - prompt 告知 **z 分數與動能兩種變換已被 panel 佔用**（spec §3.2 結論 3）。
> - 描述用**英文**：`description` 會原封不動餵給 `forge._PROMPT`（英文），且 CJK token 密度高會撞 `max_tokens`。
> - `generate_ideas` 每次 `llm.complete()` 前 `budget.charge_call()`，跟 forge 同一個 `ForgeBudget`。

- [ ] **Step 1: 寫失敗測試**（append）

```python
from research.hermes.forge import BudgetExhausted, ForgeBudget
from research.hermes.ideator import build_ideation_prompt, generate_ideas

_SCHEMA = {
    "funding_z": {"what": "rolling z of funding", "positive": "funding high",
                  "notes": "z-score transform already taken"},
    "oi_z": {"what": "rolling z of OI", "positive": "OI high", "notes": "n"},
}


class FakeLLM:
    """CI 絕不呼叫付費 API。"""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else "{}"


_GOOD = '```json\n[{"id": "fz_oi", "description": "short when funding high and OI falling", "fields": ["funding_z", "oi_z"]}]\n```'


def test_prompt_contains_every_schema_field_with_its_meaning():
    p = build_ideation_prompt(_SCHEMA, [], 5)
    for col, entry in _SCHEMA.items():
        assert col in p
        assert entry["what"] in p
        assert entry["positive"] in p


def test_prompt_bans_monotonic_transforms():
    """spec §3.2 結論 1：單調變換 Spearman 恆等於 1.0，是數學恆等式。"""
    p = build_ideation_prompt(_SCHEMA, [], 5).lower()
    assert "monotonic" in p
    for banned in ("rank(", "log("):
        assert banned in p


def test_prompt_states_the_requested_idea_count():
    assert "7" in build_ideation_prompt(_SCHEMA, [], 7)


def test_prompt_includes_death_categories_but_no_numbers():
    deaths = [{"formula": "funding_z * oi_z", "category": "redundant"}]
    p = build_ideation_prompt(_SCHEMA, deaths, 5)
    assert "funding_z * oi_z" in p
    assert "redundant" in p


def test_prompt_repair_variant_feeds_back_the_prior_error():
    p = build_ideation_prompt(_SCHEMA, [], 5, prior_error="IdeationParseError: no JSON")
    assert "IdeationParseError: no JSON" in p


def test_generate_ideas_returns_validated_ideas():
    llm = FakeLLM(_GOOD)
    ideas, rejected, failure = generate_ideas(llm, _SCHEMA, [], n_ideas=5)
    assert failure is None
    assert [i["id"] for i in ideas] == ["fz_oi"]
    assert rejected == []
    assert len(llm.prompts) == 1


def test_generate_ideas_reports_rejected_ideas_alongside_accepted():
    """spec §6：淘汰要看得見，否則幻覺欄名的淘汰率無從觀測。"""
    llm = FakeLLM('[{"id": "ok", "description": "d", "fields": ["funding_z"]},'
                  ' {"id": "bad", "description": "d", "fields": ["liquidation_z"]}]')
    ideas, rejected, failure = generate_ideas(llm, _SCHEMA, [], n_ideas=5)
    assert failure is None
    assert [i["id"] for i in ideas] == ["ok"]
    assert rejected == [{"id": "bad", "reason": "unknown panel columns: ['liquidation_z']"}]


def test_generate_ideas_validates_against_the_schema_keys_not_the_panel():
    """schema 已被 reconcile_schema 收斂成 panel 交集，所以 schema keys 就是
    ideator 能用的欄位全集。"""
    llm = FakeLLM('[{"id": "x", "description": "d", "fields": ["not_a_column"]}]',
                  '[{"id": "x", "description": "d", "fields": ["not_a_column"]}]')
    ideas, rejected, failure = generate_ideas(llm, _SCHEMA, [], n_ideas=5)
    assert ideas == []
    assert failure is not None and "not_a_column" in failure
    assert rejected[0]["id"] == "x"


def test_generate_ideas_retries_once_on_bad_json_then_succeeds():
    """LLM 不吐 fence 是已知高頻故障 —— 給 1 次重試 + 錯誤回饋。"""
    llm = FakeLLM("抱歉，我無法完成。", _GOOD)
    ideas, rejected, failure = generate_ideas(llm, _SCHEMA, [], n_ideas=5)
    assert failure is None and len(ideas) == 1
    assert len(llm.prompts) == 2
    assert "IdeationParseError" in llm.prompts[1]


def test_generate_ideas_gives_up_after_max_attempts():
    llm = FakeLLM("nope", "still nope")
    ideas, rejected, failure = generate_ideas(llm, _SCHEMA, [], n_ideas=5)
    assert ideas == []
    assert failure is not None and "IdeationParseError" in failure
    assert len(llm.prompts) == 2


def test_generate_ideas_charges_the_shared_forge_budget():
    budget = ForgeBudget(max_llm_calls=5)
    generate_ideas(FakeLLM(_GOOD), _SCHEMA, [], n_ideas=5, budget=budget)
    assert budget.used == 1


def test_generate_ideas_propagates_budget_exhausted():
    """預算耗盡是基礎設施耗盡，不是 ideation 失敗 —— 必須往外拋。"""
    budget = ForgeBudget(max_llm_calls=0)
    with pytest.raises(BudgetExhausted):
        generate_ideas(FakeLLM(_GOOD), _SCHEMA, [], n_ideas=5, budget=budget)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_ideator.py -k "prompt or generate" -q`
Expected: FAIL — `ImportError: cannot import name 'build_ideation_prompt'`

- [ ] **Step 3: 寫最小實作**（append 到 `ideator.py`）

```python
_IDEATION_PROMPT = """You are a quantitative researcher proposing NEW alpha factors for a \
crypto perpetual-futures research pipeline (1-hour bars).

You may use ONLY these columns. Nothing else exists.

{fields}

{deaths}
Hard rules — an idea that breaks any of these is wasted budget:
1. Propose ideas that are ORTHOGONAL to the columns above. A downstream gate kills any \
factor whose |Spearman| against ANY existing column is >= 0.7.
2. NEVER propose a MONOTONIC transform of a single column (rank(x), a global z-score of x, \
log(x), any linear rescale). Spearman is a RANK correlation, so a monotonic transform of x \
scores EXACTLY 1.0 against x. It is mathematically guaranteed to be rejected.
3. Rolling z-scores and momentum/differencing are ALREADY TAKEN for the main crypto series \
(that is what the _z and _mom columns are). Do not re-propose them.
4. What is NOT taken, and is where the opportunity is: dispersion/volatility of a series, \
asymmetry, persistence/duration of a state, quantile position, and above all CONDITIONAL or \
INTERACTION logic across two or more columns (e.g. "act on X only while Y is in a given state").
5. Point-in-time: a value at bar t may use only bars <= t. No look-ahead.
6. Write the economic reasoning FIRST, then the factor. An idea with no economic story is \
data mining and will not survive out-of-sample.

Return ONLY a fenced ```json block: a list of exactly {n} objects, each with:
  "id"          — short snake_case identifier, unique
  "description" — ENGLISH, <= 200 chars. The economic hypothesis AND how to compute it. \
This string is handed verbatim to a code-writing model, so it must be precise enough to \
implement without guessing.
  "fields"      — list of the column names the factor reads. Must all come from the list above.
{repair}"""

_REPAIR_TEMPLATE = """
Your previous response failed with:
{error}
Return ONLY the fenced ```json block this time. No prose outside it."""


def _render_fields(schema: dict) -> str:
    return "\n".join(
        f"- {col}: {e['what']}. POSITIVE MEANS: {e['positive']}. NOTE: {e['notes']}"
        for col, e in schema.items())


def _render_deaths(deaths: list) -> str:
    if not deaths:
        return ""
    lines = "\n".join(f"- {d['formula']}  [died: {d['category']}]" for d in deaths)
    return (
        "These factors were already tried on this symbol and BURIED. Do not re-propose "
        "them or trivial variants of them:\n"
        f"{lines}\n"
        "  redundant = too correlated with something that already exists\n"
        "  weak_ic = no predictive power\n"
        "  turnover = traded too often to be viable after costs\n"
        "  dsr = not significant once multiple testing was accounted for\n"
        "  forge_failed = the code never ran; the idea itself was never tested\n\n")


def build_ideation_prompt(schema: dict, death_summary: list, n_ideas: int,
                          prior_error: "str | None" = None) -> str:
    """Assemble the ideation prompt. Carries NO IC/performance numbers (spec §4.1)."""
    repair = "" if not prior_error else _REPAIR_TEMPLATE.format(error=prior_error)
    return _IDEATION_PROMPT.format(
        fields=_render_fields(schema), deaths=_render_deaths(death_summary),
        n=n_ideas, repair=repair)


def generate_ideas(llm, schema: dict, death_summary: list, n_ideas: int,
                   budget=None, max_attempts: int = 2) -> tuple:
    """One (or at most `max_attempts`) LLM call(s) -> validated ideas.

    Returns (accepted, rejected, failure_reason); failure_reason is None on
    success. `rejected` is the LAST attempt's validate_ideas fallout and is
    surfaced in the run summary: without it, the hallucinated-column rejection
    rate is invisible, and that rate is exactly what tells us whether the field
    schema is doing its job.

    Bounded retry with error feedback mirrors forge's repair loop, and is
    justified by evidence rather than caution: a missing JSON fence is this
    repo's known high-frequency LLM failure (stage0/stage2's swarm hit it
    repeatedly).

    BudgetExhausted propagates rather than being swallowed into failure_reason:
    like forge, running out of LLM calls is INFRASTRUCTURE exhaustion, not a bad
    response, and burning a retry on it would be wrong.

    `schema` must already be reconcile_schema()'d down to columns that really
    exist in the panel, so its keys ARE the legal field set for validation.
    """
    prior_error, rejected = None, []
    for _attempt in range(max_attempts):
        if budget is not None:
            budget.charge_call()                       # propagates BudgetExhausted
        prompt = build_ideation_prompt(schema, death_summary, n_ideas, prior_error)
        try:
            raw = parse_ideas(llm.complete(prompt))
        except IdeationParseError as exc:
            prior_error, rejected = f"{type(exc).__name__}: {exc}", []
            continue
        accepted, rejected = validate_ideas(raw, schema.keys())
        if accepted:
            return accepted, rejected, None
        prior_error = "every idea was rejected: " + "; ".join(
            f"{r['id']}: {r['reason']}" for r in rejected)
    return [], rejected, prior_error
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/test_hermes_ideator.py -q`
Expected: PASS（39 passed）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/ideator.py research/tests/test_hermes_ideator.py
git commit -m "$(cat <<'EOF'
feat(hermes): generate crypto-native factor hypotheses from the panel schema

This is the piece that was never built. hypotheses_from_llm() has always
been an adapter waiting for a producer, and run_foundry passed llm_raw=[].

The prompt spends its budget on the three things measurement says
matter. It bans monotonic single-column transforms outright, because
Spearman is a rank correlation and rank(x) scores exactly 1.0 against x
-- an identity, not a tendency. It states that rolling z-scores and
momentum are already taken, since funding_z and funding_mom ARE those
transforms, and a re-proposal is a guaranteed redundant rejection. And
it points at what is actually unclaimed: dispersion, asymmetry,
persistence, quantile position, and conditional logic across columns.

Descriptions are English because they are handed verbatim to forge's
(English) code prompt, and because CJK token density makes a 25-idea
response overrun max_tokens.

One bounded retry with error feedback, mirroring forge. That is not
caution: a missing JSON fence is this repo's known high-frequency LLM
failure. BudgetExhausted propagates rather than burning the retry --
running out of calls is infrastructure, not a bad response.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `build_queue` 來源開關

**Files:**
- Modify: `research/hermes/hypothesis_queue.py:218-234`
- Test: `research/tests/test_hermes_hypothesis_queue.py`

**Interfaces:**
- Consumes: 無新依賴
- Produces: `build_queue(symbol, manifests_dir, zoo_dir=None, llm_raw=None, derived_bases=(), sources=DEFAULT_SOURCES)`
- Produces: `DEFAULT_SOURCES: frozenset` == `frozenset({"llm"})`

> **為什麼預設值就是關閉狀態（spec §5.2）：** 不新增 config 檔。`sources` 沿用既有依賴注入慣例（`oos_start` / `ohlcv` 都這樣傳）。要重跑 zoo 當對照組，enqueue 一個 `params.sources = ["llm","zoo"]` 的 job 即可。
>
> **`zoo_dir` 從必填變選填：** 預設不啟用 zoo 時不該逼呼叫端提供 zoo 路徑。但**只要 `"zoo" in sources` 就必須有 `zoo_dir`**，否則 `Path(None).rglob` 會炸 `TypeError` —— 要 fail loud。

- [ ] **Step 1: 寫失敗測試**（append 到 `research/tests/test_hermes_hypothesis_queue.py`）

```python
from research.hermes.hypothesis_queue import DEFAULT_SOURCES, build_queue


def test_default_sources_is_llm_only():
    """zoo/derived/academic 三條源在 crypto perp 上都是死的（spec §1、Q4）。"""
    assert DEFAULT_SOURCES == frozenset({"llm"})


def test_build_queue_defaults_to_llm_only(tmp_path):
    q = build_queue(symbol="eth", manifests_dir=tmp_path,
                    llm_raw=[{"id": "a", "description": "funding_z vs oi_z"}])
    assert [h.source for h in q] == ["llm"]


def test_build_queue_without_zoo_does_not_need_a_zoo_dir(tmp_path):
    """預設不跑 zoo 時，不該逼呼叫端提供 zoo 路徑。"""
    q = build_queue(symbol="eth", manifests_dir=tmp_path,
                    llm_raw=[{"id": "a", "description": "d"}], derived_bases=["funding_z"])
    assert [h.source for h in q] == ["llm"]      # derived 也不在預設集合裡


def test_build_queue_can_re_enable_zoo(tmp_path, zoo_dir_with_one_factor):
    q = build_queue(symbol="eth", manifests_dir=tmp_path,
                    zoo_dir=zoo_dir_with_one_factor,
                    llm_raw=[{"id": "a", "description": "d"}],
                    sources=frozenset({"llm", "zoo"}))
    assert {h.source for h in q} == {"llm", "zoo"}


def test_build_queue_raises_if_zoo_enabled_without_a_zoo_dir(tmp_path):
    """fail loud：Path(None).rglob 會拋一個看不懂的 TypeError。"""
    with pytest.raises(ValueError, match="zoo_dir"):
        build_queue(symbol="eth", manifests_dir=tmp_path, sources=frozenset({"zoo"}))


def test_build_queue_rejects_an_unknown_source(tmp_path):
    with pytest.raises(ValueError, match="unknown source"):
        build_queue(symbol="eth", manifests_dir=tmp_path, sources=frozenset({"telepathy"}))


@pytest.fixture
def zoo_dir_with_one_factor(tmp_path):
    d = tmp_path / "zoo"
    d.mkdir()
    (d / "f.py").write_text(
        '__alpha_meta__ = {"id": "z1", "formula_latex": "close - open", "theme": ["trend"]}\n'
        'def compute(panel):\n    return panel["close"]\n', encoding="utf-8")
    return d
```

（`import pytest` 若檔案頂端還沒有就補上。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_hypothesis_queue.py -k sources -q`
Expected: FAIL — `ImportError: cannot import name 'DEFAULT_SOURCES'`

- [ ] **Step 3: 改實作**

把 `hypothesis_queue.py` 檔尾的 `build_queue` 整個換掉：

```python
# zoo/derived/academic are all structurally dead on crypto perps, so the DEFAULT
# is llm-only (spec Q1/Q4):
#   zoo      — 456 equity/A-share technical alphas touching only close/volume. Worse
#              than useless: their descriptions are LaTeX, so each costs 1-3 LLM
#              calls to re-forge, draining the call budget around hypothesis 20.
#              That is why eth had 6 evidence cards instead of 50.
#   derived  — _DERIVE_TRANSFORMS is ("zscore", "rank"), applied to columns already
#              IN the panel. rank(x) is strictly monotonic in x, and the dedup gate
#              uses SPEARMAN, so it scores exactly 1.0 against its own parent:
#              rejected as redundant with certainty.
#   academic — the two seeds are equity OHLCV momentum/low-vol; the panel already
#              carries roc_10 and rolling_std_20.
# Nothing is deleted: re-enable any source per job via params["sources"] to rerun
# one as a control, without a code change.
DEFAULT_SOURCES = frozenset({SOURCE_LLM})
_ALL_SOURCES = frozenset({SOURCE_ZOO, SOURCE_DERIVED, SOURCE_ACADEMIC, SOURCE_LLM})


def build_queue(symbol: str, manifests_dir, zoo_dir=None, llm_raw: list | None = None,
                derived_bases=(), sources=DEFAULT_SOURCES) -> list[Hypothesis]:
    """Assemble the Foundry hypothesis queue from the ENABLED sources, then dedupe
    (string-fingerprint collision) and filter_static (graveyard + dead classes).

    `sources` selects which adapters contribute; see DEFAULT_SOURCES above for why
    the default is llm-only.

    `zoo_dir` is optional now that zoo is off by default -- but it is REQUIRED when
    zoo is enabled, and that is checked up front: Path(None).rglob raises a TypeError
    that says nothing about the real mistake.

    `derived_bases` is a caller-ranked list of base feature names. It is NOT read
    from evidence_<sym>.json: that file's IC spans the reserved OOS window, so
    selecting from it leaks OOS information into what Foundry chooses to try.
    """
    sources = frozenset(sources)
    if unknown := sources - _ALL_SOURCES:
        raise ValueError(f"unknown source(s) {sorted(unknown)}; known: {sorted(_ALL_SOURCES)}")
    if SOURCE_ZOO in sources and zoo_dir is None:
        raise ValueError("zoo_dir is required when 'zoo' is in sources")

    collected: list = []
    if SOURCE_ZOO in sources:
        collected += hypotheses_from_zoo(zoo_dir)
    if SOURCE_DERIVED in sources:
        collected += hypotheses_from_derivation(derived_bases)
    if SOURCE_ACADEMIC in sources:
        collected += hypotheses_from_academic()
    if SOURCE_LLM in sources:
        collected += hypotheses_from_llm(llm_raw or [])
    return filter_static(dedupe(collected), symbol, manifests_dir)
```

- [ ] **Step 4: 跑全部 queue 測試確認通過**

Run: `python -m pytest research/tests/test_hermes_hypothesis_queue.py -q`
Expected: PASS

既有測試若因為 `build_queue` 現在預設不含 zoo 而失敗，**在該測試明確傳 `sources=frozenset({"zoo", ...})`** 來保留它原本的意圖 —— 不要改 `DEFAULT_SOURCES`。

- [ ] **Step 5: Commit**

```bash
git add research/hermes/hypothesis_queue.py research/tests/test_hermes_hypothesis_queue.py
git commit -m "$(cat <<'EOF'
feat(hermes): make queue sources selectable, default to llm-only

build_queue returned zoo + derived + academic + llm and run_foundry
sliced [:50], so the first 50 hypotheses were 100% zoo -- measured
composition was {zoo: 351, derived: 2, academic: 2, llm: 1}. The other
three sources never ran once, and never could have.

All three are structurally dead here. zoo is 456 equity/A-share alphas
touching only close/volume, and its LaTeX descriptions cost 1-3 LLM
calls each to re-forge, draining the 60-call budget around hypothesis 20
-- which is why eth has 6 evidence cards rather than 50. derived applies
("zscore", "rank") to columns already in the panel, and rank(x) is
strictly monotonic in x while the dedup gate is Spearman, so it scores
exactly 1.0 against its own parent. academic's two seeds are equity
momentum and low-vol; the panel already carries roc_10 and
rolling_std_20.

Nothing is deleted. Any source can be re-enabled per job via
params["sources"] to rerun it as a control, without touching code.

zoo_dir becomes optional but is still required, and checked up front,
when zoo is enabled -- Path(None).rglob raises a TypeError that says
nothing about the actual mistake.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: orchestrator 接線

**Files:**
- Modify: `research/hermes/orchestrator.py:188-196`（`Budget`）、`:282-395`（`run_foundry`）
- Test: `research/tests/test_hermes_orchestrator.py`

**Interfaces:**
- Consumes: Task 1、Task 2、Task 5、Task 6
- Produces: `run_foundry(..., sources=DEFAULT_SOURCES, n_ideas=25)`；summary 新增 `ideas_accepted: int`、`ideas_rejected: list`、`ideation_failed: str`（僅失敗時）
- Produces: `Budget(max_factors=20, early_stop_after=20, max_llm_calls=60)`

> **`early_stop_after` 從 8 放寬到 20（spec Q5）：** 早停的設計前提是佇列有多條源可跳；現在只剩一條，早停只會給截斷樣本。而第一輪的**目的就是量測點子品質** —— 樣本 8 分不出「點子爛」還是「運氣差」。`max_llm_calls=60` 的硬上限沒動，超支照樣 `BudgetExhausted`。

- [ ] **Step 1: 寫失敗測試**（append 到 `research/tests/test_hermes_orchestrator.py`）

```python
from research.hermes.orchestrator import Budget, run_foundry


def test_budget_defaults_match_the_single_source_reality():
    b = Budget()
    assert b.max_factors == 20
    # 只剩一條源，沒有下一條可跳 -> 早停只會給截斷樣本（spec Q5）
    assert b.early_stop_after >= b.max_factors
    assert b.max_llm_calls == 60          # 硬上限不動


def test_run_foundry_wires_llm_ideas_into_the_queue(foundry_env, monkeypatch):
    """整條線最重要的一條測試：llm_raw 曾經是寫死的 []。"""
    import research.hermes.orchestrator as orch

    seen = {}

    def fake_generate(llm, schema, deaths, n_ideas, budget=None, **kw):
        seen["schema_cols"] = sorted(schema)
        seen["n_ideas"] = n_ideas
        return [{"id": "fz_oi", "description": "funding_z conditional on oi_z",
                 "fields": ["funding_z", "oi_z"]}], [], None

    monkeypatch.setattr(orch, "generate_ideas", fake_generate)
    summary = run_foundry(**foundry_env)

    assert summary["queue_composition"] == {"llm": 1}
    assert summary["ideas_accepted"] == 1
    assert "zoo" not in summary["queue_composition"]
    assert seen["n_ideas"] == 25          # 要 25 個，取前 20（spec §5.3）


def test_run_foundry_records_ideation_failure_without_crashing(foundry_env, monkeypatch):
    """nightly cron 不能因為 LLM 吐垃圾就掛掉。"""
    import research.hermes.orchestrator as orch
    monkeypatch.setattr(orch, "generate_ideas",
                        lambda *a, **k: ([], [], "IdeationParseError: no JSON"))
    summary = run_foundry(**foundry_env)

    assert summary["ideation_failed"] == "IdeationParseError: no JSON"
    assert summary["candidate"] == 0
    assert summary["queue_composition"] == {}


def test_run_foundry_surfaces_rejected_ideas_in_the_summary(foundry_env, monkeypatch):
    """幻覺欄名的淘汰率要看得見 —— 那是 field_schema 有沒有在做事的訊號。"""
    import research.hermes.orchestrator as orch
    monkeypatch.setattr(orch, "generate_ideas", lambda *a, **k: (
        [{"id": "ok", "description": "d", "fields": ["funding_z", "oi_z"]}],
        [{"id": "bad", "reason": "unknown panel columns: ['liquidation_z']"}], None))
    summary = run_foundry(**foundry_env)
    assert summary["ideas_accepted"] == 1
    assert summary["ideas_rejected"] == [
        {"id": "bad", "reason": "unknown panel columns: ['liquidation_z']"}]


def test_run_foundry_only_offers_documented_panel_columns(foundry_env, monkeypatch):
    """schema 跟 panel 對不上時，執行期取交集 + warning，不 crash（spec §6）。"""
    import research.hermes.orchestrator as orch
    monkeypatch.setattr(orch, "load_field_schema", lambda: {
        "funding_z": {"what": "w", "positive": "p", "notes": "n"},
        "long_gone_column": {"what": "w", "positive": "p", "notes": "n"}})
    seen = {}

    def fake_generate(llm, schema, deaths, n_ideas, budget=None, **kw):
        seen["cols"] = sorted(schema)
        return [{"id": "a", "description": "d", "fields": ["funding_z"]}], [], None

    monkeypatch.setattr(orch, "generate_ideas", fake_generate)
    run_foundry(**foundry_env)
    assert seen["cols"] == ["funding_z"]          # stale 欄位沒被端給 LLM
```

`foundry_env` fixture：沿用 `research/tests/test_hermes_orchestrator.py` 既有的 run_foundry 呼叫慣例（features parquet、`ohlcv`、`oos_start`、fake `llm`、fake `sandbox`/`run_sandbox`）。若既有測試已有等價 fixture，**直接重用，不要另建一個**。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_orchestrator.py -k "ideas or ideation or budget_defaults" -q`
Expected: FAIL — `AssertionError: assert 50 == 20`（`Budget` 還沒改）

- [ ] **Step 3: 改 `Budget`**

```python
@dataclass(frozen=True)
class Budget:
    # 20, not 50: the queue is llm-only now, and the ideator asks for 25 ideas per
    # run (a few die in validation). 50 was sized for a 456-strong zoo queue.
    max_factors: int = 20
    # Effectively OFF (>= max_factors). early_stop_after existed to bail out of a
    # diverging night and move the budget on -- but with a single source there is
    # nothing to move on TO, so it would only truncate the sample. The first runs
    # exist precisely to MEASURE idea quality, and 8 outcomes cannot tell "the
    # ideas are bad" from "the draw was unlucky".
    early_stop_after: int = 20
    # Unchanged. This is the real circuit breaker, charged inside forge()'s repair
    # loop AND by generate_ideas.
    max_llm_calls: int = 60
```

- [ ] **Step 4: 改 `run_foundry`**

在 `orchestrator.py` 頂端 import：

```python
from research.hermes.evidence_store import load_cards, upsert_card
from research.hermes.field_schema import load_field_schema, reconcile_schema
from research.hermes.hypothesis_queue import DEFAULT_SOURCES, build_queue
from research.hermes.ideator import generate_ideas, summarize_deaths
```

（既有的 `from research.hermes.evidence_store import upsert_card` 併入上面那行；`build_queue` 的 import 加上 `DEFAULT_SOURCES`。）

改簽章：

```python
def run_foundry(symbol, manifests_dir, cfg, llm, sandbox, budget, zoo_dir=None, *,
                oos_start, ohlcv, val_frac=0.2, derived_top_k=5,
                daily_regime=None, run_sandbox=None, forge_budget=None,
                pause_file=None, sources=DEFAULT_SOURCES, n_ideas=25) -> dict:
```

docstring 的 `zoo_dir is REQUIRED (agy 4c...)` 那段改成：

```
    zoo_dir is optional: zoo is off by default (see hypothesis_queue.DEFAULT_SOURCES).
    It is required only when 'zoo' is in `sources`, and build_queue checks that.
```

把 `queue = build_queue(...)` 那一段（`orchestrator.py:350-351`）換成：

```python
    # ── ideation ─────────────────────────────────────────────────────────────
    # This is what was missing: llm_raw was hardcoded to [], so the LLM never
    # proposed anything and the queue was 100% zoo -- 456 equity alphas that only
    # read close/volume, on a panel whose crypto-native columns (funding/basis/OI/
    # long-short) no hypothesis had ever touched.
    ideation_failed = None
    llm_raw: list = []
    ideas_rejected: list = []
    if SOURCE_LLM in sources:
        # Intersect the hand-written schema with the panel's REAL columns. The test
        # asserts these match exactly; the runtime only warns, because stage0a
        # adding a column must not crash that night's cron (spec §6).
        schema, undocumented, stale = reconcile_schema(load_field_schema(), features.columns)
        if undocumented or stale:
            log.warning("%s: field_schema drift -- undocumented panel columns %s, "
                        "stale schema entries %s; offering the LLM only the %d "
                        "columns that are in both", symbol, undocumented, stale, len(schema))
        # Deaths are fed as CATEGORIES with no numbers attached: handing the LLM
        # "IC 0.029 < 0.03" invites it to bolt on a log() until the bar clears,
        # which is automated p-hacking (spec §4.1).
        deaths = summarize_deaths(load_cards(symbol, manifests_dir))
        llm_raw, ideas_rejected, ideation_failed = generate_ideas(
            llm, schema, deaths, n_ideas=n_ideas, budget=forge_budget)
        if ideation_failed:
            log.warning("%s: ideation produced nothing usable: %s", symbol, ideation_failed)

    queue = build_queue(symbol=symbol, manifests_dir=manifests_dir, zoo_dir=zoo_dir,
                        llm_raw=llm_raw, derived_bases=derived_bases,
                        sources=sources)[: budget.max_factors]
```

**注意 `forge_budget` 的建立順序**：`generate_ideas` 要收 `forge_budget`，但目前 `forge_budget = forge_budget or ForgeBudget(...)` 在 `orchestrator.py:356`（ideation 之後）。**把那一行往上搬到 ideation 區塊之前**：

```python
    # One breaker for the whole sweep, charged inside forge()'s repair loop AND by
    # generate_ideas. Must exist BEFORE ideation: an ideation call is real spend.
    # A caller sweeping multiple jobs (reconcile) can pass a shared ForgeBudget so
    # spend is counted across the whole batch, not reset per job.
    forge_budget = forge_budget or ForgeBudget(max_llm_calls=budget.max_llm_calls)
```

在 summary 區塊補上：

```python
    summary["ideas_accepted"] = len(llm_raw)
    summary["ideas_rejected"] = ideas_rejected
    if ideation_failed:
        summary["ideation_failed"] = ideation_failed
```

頂端 import 補 `SOURCE_LLM`：

```python
from research.hermes.hypothesis import SOURCE_LLM
```

- [ ] **Step 5: 跑全部 hermes 測試**

Run: `python -m pytest research/tests/ -q`
Expected: PASS（全綠）

既有 orchestrator 測試若因為 `build_queue` 預設變了而失敗，**在測試裡明確傳 `sources=`**，不要改預設值。

- [ ] **Step 6: Commit**

```bash
git add research/hermes/orchestrator.py research/tests/test_hermes_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(hermes): wire LLM ideation into run_foundry

llm_raw was hardcoded to [], so the LLM never proposed anything -- the
one line that made the whole autonomous chain an empty pipe. run_foundry
now reconciles the field schema against the panel's real columns,
summarizes the graveyard into categories, and asks the ideator for 25
ideas before building the queue.

The schema intersection deliberately warns instead of raising: the test
asserts exact coverage because a rotted schema is a repo bug, but
stage0a adding a column must not crash that night's cron -- the column
simply isn't offered until someone documents it.

forge_budget moves above ideation. An ideation call is real spend, and
it must be charged against the same breaker forge uses, or a shared
batch budget silently undercounts.

Budget defaults move to max_factors=20 with early stopping effectively
off. early_stop_after existed to bail out of a diverging night and move
the budget on, but with a single source there is nothing to move on to,
so it would only truncate the sample -- and these runs exist to MEASURE
idea quality. Eight outcomes cannot separate "the ideas are bad" from
"the draw was unlucky". max_llm_calls stays at 60.

Ideation failure is recorded in the summary rather than raised: a
nightly cron must not die because the LLM returned prose.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: job params 傳遞 `sources`

**Files:**
- Modify: `research/hermes/orchestrator.py:419-446`（`run_foundry_job`）
- Test: `research/tests/test_hermes_orchestrator.py`

**Interfaces:**
- Consumes: Task 7
- Produces: `run_foundry_job` 讀 `params.get("sources")`，缺省時用 `DEFAULT_SOURCES`

- [ ] **Step 1: 寫失敗測試**（append）

```python
def test_run_foundry_job_defaults_to_llm_only(tmp_path, monkeypatch, job_env):
    import research.hermes.orchestrator as orch
    seen = {}
    monkeypatch.setattr(orch, "run_foundry",
                        lambda *a, **kw: seen.update(kw) or {"candidate": 0})
    orch.run_foundry_job(**job_env)
    assert seen["sources"] == frozenset({"llm"})


def test_run_foundry_job_honours_an_explicit_sources_list(tmp_path, monkeypatch, job_env):
    """重跑 zoo 當對照組：enqueue params.sources=["llm","zoo"]，不必改碼。"""
    import research.hermes.orchestrator as orch
    job = json.loads(Path(job_env["job_path"]).read_text(encoding="utf-8"))
    job["params"]["sources"] = ["llm", "zoo"]
    Path(job_env["job_path"]).write_text(json.dumps(job), encoding="utf-8")

    seen = {}
    monkeypatch.setattr(orch, "run_foundry",
                        lambda *a, **kw: seen.update(kw) or {"candidate": 0})
    orch.run_foundry_job(**job_env)
    assert seen["sources"] == frozenset({"llm", "zoo"})
```

`job_env` fixture：沿用既有 `run_foundry_job` 測試的慣例（`enqueue_foundry_job` 寫出 job.json，params 含 `oos_start`）。既有若有等價 fixture 就重用。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_orchestrator.py -k run_foundry_job -q`
Expected: FAIL — `KeyError: 'sources'`

- [ ] **Step 3: 改實作**

在 `run_foundry_job` 的 `run_foundry(...)` 呼叫加一個參數：

```python
        summary = run_foundry(job["symbol"], manifests_dir, cfg, llm, sandbox,
                              budget or Budget(), zoo_dir=zoo_dir, ohlcv=ohlcv,
                              oos_start=p["oos_start"], val_frac=p.get("val_frac", 0.2),
                              forge_budget=forge_budget, pause_file=pause_file,
                              # Per-job override of the enabled sources. Re-running
                              # zoo as a control is an enqueue, not a code change.
                              sources=frozenset(p.get("sources", DEFAULT_SOURCES)))
```

- [ ] **Step 4: 跑全部研究測試**

Run: `python -m pytest research/tests/ -q`
Expected: PASS（全綠）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/orchestrator.py research/tests/test_hermes_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(hermes): let a foundry job choose its hypothesis sources

params["sources"] overrides the llm-only default per job, so re-running
zoo or derived as a control is an enqueue rather than a code change.
Follows the same dependency-injection path as oos_start and ohlcv; no
new config file.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: 驗收真跑（eth，真 LLM + 真 Docker）

**Files:** 無（只跑，不改碼；除非抓到 bug）

> **鐵律：不信 mock 綠燈。** 前三次（B regime / A mine / 轉正 golden）都是 mock 掉整合點導致綠燈假象，真 bug 只有真跑才抓到。**Task 1–8 全綠 ≠ 這件事能用。**

> **⚠️ 這一步會花錢。開跑前必須向使用者取得明確同意**，並攤開預估花費（call 數 × 模型單價）。使用者授權「跑 foundry」不等於授權任意金額。

- [ ] **Step 1: 確認前置條件**

```bash
python -c "import pandas as pd; d=pd.read_parquet('research/manifests/features_eth.parquet'); print(len(d.columns),'cols', d.index.min(), d.index.max())"
docker info > /dev/null && echo "docker ok"
```
Expected: 31 cols + `docker ok`

- [ ] **Step 2: 向使用者說明兩個預設值會擋住這次驗收，並取得同意**

**這兩個是真跑會踩到的地雷，不講清楚就會跑出一個假結果：**

1. **`--batch-max-llm-calls` 預設是 `6`**（不是 `Budget.max_llm_calls=60`）。`reconcile_foundry_jobs` 傳的 shared `ForgeBudget` 會蓋過 per-run 預設。6 call = 1 次 ideation + 5 次 forge ≈ **只跑得完 2 個點子**，拿不到 spec Q5 要的 20 個樣本。驗收要傳 `--batch-max-llm-calls 61`（1 ideation + 20 點子 × 3 retry）。
2. **`--max-tokens` 預設是 `2048`**。ideator 一次要吐 25 個點子的 JSON；即使 description 限英文 200 字元，25 × ~60 token ≈ 1500 已經很貼邊。驗收要傳 `--max-tokens 4096`。（`ChatCoder` 在建構時就綁死 `max_tokens`，forge 和 ideator 共用同一個 client，無法只調 ideation 那一次。）

向使用者報預估花費，等明確同意。

- [ ] **Step 3: Enqueue 一個 eth job**

```bash
python -c "
from research.hermes.orchestrator import enqueue_foundry_job
p = enqueue_foundry_job('eth', 'research/runs', {
    'oos_start': '2025-01-01', 'interval': '1H', 'horizon_h': 24,
    'ohlcv_path': 'research/manifests/ohlcv_eth.parquet',
})
print(p)
"
```

`ohlcv_path` 用該 checkout 裡真實存在的 candle parquet —— **先 `ls research/manifests/` 確認檔名**，不要照抄。

- [ ] **Step 4: 跑 foundry runner**

```bash
python -m research.hermes.foundry_runner run \
  --runs-dir research/runs --manifests-dir research/manifests \
  --llm openrouter --max-tokens 4096 --batch-max-llm-calls 61 \
  --confirm-spend 2>&1 | tee scratchpad_ideation_acceptance.log
```

`--confirm-spend` 的正確旗標名以 `python -m research.hermes.foundry_runner run --help` 為準（[foundry_runner.py:185](research/hermes/foundry_runner.py:185) 有一個「不加就拒跑」的付費確認旗標）。

- [ ] **Step 5: 驗收判定**

```bash
python -c "
import json, glob
for f in sorted(glob.glob('research/runs/foundry_jobs/*/job.json')):
    j = json.load(open(f, encoding='utf-8'))
    if j.get('status') != 'done': continue
    s = j['summary']
    print(json.dumps({k: s.get(k) for k in (
      'queue_composition','ideas_accepted','ideas_rejected','ideation_failed',
      'candidate','rejected','forge_failed','llm_calls_used')},
      ensure_ascii=False, indent=2))
"
python -c "
from research.hermes.evidence_store import load_cards
from collections import Counter
cards = load_cards('eth', 'research/manifests')
print('sources:', Counter(c.source for c in cards))
print('verdicts:', Counter(c.verdict for c in cards))
for c in cards:
    if c.source == 'llm':
        print(' ', c.factor_id, c.verdict, '|', c.formula[:60], '|', c.death_reason)
"
```

**依 spec §7.2，全部滿足才算過：**

- [ ] `queue_composition` 出現 `llm` 且**不是** 100% zoo
- [ ] 有 `source=llm` 的 EvidenceCard 真的寫出來（過閘或死都算）
- [ ] LLM 點子確實走完 forge → sandbox → gate 全鏈
- [ ] 死因分類合理（`redundant` / `weak_ic` / `turnover` / `dsr`），**不是 `forge_failed` 洗版**
- [ ] **`candidate = 0` 也算通過**（spec §10：那是研究結果，不是 bug）

**若 `forge_failed` 洗版** → 那是真 bug 或 prompt 品質問題，**不是**「LLM 點子不夠聰明」。走 `superpowers:systematic-debugging`，別直接改 prompt 湊。

- [ ] **Step 6: 把真跑結果回報使用者**

貼真實 summary + 卡片內容。**不要**宣稱「機制通了」而不附證據（見記憶 `feedback_verify_before_claiming`）。若 `candidate=0`，照 spec §10 誠實說明：機器造好了，下一題是點子品質，那是 prompt/模型的另一輪迭代。

- [ ] **Step 7: 清掉 log，commit（若有改碼）**

```bash
rm -f scratchpad_ideation_acceptance.log
```

真跑若抓到 bug 並修了，該修正單獨 commit，message 要寫清楚**是真跑抓到的、mock 為什麼沒抓到**。
