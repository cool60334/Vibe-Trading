# Foundry LLM crypto 原生假設生成 — 設計

- 日期：2026-07-16
- 分支：`quant-trading-dashboard`
- 狀態：設計定案，待寫實作計畫
- 二審：agy（Antigravity / Gemini）兩輪

---

## 1. 問題

Talos Foundry（`research/hermes/`）整套已建好並 push（斷点 B 橋、斷点 A 排程、因子轉正三大塊全數完成），但**在 eth/btc/sol 真跑至今挖不出任何 candidate**。整條自主鏈是空管。

根因**不是 bug**，是三個設計缺口。全部讀真碼確認，非推測：

| # | 病灶 | 證據 |
|---|---|---|
| 1 | LLM 出點子從沒接線 | `orchestrator.py:351` `build_queue(..., llm_raw=[])` 寫死。`hypotheses_from_llm()` 只是 adapter，真正的生成端從沒實作 |
| 2 | 佇列前 50 全 zoo | `build_queue` 回傳順序 `zoo + derived + academic + llm`，`run_foundry` 接著切 `[: budget.max_factors]`（=50）。實測 `queue=356 {zoo:351, derived:2, academic:2, llm:1}` → 前 50 個 100% 是 zoo。就算把 LLM 接好，點子排第 352 位永遠跑不到；derived/academic 也從沒跑過 |
| 3 | zoo 吃光 LLM 預算 | zoo 描述是 LaTeX，LLM 每個要燒 1~3 call 重寫。`max_llm_calls=60` → 約第 20 個 hypothesis 就見底。這解釋了 eth 為何只有 6 張 EvidenceCard 而非 50 張 |

### 1.1 真跑證據

```
eth: 6 張卡，全 graveyard，全 zoo
  zoo_academic_carhart_mom  graveyard  weak gross_ic -0.0279 < 0.03
  zoo_academic_cma          graveyard  weak gross_ic  0.0090 < 0.03
  zoo_academic_high52w      graveyard  weak gross_ic -0.0131 < 0.03
  zoo_academic_hml          graveyard  weak gross_ic     nan < 0.03
  zoo_academic_illiq        graveyard  SandboxRunFailed: KeyError: -1
btc: 0 張    sol: 0 張
```

### 1.2 頻率／資料錯配

`agent/src/factors/zoo/` 那 456 個因子是**股票/A 股技術因子**（alpha101 / gtja191 / qlib158 / academic），只碰 `close` / `volume`。

但 `features_eth.parquet` 有 31 欄，crypto 原生欄位齊全：

```
funding_rate_raw funding_z funding_mom
basis_rel basis_z basis_mom
oi_change_24h oi_z oi_mom oi_price_divergence
global_ls_acct_z toptrader_ls_z ls_divergence
stablecoin_supply_z depeg depeg_z fiat_prem fiat_prem_z
+ 13 個 OHLCV 技術指標（rsi_14 macd_diff roc_10 stoch_k ema_cross_9_21
  sma_cross_10_30 adx_14 atr_14 bb_width_20 rolling_std_20 obv mfi_14
  volume_zscore_20）
```

**這些 crypto 原生欄位從來沒有任何 hypothesis 碰過。** 本 repo 的現役 alpha 全部落在 funding / basis / OI / 多空比。Foundry 名為「自主發掘」，實際只在幫死掉的股票因子翻譯程式碼。

---

## 2. 已拍板決策

| # | 決策 | 理由 |
|---|---|---|
| Q1 | **zoo 預設關閉**，config 可開，不刪碼 | 它是佔著 LLM 預算的活體阻塞物（§1 病灶 3）。不刪碼是為了換幣種／週期可能翻身，且刪了無法重現歷史卡片 |
| Q2 | ideator 產出**經濟假說**；明文禁止**單調變換**；prompt 告知哪些變換已被 panel 佔用 | 見 §3 的 dedup 分析 |
| Q3 | 驗收＝**機制通即過**。`candidate=0` 也算通過 | Foundry 是研究系統，無法保證 alpha 存在。把「挖到」綁進工程驗收＝spec 永遠關不掉，且會逼人調鬆 gate 湊數（自動化 p-hacking 的人工版） |
| Q4 | **derived + academic 也預設關閉**。Foundry 純靠 LLM ideator 單一供料 | `_DERIVE_TRANSFORMS = ("zscore","rank")` 套在 panel 既有欄位上 → 單調 → Spearman=1.0 → 保證 redundant。academic 那 2 個 seed 是股票 OHLCV 動能/低波，panel 已有 `roc_10`/`rolling_std_20` |
| Q5 | `max_factors=20`、`early_stop_after=20`（實質關閉）、`max_llm_calls=60` 不動 | 早停的設計前提是佇列有多條源可跳；現在只剩一條，早停只會給出截斷樣本。第一輪的目的**就是量測點子品質**，樣本 8 太小分不出「點子爛」還是「運氣差」 |
| Q6 | ideator 吐結構化 `fields:[...]` 宣告；靜態只檢查 **`fields ⊆ panel 欄位`**（擋幻覺欄名） | 見 §4 |
| Q7 | **手寫** `field_schema.yaml`（31 欄經濟定義） | 自動抽 docstring 已驗證不可行：`basis_factors` / `funding_factors` / `oi_factors`（產出 crypto 核心欄的函式）docstring 全是 `None` |

### 2.1 Q4 的代價（明寫）

Foundry 變成**單一供料源**。LLM 掛掉或出爛點子＝整台停擺，沒有 fallback。

判斷：可接受。現在有三條源也一樣是 0 candidate——**假的冗餘不是冗餘**。

---

## 3. 核心限制：0.7 dedup 閘決定了搜尋空間

Foundry 唯一能算的資料就是 panel 的 31 欄 + OHLCV，**無法引入新資料源**。而 `GateConfig.redundant_abs_spearman = 0.7`（`gatekeeper.py:181`）會斃掉跟 `existing`（= features + graveyard）任一欄相關度 ≥0.7 的因子。

**Foundry 結構上只可能找到跟現有 31 欄都正交的東西。**

### 3.1 實測：哪些變換逃得掉

eth pre-oos 真資料，對 panel 31 欄全掃 max|Spearman|：

| 單欄變換 | max&#124;Spearman&#124; | 撞到誰 | 過 0.7 閘？ |
|---|---|---|---|
| `rank(funding_z)` 〔單調〕 | **1.000** | funding_z | ✗ |
| `global_zscore(funding_z)` 〔單調〕 | **1.000** | funding_z | ✗ |
| `funding - funding.shift(24)` 〔非單調〕 | **1.000** | funding_mom | ✗ |
| `rolling_std(funding, 168)` 〔非單調〕 | 0.617 | depeg | **✓** |
| `rolling_std(oi_z, 72)` 〔非單調〕 | 0.253 | obv | **✓** |
| `abs(basis_rel)` 〔非單調〕 | 0.373 | basis_rel | **✓** |

### 3.2 三個結論

1. **單調變換（rank / 全域 zscore / log / 線性縮放）保證被斃** —— Spearman 是秩相關，單調變換恆等於 1.0。這是數學恆等式，不是經驗猜測。→ prompt 明文禁止。
2. **非單調的單欄時序變換逃得掉** —— `rolling_std(funding,168)` 是 panel 裡沒有、經濟上有意義（資金費機制不穩定度）、且實測過閘的因子。**不可封殺。**
3. **panel 已經佔掉「z 分數」和「動能」兩種變換** —— `funding_z`/`funding_mom`/`basis_z`/`basis_mom`/`oi_z`/`oi_mom`。`funding - funding.shift(24)` 撞 `funding_mom` 撞到 1.000 就是鐵證。**沒被佔掉的是：離散度／波動度、不對稱性（`abs`）、持續時間、分位位置、跨欄交互／條件式。** → 這份「已佔用清單」要寫進 prompt。

> **修正記錄**：設計初版曾用「單欄變換保證被斃」推導出硬規則 `len(set(fields)) >= 2`（強制跨欄）。agy 二審指出該推論只對單調變換成立，實測（§3.1）證實 agy 正確。硬規則已移除——它會為了一個不存在的理由封殺整類有效因子。

---

## 4. 架構

補上 `hypotheses_from_llm` 這個 adapter 一直缺的上游。**架構本來就是兩段式**：`Hypothesis.description`（白話假說）→ `forge._PROMPT` 翻成 `compute(df)` 程式碼。所以 ideator 吐白話經濟假說，第二段自動接上，不需要新架構。

```
field_schema.yaml (31 欄經濟定義)    ─┐
existing 因子名 (不含 IC 數值)        ─┤
graveyard 死因分類 (不含 IC 數值)     ─┼─→ ideator ──1 LLM call──→ JSON 點子
已佔用變換清單 (z 分數/動能)          ─┤                              │
單調變換禁令                          ─┘                              ▼
                                                          validate_ideas (確定性)
                                                          └ fields ⊆ panel 欄位?
                                                                    │
                                              ┌─────────────────────┘
                                              ▼
build_queue(sources={"llm"}) → forge (既有) → sandbox → gate → EvidenceCard
```

### 4.1 為什麼不餵 IC 數值（agy 採納點）

agy 二審指出：把 `IC=0.029 被刷掉` 餵給 LLM，它最省力的路不是找新的金融邏輯，而是套個 `log()` / `ewma` 把 IC 硬逼過 0.03 —— **自動化的 p-hacking**。

→ 只餵**死因分類**（`weak_ic` / `redundant` / `turnover` / `forge_failed`）+ 因子描述。LLM 知道「這條路死了」，但**無法知道差多少**，就沒得逼門檻。

### 4.2 為什麼餵現役因子「名稱」而不是完整資訊

`fields` 靜態檢查擋不住語意重複；LLM 需要知道 panel 裡有什麼才能避開。但餵得太多會誘發 agy 警告的 mode collapse（生成高相關變體 → 撞 0.7 → 進 graveyard → 成為下輪脈絡 → 無限燒錢迴圈）。

→ 折衷：餵**欄位名 + 經濟定義**（field_schema 本來就要餵），**不餵**它們的 IC / 績效。

---

## 5. 元件

| 檔案 | 狀態 | 職責 |
|---|---|---|
| `research/hermes/field_schema.yaml` | 新 | 31 欄 × 「是什麼／正值代表什麼／已知性質／已被佔用的變換」。**使用者逐欄審**（見 §8） |
| `research/hermes/field_schema.py` | 新 | 載入 + 對帳 panel 欄位 |
| `research/hermes/ideator.py` | 新 | `build_ideation_prompt` / `parse_ideas` / `validate_ideas` / `generate_ideas` |
| `research/hermes/hypothesis_queue.py` | 改 | `build_queue(..., sources=...)` 讓來源可開關 |
| `research/hermes/orchestrator.py` | 改 | 呼叫 ideator、傳真 `llm_raw`；`Budget` 預設改為 `max_factors=20` / `early_stop_after=20` |

### 5.2 來源開關住在哪（Q1/Q4 的「config」具體化）

`build_queue(..., sources=frozenset({"llm"}))` —— **預設值就是關閉狀態**，zoo/derived/academic 不在預設集合裡。

覆寫路徑：foundry job 的 `params["sources"]`（`run_foundry_job` 讀 `job.json`）→ `run_foundry(sources=...)` → `build_queue`。

理由：沿用既有的依賴注入慣例（`oos_start` / `ohlcv` 都是這樣傳的），不新增 config 檔。要重跑 zoo 當對照組，enqueue 一個 `params.sources = ["llm","zoo"]` 的 job 即可，不必改碼。

### 5.3 一輪要幾個點子

ideator 向 LLM 要 **25** 個，`validate_ideas` 過濾後取前 **20**（= `Budget.max_factors`）。

要 25 不要 20 是為了吸收 `fields ⊆ panel` 的淘汰率（幻覺欄名）。若過濾後不足 20，就跑剩下幾個 —— **不重新要一批**（那會再燒一次 ideation call，且會誘使 LLM 灌水湊數）。實際數量記進 `summary["ideas_accepted"]`。

### 5.1 被刪掉的元件（agy 二審後）

初版設計有第三條靜態檢查「dead-class 由 `fields` 規則判」。**直接刪除，不是修正。**

`DEAD_CLASSES = {"intraday_ohlcv_price_derived", "binance_orderflow"}`（`hypothesis_queue.py:21`）對 LLM 來源是**空轉的**：

- sub-1H 微結構因子 → LLM 根本算不出來（panel 就是 1H bar，`cfg.interval` 有 1H 地板）
- order-flow 因子 → 那些欄位不存在於 panel，**規則 `fields ⊆ panel` 已經擋掉了**

agy 擔心這條會「連坐封殺」（同樣兩欄相乘／相除／條件觸發是完全不同的假說）。疑慮不用修 —— 拿掉這條就沒了，少一個元件。

---

## 6. 錯誤處理

| 情境 | 處理 |
|---|---|
| LLM 沒吐 JSON fence | **已知高頻故障**（記憶 `feedback_swarm_json_fence_fail`：stage0/2 swarm 常不吐 fence）。給 **1 次重試 + 錯誤回饋**，再失敗放棄。用 `json.loads(strict=False)`（記憶 `feedback_llm_json_strict_false`：LLM 吐長中文 JSON 必用） |
| ideation 全滅 | **不 crash nightly cron**。`summary["ideation_failed"] = <原因>`，佇列空，該輪 0 outcome。誠實記錄，不假裝跑過 |
| 幻覺欄名 | `validate_ideas` 擋下，記進 `summary["ideas_rejected"]`（帶原因）。**在花錢前就死** —— 否則 forge 要白燒 3 次 call 才 KeyError |
| schema 跟 panel 對不上 | 測試紅燈（repo 一致性 bug）；但**執行期只取交集 + warning，不 crash**。stage0a 新增欄位不該炸掉當晚的 cron，只是那欄在有人寫定義前不會餵給 LLM |
| `BudgetExhausted` | 沿用既有：乾淨停止 + summary 標記 |

---

## 7. 測試

### 7.1 CI（全部 fake LLM — 零付費、決定性）

- `field_schema` key ↔ `features_<sym>.parquet` 欄位對帳
- `parse_ideas`：有 fence / 無 fence / 中文長 JSON / 壞 JSON
- `validate_ideas`：幻覺欄名擋下、好點子放行
- `build_queue(sources=...)`：zoo 關閉 → 佇列無 zoo；只開 llm → 佇列全 llm
- `orchestrator`：fake LLM 下 `summary["queue_composition"]` 出現 `llm`

**CI 絕不呼叫付費 LLM** —— 付費 + 非決定性會毀掉 CI（既有決策，agy 前輪確立）。

### 7.2 驗收（真跑，非 CI）

eth 真 LLM + 真 Docker 跑一輪。依 Q3，滿足以下即通過：

- `summary["queue_composition"]` 出現 `llm` 且不再是 100% zoo
- 有 `source=llm` 的 EvidenceCard 真的寫出來（過閘或死都算）
- LLM 點子確實走完 forge → sandbox → gate 全鏈
- 死因分類合理（`redundant` / `weak_ic`…），不是 `forge_failed` 洗版
- **`candidate = 0` 也算驗收通過**

> 鐵律：**不信 mock 綠燈**。前三次（B regime / A mine / 轉正 golden）都是 mock 掉整合點導致綠燈假象，真 bug 只有真跑才抓到。見記憶 `feedback_verify_before_claiming`。

---

## 8. 已知限制（明寫，不裝作沒這回事）

1. **`fields` 是宣告，不是保證。** 實際 code 是 forge 之後才寫的。LLM 可以宣告兩個欄卻寫出只用一個欄的 code —— 靜態檢查綁不住。`validate_ideas` 是**省錢的前置過濾**，不是密不透風的保證。最後防線仍是 gate 的 dedup。
2. **`field_schema.yaml` 是 Claude 寫的，會寫錯。** 對 `depeg_z` / `fiat_prem_z` 這類欄位的理解來自程式碼和記憶，不是市場經驗。**實作計畫必須把「使用者逐欄審這張表」列為明確檢查點。**
3. **`field_schema.yaml` 會腐爛。** stage0a 加新欄位時表不會自動跟上。靠 §7.1 的對帳測試擋。

---

## 9. 明確不做（YAGNI）

| 不做 | 理由 |
|---|---|
| Multi-Armed Bandit / Thompson Sampling 配額調度（agy 提案） | 系統至今 0 candidate。還沒證明 LLM 挖得到東西，就先蓋貝氏調度器去分配一個從未產出的資源＝拿還沒發生的問題當設計。agy 二審已同意此為 YAGNI |
| 刪 zoo 程式碼 | 只在 config 關閉。刪了無法重現歷史卡片 |
| 動 gate 門檻 | p-hacking 的滑坡 |
| 「需要新資料源」的點子另存 backlog | 另一個 feature（新產出檔 + 新人工流程）。要的話單開 |
| bridge / OOS 重驗 | 斷点 B 已建好且測過，不在本 spec |

---

## 10. 誠實的預期出口

機制全通、卡也寫了、`candidate` 仍然 **0**，是**真實可能的結果**。

那時真正的問題會變成「**LLM 出的點子夠不夠聰明**」—— 那是 prompt / 模型的迭代議題，**不是這個 spec 的失敗**。本 spec 的產出是一台接好線、能被量測的機器；機器造好之後點子品質好不好，是下一輪的事。

---

## 11. agy 二審記錄

### 採納

| # | agy 意見 | 處置 |
|---|---|---|
| 1 | 餵 graveyard IC 給 LLM ＝ 自動化 p-hacking（會套 `log()`/`ewma` 硬逼過門檻） | 採納 → §4.1 只餵死因分類，不餵 IC 數值 |
| 2 | Dedup 死迴圈（生高相關 → 撞 0.7 → 進 graveyard → 成脈絡 → 無限燒錢） | 採納 → §4.2 限制餵給 LLM 的脈絡 |
| 3 | DEAD_CLASSES 靠 LLM 自我舉報（`r.get("dead_classes")`）＝形同虛設 | 採納並一般化 → §5.1（結論是該檢查對 LLM 來源空轉，直接刪） |
| 4 | 兩段式（經濟假說 → 程式碼） | 採納 → §4（但這是免費的，架構本來就兩段） |
| 5 | **`len(set(fields)) >= 2` 會封殺單欄時序因子；「單欄必被 0.7 斃」的推論不成立** | **採納 —— agy 抓到 Claude 的真錯。** 實測（§3.1）證實。硬規則移除 |

### 駁回（讀真碼後，agy 二審已表示同意）

| # | agy 意見 | 駁回理由 |
|---|---|---|
| 1 | Turnover 門檻是股票標準，會誤殺高持久性 crypto 因子 | `gatekeeper.py:182` `max_turnover=0.5` 是**上限**（`if mean_turnover > cfg.max_turnover: reject`），程式碼裡**沒有任何下限**。低換手因子不會被這條擋。agy 講反了 |
| 2 | `KeyError:-1` 證明 sandbox / data loader 對欄位對齊、NaN/Inf 容錯脆弱 | zoo 因子的程式碼**根本沒被執行**。`hypotheses_from_zoo()` 只用 `ast.literal_eval` 抽 `__alpha_meta__` 的 `formula_latex` 當文字描述，LLM 看描述重寫一份新的 `compute(df)`。該 traceback 是 `File "<string>", line 16, in <lambda>` 在 `rolling.apply` 裡，是 LLM 自己寫的 code 對 DatetimeIndex 的 Series 做 `s[-1]`（當 list 用）。是 LLM 寫爛 + forge 修復迴圈 3 次沒修好，不是基礎設施問題 |
| 3 | 餵 graveyard ＝ 讓 LLM 看著全樣本答案考試，摧毀 OOS 純潔性 | `orchestrator.py:307-312` `foundry_split(features_full, oos_start)` 已把 OOS 鎖死，還有第二次 `strict=True` 呼叫當 defence-in-depth（會 raise `OOSLeakError`）。`evaluate()` 只看得到 `oos_start` 之前的列，graveyard 卡上的 IC 全是 pre-oos。多重測試債另由 `foundry_dsr` 處理（從 `research_ledger` 讀同 interval 的歷史 trial 數算 deflated sharpe） |
| 4 | Multi-Armed Bandit 配額調度 | YAGNI，見 §9 |

### Claude 自己補的（agy 兩輪都沒講清楚）

1. **zoo 不只沒用 —— 它把 LLM 預算整碗吃掉**（§1 病灶 3）。這讓 Claude 從「zoo 降權」倒向 agy 的「砍」。
2. **`derived` 來源也是結構性死的**（Q4）。`rank(x)` 對 `x` 嚴格單調 → Spearman 恰好 1.0 → 保證 redundant。
3. **panel 已佔掉「z 分數」和「動能」兩種變換**（§3.2 結論 3）。這是實測 `funding - funding.shift(24)` 撞 `funding_mom` 撞到 1.000 才發現的，直接決定了 prompt 該告訴 LLM 什麼。
