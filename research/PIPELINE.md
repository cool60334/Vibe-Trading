# Research Pipeline 說明

本文件說明 `research/pipeline/` 各階段的功能與執行方式。

## 30 秒看懂整條 pipeline（白話）

目標：從一堆市場資料裡，自動找出「能預測未來漲跌的訊號」，組成策略，回測，挑出最好的。

一條龍流程，分三大塊：

1. **找因子（Stage 0a → 0 → 1）** — 先算一池指標、量出每個指標對「未來報酬」的相關性（叫 **IC**），再讓 AI 依證據挑因子，最後嚴格驗證哪些真的有效。
2. **做策略（Stage 2 → 2b → 2.5）** — 用有效因子寫成買賣規則（YAML），編譯成可執行程式碼，並標記市場處於多頭/空頭/盤整。
3. **回測挑選（Stage 3 → 3-diag → 4 → 5）** — 跑歷史績效、診斷該怎麼改、掃參數調優、最後挑出贏家。

> 💡 **train/OOS 切分（重要）**：設 `oos_start` 後，回測與調參**只用 train 窗**，stage4 調完**自動**用最佳參數在 held-out OOS 跑一次（walk-forward），結果寫進 `manifest.walk_forward`。這才是可信的「換到沒看過的資料還賺不賺」。詳見檔尾「Walk-Forward train/OOS」。

### 幾個關鍵名詞（新手必看）

- **因子（factor）**：一個你覺得能預測漲跌的數字訊號，例如「資金費率」「RSI」。
- **IC（Information Coefficient）**：這個因子今天的值，和「未來 N 小時報酬」的相關係數。|IC| 越大代表預測力越強；正號=同向、負號=反向（contrarian）。一般加密貨幣 |IC|>0.05 就算有料。
- **horizon（時窗）**：往未來看多久（量報酬）。本專案看 8/24/72/168 小時（即 8h ~ 1 週）。
- **interval（K 棒週期）≠ horizon**：`interval` 是 K 棒大小（`research_config.yaml` 預設 `"1H"` = 1 小時 K），horizon 是「往未來看多久量報酬」。兩者獨立。✅ **15m / 30m / 1H 均支援**：所有 hour-anchored 量（forward-return horizon、rolling 窗、IC 步距）透過 `lib/timeframe.py::bars_per_hour()` 換算成 bar 數。設環境變數 `RESEARCH_INTERVAL=30m`（仿 `RESEARCH_ONLY_SYMBOL`）即切到該級別，1H 為預設不需設；產物自動 namespace 進 `research/manifests/<interval>/`（1H 留 root）。見檔尾「多時間級別」。
- **feature store**：算好的因子數值倉庫，存成 `features_<sym>.parquet`。下游階段直接讀它，不用每次重抓資料（省時、可重現）。
- **evidence 表**：`evidence_<sym>.json`，一張「整池因子的 IC 排名表」，是 AI 挑因子時看的證據。
- **verdict（裁決）**：Stage 1 給每個因子的評級——`single_use`（夠強可單用）、`ensemble_only`（弱，只能多因子組合）、淘汰。
- **train / OOS（樣本內/樣本外）**：把資料切兩段——**train（in-sample）**用來選參數，**OOS（out-of-sample, 樣本外）**是 train 之後、選參時**完全沒看過**的期間，用來驗證策略換到新資料還賺不賺。設 `research_config.yaml` 的 `oos_start` 啟用切分。
- **walk-forward（前測）**：在 train 窗調好參數，再丟 held-out OOS 看績效能否維持。`manifest.walk_forward` 存的就是這個——**唯一可信的真·樣本外結果**。

## 各階段概覽

| 階段 | 檔案 | 功能 |
|------|------|------|
| **Stage 0a** | `stage0a_features.py` | **特徵與證據建置**：抓 OHLCV（OKX）+ 非價格資料（funding 走 OKX、**OI + L/S 持倉比走 Binance daily-metrics archive**、**穩定幣供給走 DefiLlama**，皆多年全史）→ 算一池技術指標 + 非價格因子（含持倉因子）→ 存進 feature store；再算每個因子在各 horizon 的 IC，輸出排名表 `evidence_<sym>.json`。**無 LLM，純計算**。 |
| **Stage 0** | `stage0_discovery.py` | **因子探索**：2-agent LLM swarm（研究員提案 + 審查員把關過擬合）讀 evidence 表挑/組因子，寫出 `candidates_<sym>.json`（每個候選帶 `feature_key` 指向 feature store 欄位）。**執行前會 preflight 檢查 Stage 0a 產物存在**。 |
| **Stage 1** | `stage1_factors.py` | **因子評估**：依 candidates 的 `feature_key` 從 feature store 取序列，算 IC/IR/穩定性/verdict（呼叫 `factor_extended` + `factor_regime`）；dump `factor_values_<sym>.parquet` + `factor_<sym>.json`/`.md`。 |
| Stage 2 | `stage2_strategies.py` | 策略合成：deterministic scaffold 依 stage1 verdict 產 `strategy_<id>.yaml`（LLM swarm 只給理由）；**進場方向依因子實測 IC 符號**（正 IC→trend 做高、負 IC→contrarian 做低）；≥3 因子用 `logic: any` |
| Stage 2b | `stage2b_compile_signal.py` | YAML→signal_engine 編譯：Jinja 模板產 `signal_engine.py`，AST 雙驗證 + 自動 smoke test |
| Stage 2.5 | `stage2_5_regime.py` | 市場機制分類：偵測多頭/空頭/盤整等 regime（**價格** regime，供報告用） |
| Stage 3 | `stage3_backtest.py` | 回測：用編譯後 signal_engine 跑歷史績效；讀 parquet 不 fetch OKX。**設 `oos_start` 時 base = train 窗** |
| Stage 3-diag | `stage3_diagnose.py` | 回測診斷：讀 base + stage4 best，輸出 `recommended_action`（proceed / back_to_stage_2 / back_to_stage_4） |
| Stage 4 | `stage4_optimize.py` | 參數優化：deterministic grid sweep。**設 `oos_start` 時只在 train 掃參、掃完自動跑 held-out OOS holdout** 寫入 walk_forward |
| Stage 5 | `stage5_select.py` | 策略挑選：純 Python 加權算分，挑 `recommended_action != back_to_stage_2`，`proceed` 標 selected=True，寫 `selection.json`；完成後**自動為每個策略 emit `manifest.json`**（存至 `research/manifests/<strategy_id>/manifest.json`），dashboard `GET /api/strategies` 即可列出並進行 promote 流程 |

執行順序：`0a → 0 → 1 → 2 → 2b → 2.5 → 3 → 3-diag → 4 → 3-diag → 5`。

> 🔁 **3-diag 跑兩次（重要）**：stage 4 會 gate 在 `diagnosis.json` 存在，所以 diag 必須**先**跑一次；但也是 stage 4 才產出 walk-forward holdout（寫 `walk_forward_runs`），而 3-diag 要**讀得到 holdout 才會 OOS-aware**。因此 stage 4 之後**再跑一次 3-diag**，產出真正以 held-out OOS 為錨的 `recommended_action` 供 stage 5 挑選。少了第二趟，乾淨首跑的 diag 只看 in-sample base 指標（trade gate 50），會把 **OOS 交易數過少**的策略誤判成 `proceed`（實例：BTC s1_regime in-sample 117 筆過關，但 OOS 僅 13 筆應 back_to_stage_4）。手動逐 stage 跑時也要記得 stage 4 後補跑一次 3-diag。

> Stage 5 額外產出：除 `selection.json` 外，還會對 `strategy_runs.json` 裡**全部策略**（含未入選）emit `research/manifests/<strategy_id>/manifest.json`。此為 **derived artifact**，請勿手動編輯——下次 `stage5_select` 跑完會自動覆寫。

> ⚠️ **Stage 0a 必須先跑**：Stage 0 與 Stage 1 都依賴它產出的 feature store 和 evidence 表，缺了會直接報錯。

---

## Stage 2b — YAML→signal_engine 編譯

### 用途

將策略 YAML spec（`StrategySpec` schema）透過 Jinja 模板編譯成 `signal_engine.py`，並在寫入前對產出碼做 AST 語法驗證與 backtest scrubber 檢查。位於 Stage 2（市場機制分類）與 Stage 2.5 之間。

### 用法

```bash
# 編譯特定策略（從 repo root 執行）
python -m research.pipeline.stage2b_compile_signal --strategy <strategy_id>

# 試跑（只顯示產出碼，不寫入檔案）
python -m research.pipeline.stage2b_compile_signal --strategy <strategy_id> --dry-run
```

### Escape hatch — 保護手寫 signal_engine

若 `signal_engine.py` 前 5 行內含 `# manual: do-not-overwrite`，stage 2b 跳過該檔案，不覆寫。

```python
# manual: do-not-overwrite
# 手寫實作，暫不透過 DSL 編譯
```

### DSL 詞彙速查

#### 指標來源格式（`indicators[*].source`）

```
stage1:<factor_key>
# 例：stage1:funding_rate, stage1:oi_change_24h
```

#### 平滑選項（`indicators[*].smoothing`）

| 值 | 說明 |
|---|---|
| `none` | 不平滑 |
| `sma_<n>` | n 期簡單移動平均，例 `sma_3` |
| `ema_<n>` | n 期指數移動平均，例 `ema_5` |

#### 進場條件格式（`entry_long.conditions` / `entry_short.conditions`）

```
<indicator>_percentile_<n>d <op> <value> [persist <m>/<k>]
<indicator>_zscore_<n>d    <op> <value> [persist <m>/<k>]
<indicator>                <op> <value> [persist <m>/<k>]
```

- `<op>`：`<=`、`>=`、`<`、`>`、`==`
- `persist <m>/<k>`：過去 k 根 K 線中至少 m 根滿足條件才觸發
- 例：`funding_rate_percentile_90d <= 20 persist 2/3`

#### 多條件組合（`entry_long.logic` / `entry_short.logic`）

| 值 | 說明 |
|---|---|
| `all`（預設）| AND — 所有條件同時成立才進場（多因子共識；3+ 因子常過稀疏、近 0 交易）|
| `any` | OR — 任一條件成立即進場（每因子各有 edge、但交集太稀疏時用）|

> Stage 2 scaffold ≥3 因子自動用 `any`；進場方向（做高 `>=` 或做低 `<=`）依各因子**實測 IC 符號**決定，不寫死 contrarian。

#### 頂層控制欄位（`StrategySpec` 頂層）

| 欄位 | 型別 | 預設值 | 說明 |
|---|---|---|---|
| `regime_filter` | bool | `false` | `true` 時在編譯出的 signal_engine 插入 regime mask：bear 期間屏蔽多單、bull 期間屏蔽空單 |
| `size_mult` | float (0, 1] | `1.0` | 訊號強度縮放係數；`1.0` = 無縮放；`0.45` 可將倉位縮半（無需動 leverage 設定）|

#### 出場規則類型（`exit_rules[*].condition`）

| `condition` | 必填欄位 | 說明 |
|---|---|---|
| `time_based` | `max_hold_hours: int` | 最多持有 N 小時後強制平倉 |
| `take_profit_pct` | `value: float` | 獲利達 value% 平倉 |
| `stop_loss_pct` | `value: float` | 虧損達 value% 平倉 |
| `signal_invalidation` | `expression: str` | 當指標落入中性區間時平倉，格式：`<indicator>_percentile_<n>d between <lo>,<hi>` |

### 失敗模式

- YAML schema 驗證失敗 → 顯示 Pydantic validation error，中止
- AST 語法錯誤（jinja 渲染後）→ 顯示 SyntaxError，中止
- pytest 測試失敗（若 `--skip-test` 未設定）→ 顯示測試輸出，中止
- 遇 `# manual: do-not-overwrite` → 印出跳過訊息，正常結束

---

## Stage 0a — 特徵與證據建置（Feature & Evidence Build）

### 這階段在做什麼（白話）

把「原始資料」變成「算好的因子數值 + 一張 IC 排名表」，給後面的 AI 當證據。整段**不用 LLM**，純 Python 計算，所以快、便宜、可重現。

做三件事：
1. 抓資料：OHLCV K 線（OKX）+ 非價格資料（資金費率 OKX、**OI + L/S 持倉比 Binance daily-metrics archive**、**穩定幣供給 DefiLlama**）。
2. 算因子：一池技術指標（RSI、MACD、ATR、布林帶寬…）+ 非價格因子，全部存進 feature store（`features_<sym>.parquet`）。
3. 量證據：算每個因子對未來 8/24/72/168h 報酬的 **IC**，依 |IC| 由大到小排序，寫出 `evidence_<sym>.json`。

> **穩定幣資料源 = DefiLlama**（`research/lib/defillama_data.py`，endpoint `stablecoins.llama.fi/stablecoincharts/all`，免費無 key、2017 至今全史、聚合所有 USD 穩定幣）。早期用 CoinGecko 免費版卡 365 天 → 因子只 1 年覆蓋、IC 被牛市灌水；換 DefiLlama 後 BTC stablecoin_supply_z 覆蓋率 25%→99.9%，真 4yr IC 從假象 +0.104 降到誠實 +0.068。

> **OI / 持倉資料源 = Binance daily-metrics archive**（`research/lib/oi_metrics.py` + `lib/binance_dump.py`，`data.binance.vision futures/um/daily/metrics`，免費無 key、**5 分鐘粒度**、BTC ~5.8yr / ETH·SOL ~4.5yr）。解掉舊 Bybit API 7 天保留上限。白送 4 個持倉因子：`global_ls_acct_z`（散戶多空比 z）、`toptrader_ls_z`（大戶多空比 z）、`ls_divergence`（兩者背離）、`taker_buysell_ratio`（taker 買賣失衡）。**Bybit OI 仍作為 fallback**（`fetch_oi_history_bybit`）。live 部署的新鮮度由 `dump_oi --live` 端點覆蓋（archive 為 T+1）。持倉因子隨幣異：`global_ls` 強 BTC/SOL 弱 ETH、`ls_divergence` 三幣皆強（SOL −0.10 single_use）。

### 用法

```bash
# 從 repo root 執行（先跑這個，再跑 Stage 0）
python -m research.pipeline.stage0a_features
```

### 輸出（都在 `research/manifests/`）

| 檔案 | 內容 |
|------|------|
| `features_<sym>.parquet` | feature store：所有因子的時間序列數值 |
| `features_<sym>.meta.json` | 中繼資料：schema 版本、欄名、時間範圍、列數 |
| `evidence_<sym>.json` | IC 排名表：每個 `feature_key` 的 `ic_by_horizon` / `ir` / 樣本數，附 multiple-testing 警語 |

### 注意

- evidence 表只是「篩選參考」，**不是最終裁決**。真正的把關靠 Stage 0 的審查員 + Stage 1 的 verdict gate。
- 大指標池容易出現 IC 膨脹的偽訊號（multiple-testing），evidence 檔內含 caveat 提醒。

---

## Stage 0 — 因子探索（Factor Discovery，證據驅動）

### 這階段在做什麼（白話）

依 Stage 0a 的 evidence 表自動挑出符合 IC/IR 門檻的因子，輸出 `candidates_<sym>.json` 供 Stage 1 評估。**預設為 deterministic 模式（無 LLM）**，靠純 Python 規則挑選；可選 `--use-swarm` 啟用 LLM 替每個因子補經濟邏輯散文。

> **設計變更紀錄**：原本 LLM swarm 主導 / deterministic 為 fallback 的模式（swarm 偶爾不吐 ```json fence 而整條 pipeline 掛）已翻轉為 deterministic 主、swarm enrichment 副。見 `openspec/changes/stage0-deterministic-discovery/`。

### 用法

```bash
# 預設：deterministic only（不呼叫 LLM）
python -m research.pipeline.stage0_discovery

# 強制重跑（忽略快取）
python -m research.pipeline.stage0_discovery --force
# 或
RESEARCH_FORCE_DISCOVERY=1 python -m research.pipeline.stage0_discovery

# 啟用 LLM swarm 為每個因子補 economic_logic 散文（失敗會 fail-soft）
python -m research.pipeline.stage0_discovery --use-swarm
# 或
RESEARCH_STAGE0_USE_SWARM=1 python -m research.pipeline.stage0_discovery

# 顯式關閉 swarm（與預設等效）
python -m research.pipeline.stage0_discovery --no-swarm
```

### Deterministic 挑選規則

`research/pipeline/lib/factor_selector.py::select_candidates_from_evidence` 對每個 evidence entry 依序執行：

1. 算 `top_horizon = argmax_h |IC[h]|`、`top_abs_ic = |IC[top_h]|`。
2. 過 `top_abs_ic ≥ min_abs_ic` 且 `|IR| ≥ min_abs_ir`。
3. 過 `feature_key` 必須在 feature store parquet 中存在。
4. 依 `top_abs_ic` 降冪取前 `max_candidates`。
5. 每個 candidate 自動帶上：`expected_ic_sign` (sign of IC@top)、`horizons_h` (top + 同向鄰近 horizons)、placeholder `economic_logic`、category。

### 門檻參數（可調）

在 `research_config.yaml` 加 `stage0_selector` 區塊覆寫預設：

```yaml
stage0_selector:
  min_abs_ic: 0.05      # 預設；最小 |IC| 過門檻
  min_abs_ir: 0.10      # 預設；最小 |IR| 過門檻
  max_candidates: 6     # 預設；最多挑幾個
```

不寫此區塊則用上述預設。

### Preflight 檢查

Stage 0 開跑前會檢查 `features_<sym>.parquet` 和 `evidence_<sym>.json` 是否存在，缺了直接報錯並要你先跑 Stage 0a。

### 輸出

對每個 symbol 產出 `research/manifests/candidates_<sym>.json`，格式符合 `CandidatesManifest` schema（定義於 `dashboard/server/schemas.py`）。每個候選含：
`feature_key`（指向 feature store 欄名）、`name`、`formula`、`expected_ic_sign`（+/−/?）、`economic_logic`、`horizons_h`、`category`。

### 快取

`research_config.yaml` 的 `discovery_cache_days`（預設 7）控制重跑間隔。若 `candidates_<sym>.json` 的 `generated_at` 在此期間內，stage 0 跳過挑選。設為 0 或加 `--force` 則停用快取。

### Swarm enrichment 模式（`--use-swarm`）

在 deterministic 挑完因子後，呼叫 `crypto_factor_lab` swarm 為每個 candidate 重寫 `economic_logic` 散文（其他結構欄位不變）。**fail-soft**：swarm 子程序失敗 / 超時 / JSON 解析失敗 / 部分 match 失敗，都會保留 deterministic placeholder 文字，pipeline 繼續、exit code 仍 0。

### 失敗行為

Stage 0 已**無「失敗」概念**（除非 0a 產物缺失）：

- 0a 產物齊備 → 永遠寫合法 `candidates_<sym>.json`、exit code 0
- 0 因子過門檻 → 寫空 candidates 陣列 + 紅字警告，exit code 仍 0
- 0a 產物缺失 → 該 symbol 標 FAIL、exit code 1、**不寫** candidates 也**不寫** failed.json

> 註：原本的 `candidates_<sym>.failed.json` 寫出邏輯已移除（stage 0 不再產生）；Stage 1 仍偵測歷史檔案存在做向後相容。`RESEARCH_LEGACY_FACTORS` 環境變數行為改為 no-op（讀但不行動）。

---

## Stage 2 — 策略合成（Strategy Synthesis）

### 這階段在做什麼（白話）

把 Stage 1 存活的因子組成可回測的買賣規則，透過**archetype 工廠**確定性地產生。

**關鍵變更**：Stage 2 現已改為 **archetype-driven scaffolder**（取代 stage 2-old 手寫 YAML）。三種 archetype 可選：`single_factor`（單因子）、`trend_with_gate`（趨勢+閘控因子）、`consensus_all`（全因子共識多數決）。每個 symbol 最多產 3 個 archetype plan，各自生成獨立的 `strategy_<coin>_s<seq>_<archetype>.yaml` + `generation.json`。LLM swarm（`crypto_trading_desk`）僅提供經濟邏輯散文，掛了也不影響 YAML 生成（fail-soft）。

### Archetype 選擇

`research/pipeline/lib/archetype_router.py::pick_archetypes()` 依 candidates 個數確定性挑選：
- **≤2 因子** → `single_factor`
- **2~4 因子** → `trend_with_gate`（主因子 + gate 因子）
- **≥3 因子** → `consensus_all`（多因子 OR）

### Fan-out 與策略 ID

每個 archetype 產一份完整策略 YAML 與 `generation.json`。策略 ID = `<coin>_s<seq>_<archetype>`（例 `btc_s1_single_factor`、`eth_s2_trend_with_gate`）。

此外，`_generate_for_symbol_multi` 對**每個 archetype** 額外 fan-out 一份 `_regime` 變體：ID 後綴 `_regime`（例 `btc_s1_single_factor_regime`），自動設 `regime_filter: true`，並同步在 `strategy_runs.json` 中註冊。此變體可直接進 stage 2b 編譯，無需手動建檔。

### 進場方向（重要）

每個因子的進場方向**依其實測 IC 符號**決定（不寫死 contrarian）：
- **正 IC → trend**：因子值高時做多（`>= 80`）、低時做空（`<= 20`）
- **負 IC → contrarian**：因子值低時做多（`<= 20`）、高時做空（`>= 80`）

### Auto-registration

每個產出的策略自動在 `strategy_runs.json` 註冊（透過 `register_strategy()`，冪等、累加）。不需手動建檔。

### Swarm enrichment（opt-in）

預設路徑為 **swarm-free deterministic**。加 `--use-swarm` 或設 `RESEARCH_STAGE2_USE_SWARM=1` 開啟 LLM enrichment，fail-soft 掛時保留 placeholder。

### 用法
```bash
# 預設：deterministic 無 swarm
python -m research.pipeline.stage2_strategies

# 啟用 swarm enrichment（fail-soft）
python -m research.pipeline.stage2_strategies --use-swarm
# 或
RESEARCH_STAGE2_USE_SWARM=1 python -m research.pipeline.stage2_strategies
```

---

## Stage 3 — 回測（Backtest）

### 用法
```bash
python -m research.pipeline.stage3_backtest
```

### 跑哪些 run
讀 `research/strategy_runs.json`，每策略跑：`base_run`（主回測）、`regime_runs`（bull/bear/neutral 切片）。透過 subprocess 呼叫 `python -m backtest.runner <run_dir>`，產物寫 `runs/<run>/artifacts/`（metrics.csv / equity.csv / trades.csv …）。

### train/OOS 行為
- **未設 `oos_start`**：base = 全期（legacy）。
- **設 `oos_start`**：base = **train 窗** `[start, oos_start)`，regime 切片也夾在 train 內 → in-sample 不偷看 OOS。真 OOS 由 Stage 4 的 holdout 負責。

### Fail-fast guard（架構貼合檢驗）

Base run 完成後，檢驗策略是否「適合此 archetype」。若：
- **sharpe < -2** OR
- **trades_per_year > 1000**

→ 策略標記 `archetype_misfit`，寫 sentinel `manifests/<id>/archetype_misfit.json`；regime / OOS runs 全部 SKIP。

**作用**：早期偵測 archetype 選錯，不浪費時間跑 stage 4 參數掃描。Stage 4 在消費 optimization.json 時會檢查此 sentinel，misfit 策略直接跳過 sweep。

> ⚠️ base 失敗時舊 artifacts 還在會被誤判 PASS（同 stage0）；要重跑先刪 `runs/<run>/`。

---

## Stage 3-diag — 回測診斷

讀 base run（train 窗）+ Stage 4 best（`optimization.json`）+ **walk-forward holdout 指標**（`manifest.walk_forward`）做概念層路由，吐 `recommended_action`：

> 🔁 **OOS-aware 需在 stage 4 之後跑**：diag 的 walk-forward 指標來自 `strategy_runs.json` 的 `walk_forward_runs`，那是 **stage 4** 才寫入的 holdout run。所以 pipeline 在 stage 4 後**再跑一次 3-diag**（見上方執行順序）；stage 4 之前的第一趟 diag 只為滿足 stage 4 的 `diagnosis.json` gate，其 verdict 會被第二趟覆寫。
- `proceed`：概念成立，可進 Stage 5。
- `back_to_stage_4`：有潛力但要調參。
- `back_to_stage_2`：概念有缺陷（如 OOS 負 sharpe），回去重做策略。

### 三層輸入（OOS-first）

當 `oos_start` 已設且 stage 4 已跑完 walk-forward holdout，stage 3-diag 會把三段指標都餵給 LLM，並明示「Walk-Forward 是 authoritative 真樣本外結果」：

1. **TRAIN（in-sample）**：base run 指標；僅用於量 overfit gap（train 對比 OOS）。
2. **STAGE4-BEST**：掃參完的 in-sample 最佳組；同樣只是 train-side 證據。
3. **WALK-FORWARD（held-out OOS）**：用 stage 4 best params 在 `[oos_start, today]` 重跑的指標；**這才是最終裁決依據**。

當 `walk_forward` 不存在（未設 `oos_start` 或 holdout 未產生）→ prompt 結構回退到舊的 train-only 兩段式，行為與改動前一致。

### Deterministic fallback（LLM 失敗時）

LLM 路線掛掉時，`rule_based_action` 會看到 wf metrics 就以 OOS 為錨：

- `wf.sharpe < 0` → `back_to_stage_2`
- `wf.sharpe < 1.0` OR `|wf.dd| > 0.15` OR `wf.trade_count < 30` → `back_to_stage_4`
- 其餘 → `proceed`

OOS trade gate 設 30（vs train-only 路徑的 50），因為 holdout 樣本本來就短。

### 舊 vs 新 決策入口對比

| 面向 | 舊（train-only） | 新（OOS-first） |
|------|---------------|----------------|
| Sharpe 依據 | base run（train 窗）| walk-forward（held-out OOS） |
| Drawdown 依據 | base run | walk-forward |
| Trade count 門檻 | 50（train） | 30（OOS）|
| Stage 4 best 角色 | 主要參考 | 輔助、僅作 in-sample 上限參考 |
| Train 指標角色 | 主要依據 | 僅用於 overfit gap 偵測 |
| Walk-forward 角色 | 未讀 | authoritative |
| LLM prompt 結構 | 2 段（train + stage4-best）| 3 段（train + stage4-best + walk-forward，最後一段標 authoritative）|

### 寫進 diagnosis.json

新增兩個欄位（皆 optional，向後相容舊檔）：
- `walk_forward_source`：用了哪個 run dir 取 wf metrics（debug 用）
- `walk_forward_metrics`：拿到的 sharpe / max_drawdown / trade_count 等原始值

### 用 LLM（`vibe-trading run`）

⚠️ 會讀 `optimization.json` + `manifest.walk_forward`，若 stage 4 舊未更新會誤導；重跑前確認 stage4 已產出最新 walk-forward。

```bash
python -m research.pipeline.stage3_diagnose
```

---

## Stage 4 — 參數優化（Deterministic Grid Sweep）

### 這階段在做什麼
把 YAML 的 `parameter_search_ranges` 展開成參數網格，每組跑一次回測，依 sharpe（過 trade_count 門檻）排名，挑最佳，寫 `optimization.json`（`best_params` + top-5 摘要）。**純確定性、無 LLM**（早期 LLM 版回空 dict 燒 445k token，已廢）。

### train/OOS（walk-forward）
- **未設 `oos_start` 且無 `--train-start`**：在全期掃參（legacy；此時「OOS」是假的）。
- **設 `oos_start`**：自動只在 **train 窗**掃參 → 挑 best → **自動用 best 在 held-out OOS 窗跑一次** holdout run → 寫進 `manifest.walk_forward`。這就是真·樣本外驗證。
- 手動覆寫：`--train-start / --train-end`。

### size_mult 掃描範圍

`_default_spec_scaffold` 預設在 `parameter_search_ranges` 加入：

```yaml
size_mult: [0.4, 1.0, 0.2]   # 從 0.4 到 1.0 步距 0.2，即 [0.4, 0.6, 0.8, 1.0]
```

stage 4 掃參時會同步展開此維度，自動找出適合的倉位縮放係數，無需手動設定。

### 前置
需先有 `diagnosis.json`（Stage 3-diag 產），否則 SKIP/FAIL。順序：stage3 → stage3-diag → stage4。

```bash
python -m research.pipeline.stage4_optimize --strategy <id> --max 200
# 手動 walk-forward 窗：
python -m research.pipeline.stage4_optimize --strategy <id> --train-start 2022-06-01 --train-end 2025-01-01
```

---

## Stage 5 — 策略挑選（Selection）

純 Python 加權算分（無 LLM），挑出可進 testnet 的策略，寫 `selection.json`；完成後自動 emit per-strategy `manifest.json`。

- **資格**：要有 diagnosis + optimization + metrics，且 `recommended_action != back_to_stage_2`。
- **selected**：需同時滿足三條件 → `recommended_action == proceed` **且** gate `fatal_fail == False` **且** `validations_ok`（必要驗證存在且通過：cost-stress 一律必要，CPCV 於 sweep grid 組合數 >1 時必要，經 `emit_manifest.py::required_validations_ok` 判定）→ `selected=True`；`back_to_stage_4`、或 `proceed` 但 `fatal_fail==True`（如 fee-illusion／無 OOS holdout）、或必要驗證缺失/未過（`gate.not_tested` 非空或驗證未通過）→ 入榜但 `selected=False`。selected 與 dashboard promote gate 一致：`fatal_fail` 或 NOT_TESTED 的策略絕不標 selected（見 `decide_selected`）。
- **評分**：`0.4×(sharpe/1.5) + 0.3×(1−|dd|/0.10) + 0.2×(pf/1.5) + 0.1×(trades/100)`（各項 clamp 0~2）。

```bash
python -m research.pipeline.stage5_select
```

### manifest.json 自動 emit

stage 5 寫完 `selection.json` 後，會遍歷 `strategy_runs.json` 裡**所有策略**（含未入選），各呼叫 `emit_manifest_for_strategy` 把完整 `StrategyManifest` 寫進 `research/manifests/<strategy_id>/manifest.json`。dashboard 的 `GET /api/strategies` 讀這批 manifest.json 來列出策略、提供 promote 流程入口。

> ⚠️ `manifest.json` 是 **derived artifact**，請勿手動編輯——下次 `stage5_select` 跑完會自動覆寫。如需調整策略資訊，應修改 `strategy_runs.json` 或策略 YAML，再重跑 stage 5。

> 之後 → dashboard promote → testnet（`POST /api/strategies/<id>/promote` → `/api/testnet/<id>/start`）。

> **部署注意（regime_filter）**：編譯時 `regime_filter: true` 的 signal_engine 在執行期會讀取 `research/manifests/regime_<sym>.json`。部署或拉新版時，請把此 regime 檔連同 feature parquet 一起同步，否則 regime mask 會用到舊資料。

### Gate — OOS-aware 判斷規則（`emit_manifest.py::compute_gate`）

dashboard 的 promote 按鈕靠 `gate.fatal_fail` 決定是否解鎖。2026-06-04 更新後，gate 改為**OOS 優先評估**：

| 情況 | eval_metrics | min_sharpe 門檻 | min_trades 門檻 |
|---|---|---|---|
| `backtest.oos` 有值（walk-forward） | OOS 指標 | 1.0（`GATE_MIN_WALK_FORWARD_SHARPE`） | 30（`GATE_OOS_MIN_TRADES`） |
| `backtest.oos == None`（舊式 legacy） | in-sample 指標 | 1.5（`GATE_MIN_SHARPE`） | 100（`GATE_MIN_TRADES`） |

`max_drawdown`（≤ 0.10）與 `min_profit_factor`（≥ 1.5）門檻不論模式不變。

**OOS 資料來源（`build_backtest_block`）**：優先用 `oos_runs[0]`；若 `oos_runs` 為空則 fallback `walk_forward_runs[0]`；兩者皆空 → `oos = None`（legacy 路徑）。

**`alpha_not_fee_illusion` 行為**：
- 有 stress 資料（`stress_runs` 非空）→ `fatal=True`；worst sharpe ≤ 0 → hard block。
- 無 stress 資料 → `fatal=False`；`passed=False`（informational warning）；**不設 `fatal_fail`**。

---

## Walk-Forward train/OOS（核心：可信回測）

### 為什麼
若在全部資料上選參，再拿同一段資料的子集當「OOS」= 循環驗證、會高估。正確做法是切 train / 真 held-out OOS。

### 怎麼設
`research_config.yaml`：
```yaml
oos_start: "2025-01-01"   # 之前 = train（選參）、之後 = held-out OOS（驗證）；留空 = 不切分
```

### 時間軸
```
period_start ─────────────── oos_start ─────────────── today
            [   in-sample TRAIN   ]   [   held-out OOS   ]
             ↑ Stage3 base 回測         ↑ Stage4 自動 holdout
             ↑ Stage4 掃參               （manifest.walk_forward）
             ↑ Stage3-diag 判讀
```

### 規則
- **in-sample（train）**：base 回測 + regime 切片 + 掃參，全部止於 `oos_start`，**不碰 OOS**。Stage 3-diag 仍會讀 train 指標，但只當作 overfit gap 偵測用，不作為主要裁決。
- **真 OOS**：只有 `manifest.backtest.walk_forward` 是真樣本外。`oos_runs` 已於 2026-06-11 移除（B4）——`walk_forward` 是唯一 OOS 來源。**Stage 3-diag 現在也直接消費 walk-forward 指標**作為 LLM prompt 與 deterministic fallback 的最終裁決依據。
- **解讀**：train 調得好、held-out 也撐住 = 真 edge；train 好但 held-out 崩 = overfit。例 BTC s2：train sharpe ~1.5 → held-out 2025 僅 0.27 = edge 在 2025 退潮（真相，非藏起來）。

### 自動流程
設好 `oos_start` 後，照常跑 `stage3 → stage3-diag → stage4`，walk-forward 全自動，無需手動切窗。

---

## 多時間級別（15m / 30m）✅ 已實作

15m / 30m / 1H 全支援。所有 hour-anchored 量（forward-return horizon、funding/stablecoin 的 rolling 窗、IC 步距）透過 `lib/timeframe.py::bars_per_hour()`（`{15m:4, 30m:2, 1H:1}`）換算成 bar 數；stage0a `build_feature_dict`、stage1 `factor_extended` / `factor_regime`、stage3 backtest 全串 `cfg.interval`。

### 怎麼切級別

設環境變數 `RESEARCH_INTERVAL`（仿 `RESEARCH_ONLY_SYMBOL`）：

```bash
# 跑 ETH @ 30m 的 stage0a（1H 為預設，不設此變數即可）
RESEARCH_INTERVAL=30m RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.stage0a_features
```

- 合法值：`15m` / `30m` / `1H`（`SUPPORTED_INTERVALS`）。
- 產物自動 namespace 進 `research/manifests/<interval>/`（1H 留 root，零回歸）。
- dashboard 亦可依 interval 檢視（nav 級別選擇器、`?interval=` query、pipeline RunBar 級別下拉）。

### 結論（已驗）

- **盤中純 OHLCV 因子類 = 死路**：mom_4/8/16 等短線動量 IC≈−0.05 是 bid-ask bounce 微結構假象，非真 alpha。
- **慢的非價格因子塞日內無用**：funding（8h）、穩定幣（日頻）在 15m/30m 幾乎沒新資訊。
- **未試的免費角度**：Binance daily-metrics archive 是 **5 分鐘**粒度 → 可衍生盤中 OI / L/S 持倉 / taker 失衡因子（+ 時段效應）。盤中因子**務必過 entry-lag / lookahead 稽核**（order-flow 教訓）。
