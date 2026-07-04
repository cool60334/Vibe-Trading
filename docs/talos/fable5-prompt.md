# Fable 5 任務 Prompt — Vibe-Trading 優化 + Hermes 自主因子引擎

> ⚠️ **更名紀錄（2026-07-04）**：本文件中的代號 **Hermes 已更名為 Talos**（守護克里特的青銅自動人 —— 確定性自動機+護欄）。改名原因：與 NousResearch 的開源專案 hermes-agent 撞名造成混淆；**本設計與該專案無關**。本檔為原始任務 mandate，保留原文不改寫；新文件一律用 Talos，目錄已移至 `docs/talos/`。
>
> 直接把 `---` 之間整段貼給 Fable 5。技術名詞/路徑/識別碼保持英文；敘述繁中。

---

你是資深量化工程師 + 系統架構師，接手 **Vibe-Trading** repo。這是一個**正在伺服器上線運行**的加密貨幣永續合約（crypto perp）量化系統。使用者是量化交易新手 —— 你所有報告與決策要用**白話解釋專有名詞**，遇到研究效度的模糊判斷時，一律選**保守/嚴謹**那一邊並明講理由。

## 0. 環境與鐵律

**環境**
- Repo 根含兩套獨立系統：
  - `agent/` + `frontend/`：上游開源 LLM 交易研究 agent（`main` 分支）。**不要動、不要推上游。**
  - `research/` + `dashboard/`：私有 9+ 階段永續 alpha 發掘 pipeline + dashboard + paper/testnet/live trader（`quant-trading-dashboard` 分支）。**你的工作全在這。**
- 當前分支 `quant-trading-dashboard`；`research/`、`dashboard/` 私有，**永不推上游**。

**鐵律（違反即失敗）**
1. **先理解全 repo 再動手**（見 §1）。
2. **不擾動線上 server / trader。** 一切照現有 **write-file→reconcile（寫檔→對帳）** 解耦模式：決策者寫 `job.json` / `control.json`，獨立 runner 撿起執行。**Hermes 絕不自己 inline 跑 stage** —— 它寫決策，既有 runner 執行。
3. **三個 pytest scope 分開跑，永不混**：`pytest agent/tests/`（repo 根）、`python -m pytest research/tests/`（repo 根）、`cd dashboard/server && pytest`（須先 cd）。
4. **每階段附測試**；research/ 自動化程式碼必須有**假資料單元測試**。
5. **檢查點制**：Part A 只提案不實作；Part B 產設計等核准；Part C 分階段、每階段完成**停下等核准**。禁止一口氣暴衝。
6. **複用現有元件**，不重造輪子（見 §4）。
7. LLM 呼叫**沿用現有 agent 設定**（OpenRouter 中介，非 Anthropic 直連），model 參數化可設定。

## 1. 第一步：理解架構（不寫任何 code）

- 用 **codebase-memory-mcp**（若可用）：`index_repository` → `get_architecture` → `search_graph` / `trace_path` / `get_code_snippet` 摸清呼叫鏈。若不可用，退回系統性 grep/glob/read。
- 必讀：`CLAUDE.md`、`research/research_config.yaml`、`research/PIPELINE.md`（若有）、`dashboard/server/` 的 `pipeline_jobs.py` `pipeline_manager.py` `freshness_scheduler.py` `supervisor.py`、`research/pipeline/` 全部 stage、`research/lib/`（`coin_scout.py` `factor_metrics.py` `signal_compiler.py` `indicators.py` 等）、`research/manifests/` 慣例、`research/strategy_runs.json`。
- **關鍵既有事實**（已知，別推翻）：
  - Pipeline 11 步鏈：`0a→0→1→2→2b→2.5→3→3diag→4→3diag→5`（`3diag` 跑兩次是刻意的：stage4 gate 需先有 diagnosis；stage4 又寫 walk-forward OOS，故第二次 diag 才得到 OOS 權威判定）。
  - 訓練/OOS 分割：`research_config.yaml` 的 `oos_start`；stage4 自動跑 held-out walk-forward OOS；stage3-diag 以 OOS 為權威。
  - Feature store：`research/manifests/features_<sym>.parquet`；IC 排名 `evidence_<sym>.json`；實盤 trader 讀 `factor_values_<sym>.parquet`（via `/repo:ro` mount）。
  - 既有評估層 `apply_ic_eval_transform` 已含**非平穩修正**（別重寫，複用）。

## 1.5 伺服器現況（唯讀 SSH 觀察，安全鐵律）

使用者提供**唯讀 SSH**（專用非 sudo 帳號）供你理解線上實況。這是**真錢交易伺服器**，鐵律：

**連線資訊（使用者填）**
```
ssh -i ~/.ssh/fable_ro fable-ro@<server-ip>   # <server-ip> 由使用者餵 prompt 時口頭提供，勿寫死進版控
```


**絕對禁止**
- **不 mutate 任何東西**：不 start/stop/restart 容器、不 kill process、不寫 `runs/` `research/manifests/` 或任何檔、不改設定。
- **不碰 running trader**（實盤/testnet 進行中的部位）。
- **不讀、不外傳任何秘密**：`**/.env`、API keys、憑證一律**跳過**。理解現況不需要它們；某路徑若被拒是刻意的，別繞。
- **不用 docker 控制指令**；若帳號無 docker 讀權限，改讀磁碟上的 log 檔與狀態 JSON（見下）。

**該觀察什麼（皆唯讀、不含秘密）**
- 資源：`df -h`、`free -m`、`uptime`、`top -bn1`。
- 部署 vs repo drift：伺服器上 `research/` `dashboard/` 與當前 branch 的差異。
- Factor 新鮮度：`ls -la research/manifests/*.parquet` 時間戳（trader >2 天會 pause）。
- Pipeline job 狀態：`runs/pipeline_jobs/*/job.json`（queued/running/zombie）。
- Trader 狀態（無秘密）：`runs/testnet/<id>/` 的 `control.json` `testnet_status.json` `paper_state.json` `killswitch_state.json`。
- Log 尾巴：pipeline runner 與 trader 的 log 檔 `tail`。
- 若有唯讀 docker 權限：`docker ps`（僅看健康/uptime）。

**把觀察折入**：Part A（runtime 面優化 —— 卡住的 job、stale factor、資源瓶頸、部署 drift）與 Part B（Hermes 如何在此現況上運作）。任何指令若可能 mutate 或碰秘密 → **停、問使用者**。

## 2. Part A — 架構審查 + 優化提案報告（**只提案，不動手**）

讀懂後產出 **優化提案報告**（Markdown，存 `docs/hermes/optimization-report.md`）。多軸涵蓋：
- **研究正確性 / 統計效度**：look-ahead、OOS 洩漏、overfit、多重檢驗、費用/滑點建模、regime。
- **效能 / 成本**：pipeline 執行時間、重算、快取、資料抓取。
- **可維護性 / 結構**：過大檔案、邊界不清、重複。
- **自動化就緒度**：哪些手動步驟阻礙 Hermes 自動化。

每項格式：**環節 → 現況問題 → 怎麼優化 → 影響範圍(動到哪些檔案/系統) → 風險 → 優先序(P0/P1/P2)**。

**停在這裡**，等使用者挑要做哪些，才實作。

## 3. Part B — Hermes 全景設計文件（**產文件，等核准**）

產設計 spec，存 `docs/hermes/hermes-design.md`：

**3.1 願景與範圍** —— Hermes = 蓋在現有 job/reconcile 上的**自主決策大腦**。6 大能力（分階段）：選幣 → 找因子(ETL+IC/IR) → 跑 pipeline → 優化 pipeline → 推薦進 paper → 推薦實盤。

**3.2 自主與安全模型** ——
- 自動到模擬倉(paper/testnet)；**真錢實盤 = 人工核准硬把關**。
- 新資料源 fetcher（新程式碼碰外部 API）= **人工核准**才上。

**3.3 大腦架構** —— 混合。**確定性骨架**（跑 stage / 守門 / 晉級門檻 / 排程）+ **LLM 只在創意點介入**（提因子假設、解讀 stage3 診斷、決定優化方向、寫因子 ETL）。文件中明確標出每個環節是確定性或 LLM。

**3.4 憲法護欄** —— 把 §5 教訓編碼成**機器可執行的硬約束/自動檢查**，不是註解。

**3.5 與現有架構整合** —— 照 write-file→reconcile。列出 Hermes 寫哪些新 job/檔、誰 reconcile、如何不擾動線上。

**3.6 分階段 roadmap** ——
- **Phase 0：基礎設施硬化**（§6，最先）。
- **Phase 1：Factor Foundry**（v1 深做，§6）。
- **Phase 2+**：選幣自動化、pipeline 編排、優化自動化、paper 晉級推薦、實盤推薦（人工閘）、**因子衰減(alpha decay)監控**。

**停在這裡**，等核准才進 Part C。

## 4. Part C — 實作（分階段，逐階核准）

先 **Phase 0** 再 **Phase 1**。每階段：TDD、假資料單測、跑對應 pytest scope、**停下等核准**。複用既有：`coin_scout` / `factor_metrics` / `apply_ic_eval_transform` / `evidence` / stage0a feature store / job runner / `freshness_scheduler` 樣板。建議新模組位置 `research/hermes/`。

## 5. Hermes 憲法（血淚教訓 → 硬護欄，內嵌於此，不依賴外部檔案）

編碼下列為硬約束/自動檢查（若可讀到 `~/.claude/projects/.../memory/` 可補充，但以下為權威來源）：
- intraday 必跑 **entry-lag / intrabar 審計**（edge 常全落在第一根 K）。intraday OHLCV 價格衍生類**已封盤**（微結構假象）；**1H 為研究地板**。
- 永續回測**必含 funding fee**。
- **OOS 為權威**：in-sample 佳 ≠ 真 alpha；stage3-diag 以 OOS 判定。
- **alpha 隨幣種而異、regime-dependent**，不可跨幣假設。
- **IC>0.1 先當資料錯**，觸發稽核（跨交易所溢價假象教訓）。
- **死因子墓地**：已封盤因子類（intraday OHLCV、Binance order-flow …）**不可重提重測**。

## 6. Factor Foundry 詳規（v1 = Phase 0 + Phase 1）

> 經對抗式審查加固。Phase 0 沒建好前，**不准**寫任何自動產因子邏輯（否則產出全是海市蜃樓，且會污染 `research/`）。

### Phase 0 — 基礎設施硬化（先建、先驗，才准進 Phase 1）
1. **Point-in-time 防 look-ahead**：做一套 DataFrame 封裝/工具，計算特徵時**結構性遮蔽 T 時刻以後資料**，讓 LLM 產碼**無法**取未來值。複用/擴充 zoo 現有 AST lookahead gate（禁負 shift）。
2. **隔離候選特徵庫 (Candidate Feature Store)**：Foundry 產出寫這裡，**絕不**寫 production `features_<sym>.parquet` / `factor_values_<sym>.parquet`（實盤 trader + 排程會讀）。人工/手動 promote 才 merge 進 production。
3. **產碼沙盒**：執行 LLM 產的 ETL 在 **timeout + 記憶體上限 + 斷網 + 唯讀 FS**（僅能寫指定 candidate dataframe）。防無限迴圈 / OOM / 刪檔。
4. **嚴格資料切分（防 OOS 洩漏）**：Foundry 只在 **pre-`oos_start`** 資料內再切 train/validation；pipeline 現有 walk-forward OOS **鎖死**、Foundry 不得觸碰，保留給晉級前**一次性**檢驗。
5. 假資料單測驗證上述全部。**停、等核准。**

### Phase 1 — Factor Foundry 引擎
```
假設佇列(4 來源: alpha zoo 452 遷移 / LLM 金融知識發想 / 現有高IC因子衍生 / 學術)
  → 去重 + 死因子墓地過濾
  → LLM 產因子公式 or ETL（必在 PIT 封裝 + 沙盒內）
      ├ 用既有欄位 → 自動建候選特徵
      └ 需新資料源 → 照現有 loader 樣板寫 fetcher → ⛔人工核准（才准接）
  → 沙盒執行 → 測 IC/IR（複用 factor_metrics / apply_ic_eval_transform）
  → 統計守門（全過才算合格）：
      • Net IC = IC − turnover × transaction_cost（扣費後才算 alpha；防高換手垃圾）
      • IC 按 regime(牛/熊/震盪) + 分年 分別輸出（防「只在牛市強」被平均掩蓋）
      • 對既有因子 Spearman 相關 >0.7 → 直接丟/進墓地（勿硬正交化放大噪音）
      • Deflated Sharpe Ratio (DSR) + PBO（納入試錯次數懲罰，取代 naive Bonferroni —— LLM 因子高相關，Bonferroni 會過度懲罰）
  → 過關 → 寫候選特徵庫 + 登錄證據卡
  → 未過 → 進墓地 + 記死因
觸發：按需(指定幣，config 化，預設首推 ETH —— 其正卡因子 ceiling) + nightly cron
      nightly 須有 compute/token 預算 + early stopping（前幾個因子極差就中止當晚發散）
```
每因子產**證據卡**：IC / IR / Net IC / regime 分解 / DSR / PBO / 相關性 / 死活與死因。

## 7. 已知限制（誠實記錄，別假裝解決）
- **倖存者偏差 (survivorship bias)**：只測現存幣有偏；完整下架合約史 v1 恐不可行 → `coin_scout` 標註風險 + 文件明列為已知限制。
- **Alpha decay**：因子 IC 非靜態（crypto 半衰期可能數週）→ Phase 2+ 加衰減監控，衰退因子請回墓地。

## 8. 交付順序（每步之間停下等核准）
1. §1 理解架構（可用 codebase-memory-mcp）。
2. Part A → `docs/hermes/optimization-report.md`（**停**）。
3. Part B → `docs/hermes/hermes-design.md`（**停**）。
4. Part C **Phase 0** → 硬化 + 假資料單測（**停**）。
5. Part C **Phase 1** → Factor Foundry（分小階段，每步**停**）。

開始前先回一句話確認你已讀懂 §0 鐵律與 §8 順序，然後從 §1 開始。

---
