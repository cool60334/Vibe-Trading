# Talos — 全景設計文件（Part B）

> 產出：Part B 設計 spec · 2026-07-08 · 分支 `quant-trading-dashboard`
> 前置：Part A 優化報告（`docs/talos/optimization-report.md`）已完成；憲法地基 A1/A2/A3 已落地並部署伺服器（HEAD `ec3ba86`）。
> 性質：**設計文件，等核准才進 Part C 實作**。本文不寫任何 production code。
> ⚠️ 2026-07-12：Part C Phase 0–1 已核准並**實作完成**，本文為歷史設計快照；真實進度見文末「交付狀態」與根目錄 `PROJECT_STATE.md`。
>
> **命名**：Talos = 守護克里特的青銅自動人 —— 確定性自動機 + 護欄。原代號 Hermes 於 2026-07-04 更名（與 NousResearch `hermes-agent` 撞名，本設計與該專案無關）。

---

## 0. 一句話定位

**Talos = 蓋在現有 job/reconcile（寫檔→對帳）解耦模式上的自主研究決策大腦。** 它**寫決策**（job/候選/證據卡），既有 runner **執行**；它**永不 inline 自己跑 stage、永不碰線上 trader**。v1 只深做一件事：**Factor Foundry（因子鑄造廠）** —— 直攻當前系統最痛的瓶頸「因子數 / archetype ceiling」（見 [`project_eth_s1_sharpe_ceiling`]，ETH 500-combo sweep 觸頂 sharpe 0.744，瓶頸在因子供給而非優化）。

---

## 3.1 願景與範圍

### 6 大能力（分階段，非一次到位）

| # | 能力 | 白話 | v1 狀態 |
|---|------|------|---------|
| 1 | 選幣 | 掃哪些幣有足夠資料源可做研究 | 手動（`coin_scout` 已存在）→ Phase 2+ 自動 |
| 2 | **找因子** | ETL + 算 IC/IR，發掘新預測訊號 | **v1 深做 = Factor Foundry** |
| 3 | 跑 pipeline | 對候選因子跑 9 階段回測 | 手動觸發 → Phase 2+ 編排 |
| 4 | 優化 pipeline | 改善 pipeline 本身效率/正確性 | 只提案（Part A 報告）→ Phase 2+ |
| 5 | 推薦進 paper | 通過門檻的策略推上模擬倉 | **v1 不做**（v1 只產候選因子，不碰策略/paper）；Phase 2+ 且採 proposal job（見 3.2）|
| 6 | 推薦實盤 | 真錢部署 | **永遠人工核准硬把關** |

### v1 範圍鎖定（決策 D4）

**v1 = Factor Foundry，其餘手動。** 理由：能力 2 是唯一直接鬆綁 ceiling 的環節；其餘（選幣/編排/優化/晉級）在因子供給不足時做了也無用。先把「因子從哪來」做對做穩，再談自動化其餘環節。

### 明確不在範圍（決策 D3）

- **不上網爬未知因子**。只做兩件：①在**既有欄位**上發明衍生因子；②接**已知但尚未接**的資料源（照現有 loader 樣板，且新 fetcher 走人工核准閘）。
- **不自動改 production**。Foundry 產出寫隔離候選庫，人工 promote 才進 production。
- **不自動實盤**。

---

## 3.2 自主與安全模型

分三級信任邊界，愈往真錢愈嚴：

| 邊界 | 動作 | Talos 權限 |
|------|------|-----------|
| **候選庫（隔離）** | 產因子、算 IC、寫候選特徵、記證據卡/墓地 | **全自動** |
| **模擬倉（paper/testnet）** | 把通過門檻的策略推上紙上交易 | 安全模型**准**自動（決策 D1），但 **v1 不實作此能力**；Phase 2+ 落實時仍走 write-file→reconcile：Talos 寫 proposal job，不 inline 推倉（與骨架其餘機制一致，見附錄 C-9）|
| **Production 特徵庫** | merge 候選因子進 `features_<sym>.parquet` / `factor_values_<sym>.parquet` | **人工 promote** |
| **新資料源 fetcher** | 新程式碼碰外部 API | **人工核准才上**（決策 D7）|
| **真錢實盤** | live 部署 | **人工核准硬把關**（決策 D1）|

**核心原則**：自動化只發生在「錯了不會虧錢、不會污染線上」的沙盒側。任何會影響真錢系統讀取的資料（production parquet、trader config）或碰外部世界（新 API）都需人工閘。

**優化只提案（決策 D9）**：Talos 對 pipeline 本身的改善一律「先設計、分階段、提案等核准」，不自動改 pipeline code。

---

## 3.3 大腦架構 —— 確定性骨架 + LLM 只在創意點介入

**混合架構（決策 D2）**。這是整份設計的核心安全模型：**憲法護欄是確定性硬約束，不是 prompt 指引**。LLM 只被允許在「需要創意、錯了有確定性守門攔截」的窄點介入。

### 為什麼不用 agent 框架當引擎（agy 二審關鍵裁定）

若用通用 self-improving agent（如 hermes-agent）當引擎，**安全模型會反轉**：憲法從硬約束退化成 prompt 裡的建議，LLM 可繞過。故骨架 = 自寫確定性 code。hermes-agent 唯一合理定位 = Phase 2+ 可選實驗：斷網容器內的「虛擬初階研究員」，只能透過 write-file 介面向骨架提交產物 —— **它是系統的使用者，不是引擎**。

### 每環節標記（確定性 / LLM）

| 環節 | 類型 | 說明 |
|------|------|------|
| 假設佇列生成（4 來源） | 確定性 | alpha zoo 遷移 / 高IC因子衍生 / 學術 = 確定性列舉；LLM 發想為其中一源 |
| 因子公式/ETL 產碼 | **LLM** | 唯一真創意點；必在 PIT 封裝 + 沙盒內 |
| 去重 + 死因子墓地過濾 | 確定性 | **數值序列** abs(Spearman)>0.7 比對候選 vs 既有/墓地歷史值（非字串名比對——LLM 換名即繞過）|
| 沙盒執行 | 確定性 | timeout + 記憶體上限 + 斷網 + **root FS 唯讀 + 單一可寫 output mount**（僅落候選 dataframe）|
| IC/IR/Net IC 計算 | 確定性 | 複用 `factor_metrics` / `apply_ic_eval_transform` |
| 統計守門（DSR/PBO/regime/Net IC） | 確定性 | 全過才合格；閾值寫死 |
| stage3 診斷解讀 | **LLM**（Phase 2）| v1 維持靜態摘要 + deterministic fallback |
| 晉級門檻判定 | 確定性 | OOS 為權威，門檻寫死 |
| 排程/觸發 | 確定性 | 按需 + nightly cron |

### 有界修復迴圈（agy 二審升級點）

LLM 單發產碼良率太低。故創意點升級為**有界修復迴圈**：

```
LLM 產碼 → 沙盒跑 → 失敗則回饋錯誤重試（≤N 次）
         → token / 時間 / compute 硬預算封頂
         → 成功 → 入候選庫
         → N 次仍失敗 → 進墓地，記死因
```

Early stopping：nightly 若前幾個因子極差，中止當晚（防發散燒預算）。

### LLM 設定（鐵律 7）

沿用現有 agent 設定（OpenRouter 中介，**非 Anthropic 直連** —— 見 [`vibe_trading_provider_constraint`]），model 參數化可設定。

---

## 3.4 憲法護欄 —— 血淚教訓編碼成機器可執行硬約束

**原則：不是註解，是 code path 上的自動檢查。** 下列每條都要有對應的確定性 gate 或封裝，Foundry 無法繞過。

| 憲法條文 | 硬約束機制 | 來源教訓 |
|---------|-----------|---------|
| **Point-in-time，禁 look-ahead** | PIT DataFrame 封裝結構性遮蔽 T 之後資料 + 擴充 zoo AST gate：禁負 shift **且封殺 `bfill`/`backfill`/`fillna(method='bfill')` 等逆向填補**（無聲拿未來值補現在，見附錄 C-5） | [`project_ic_measurement_layer_fix`] |
| **OOS 為權威** | Foundry 只在 pre-`oos_start` 內切 train/val；pipeline walk-forward OOS 鎖死 | [`project_oos_overfit_finding`] |
| **永續回測必含 funding fee** | 沿用 A1 realistic 成本模型（已 default） | [`project_stablecoin_sign_strategy`] |
| **intraday 必跑 entry-lag / intrabar 審計** | 1H 為研究地板；intraday OHLCV 價格衍生類**已封盤** | [`project_orderflow_poc_results`] [`project_intrabar_stop_audit`] |
| **abs(IC)>0.1 先當資料錯** | 觸發稽核，不直接採信（用 abs 防反向因子繞過） | [`project_massive_crossvenue_premium`] |
| **alpha 隨幣種而異、regime-dependent** | IC 按 regime + 分年輸出，禁跨幣假設 | [`project_eth_4yr_negative_result`] |
| **死因子墓地不可重提** | 墓地名單確定性過濾（intraday OHLCV / Binance order-flow …） | [`project_intraday_ohlcv_class_dead`] |
| **多重檢驗全漏斗記帳** | `research_ledger.jsonl`（見下）計數所有試錯；**Foundry DSR 必須從 ledger 撈家族累計 trial 做全局懲罰**（stage4 內建 DSR 只罰單 sweep 內，Foundry nightly 若只看當晚 trial=自動過擬合機，見附錄 C-3） | A2 已落地 |

### 已落地的憲法機械（A2/A3 —— Talos 直接站在上面）

Part A 第二批已把時間窗與記帳機械落地並部署，Talos 設計直接引用、不重造：

- **`resolve_anchor_date()`（`research/pipeline/stage3_backtest.py`）** —— `window_end` 凍結研究窗上緣 + `final_holdout_start` 硬上限；連手動 `--train-start` 路徑也蓋。兩 config 欄位預設 `None`＝未啟用，啟用需使用者在 `research_config.yaml` 設值並每季滾動 `window_end`（現值 `2026-04-01`，保留 ~3.3 月終極 holdout）。
- **`research/lib/research_ledger.py`** —— append-only JSONL「漏斗黑盒子」。計數 stage0a 篩了幾個因子、跑了幾次 sweep、同一 OOS 窗被評估幾次。**透明非懲罰**：計數浮上 manifest/gate 當 red flag，不硬擋；寫入 fail-soft（ledger 故障絕不 fail stage）。
- **`RedFlagCode.OOS_WINDOW_OVEREVALUATED`** —— 同窗評估 ≥4 次觸發 informational red flag（不 fatal）。
- **`python -m research.pipeline.final_holdout --strategy <id>`** —— **全 repo 唯一**准在 `[final_holdout_start, today]` 回測的 code path，promote 前人工一次性跑；任何自動鏈（stage/runner/cron/dashboard 按鈕）都不得 import 它，re-peek 大聲警告。

**Talos 的義務**：每次 Foundry 動作（篩因子/產碼/跑 IC）都寫 ledger 事件，讓多重檢驗債務可見。

### A1 成本模型（已落地，Foundry 繼承）

realistic 成本模型（taker 雙腿 + 逐 bar funding PIT 查值 + 平倉費）已是研究側 default（兩層 gating：引擎 legacy default 不變供 golden byte-identical，研究 pipeline default realistic）。Foundry 的成本把關用此成本模型算 **Net IR/Sharpe**（1-period 重平衡淨值序列），訊號品質用 **Gross IC**——**不**把成本塞進 IC（見附錄 C-1 2026-07-08 逆轉）。

---

## 3.5 與現有架構整合 —— write-file → reconcile

**鐵律 2：Talos 絕不 inline 跑 stage。** 它寫決策檔，既有獨立 runner 撿起執行。與線上零耦合。

### Talos 寫哪些檔 / 誰 reconcile

| Talos 寫入 | 位置（建議） | Reconciler（既有/新） | 擾動線上？ |
|-----------|-------------|---------------------|-----------|
| Foundry job | `runs/foundry_jobs/<id>/job.json` | 新 foundry runner（樣板抄 `pipeline_jobs.py`）| 否，獨立 job 佇列 |
| 候選特徵 | `research/manifests/candidate_features/<sym>.parquet` | 無（人工 promote）| 否，隔離庫。**須用 `factor_io` atomic write（mkstemp+os.replace）**——parquet 無 native append，nightly 併發直寫會損毀（見附錄 C-8）|
| 證據卡 | `research/manifests/foundry_evidence/<sym>.json` | dashboard 唯讀顯示 | 否 |
| 墓地 | `research/manifests/factor_graveyard.json` | Foundry 自讀自寫 | 否 |
| 新 fetcher 提案 | `runs/foundry_jobs/<id>/fetcher_proposal.py` | **人工核准後**才進 `research/lib/` | 否，等閘 |

**關鍵隔離不變式**：Foundry **絕不**寫 production `features_<sym>.parquet` / `factor_values_<sym>.parquet`（實盤 trader + freshness scheduler 會讀）。違反 = 污染真錢系統。

### 複用既有元件（鐵律 6，別重造）

- `research/lib/coin_scout.py` —— 選幣可行性探測
- `research/lib/factor_metrics.py` + `apply_ic_eval_transform` —— IC 評估（已含非平穩修正，**別重寫**）
- `research/pipeline/stage0a_features.py` feature store 樣板
- `dashboard/server/pipeline_jobs.py` / `freshness_scheduler.py` —— job runner + 排程樣板
- `research/lib/cpcv.py`（purge+embargo）、stage4 DSR —— Part A 確認已健全，直接複用

### 新模組位置

`research/hermes/`（目錄名沿用，避免大規模改動；文件內一律稱 Talos）。

---

## 3.6 分階段 Roadmap

### Phase 0 — 基礎設施硬化（**最先，沒建好前不准寫任何自動產因子邏輯**）

否則產出全是海市蜃樓且污染 `research/`。五項（agy 二審：Phase 0 硬化先於 Phase 1）：

1. **PIT 防 look-ahead 封裝** —— DataFrame 工具，算特徵時結構性遮蔽 T 之後資料，讓 LLM 產碼**無法**取未來值。複用/擴充 zoo AST lookahead gate：禁負 shift **且封殺 `bfill`/`backfill`/逆向 `fillna`**（附錄 C-5）。
2. **隔離候選特徵庫** —— Foundry 產出寫此，絕不寫 production。人工 promote 才 merge。
3. **產碼沙盒** —— LLM 產的 **feature transform** ETL 跑在 timeout + 記憶體上限 + 斷網 + **root FS 唯讀 + 單一可寫 output mount（tmpfs 或指定 bind-mount，僅落 candidate dataframe）**。防無限迴圈 / OOM / 刪檔。**兩階段切分**（附錄 C-2/C-6）：**data fetching**（新資料源、需連網）= 沙盒外、人工核准後才跑；**feature transform** = 斷網沙盒內，禁任何網路請求。
4. **嚴格資料切分** —— Foundry 只在 pre-`oos_start` 內再切 train/val；pipeline walk-forward OOS 鎖死不得觸碰，保留給晉級前一次性檢驗（`final_holdout.py`）。
5. **假資料單測驗證上述全部**。→ **停、等核准。**

### Phase 1 — Factor Foundry 引擎

```
假設佇列（4 來源：alpha zoo 452 遷移 / LLM 金融知識發想 / 高IC因子衍生 / 學術）
  → 去重 + 死因子墓地過濾（數值序列 abs(Spearman)>0.7 比對，非字串名比對——見 3.3）
  → LLM 產因子公式 or ETL：
      • Feature Transform → 斷網沙盒內（PIT 封裝 + 有界修復迴圈 ≤N 次）
      • 新資料源 Fetching → 沙盒外、人工核准後、允許連網的獨立階段（見 2.2 兩階段切分）
  → 沙盒執行 → 測 IC/IR（複用 factor_metrics / apply_ic_eval_transform）
      • 若產出 1H signal → IC 用 lagged signal（模擬 execution lag）；促晉級進 pipeline
        時繼承 stage3 lag-stress / intrabar 審計（見 4.2）
  → 統計守門（全過才合格；門檻 config 化，lag 守門員內建；agy-v2 見 1A 計畫）：
      • 訊號品質 = Gross IC（factor vs 原始 h-horizon 報酬）
      • 成本把關 = Net IR/Sharpe（1-period 重平衡淨值：weights_t×ret1_{t+1} − turnover_t×cost；
        turnover 來自權重＝P1；1-period 避免 h-period 重疊→Sharpe 假暴增。⚠️ 廢除 net_ic，見附錄 C-1）
      • turnover 絕對上限守門（>max_turnover 不可交易→reject）
      • 非重疊 IC / Newey-West：長 horizon 每 h 小時取一點補 ic_nonoverlap（Part A A5，附錄 C-4）
      • IC 按 regime（bull/bear/neutral，日級 ffill）+ 分年 分別輸出（防「只在牛市強」被掩蓋）
      • 對既有+墓地因子 abs(Spearman)>0.7 → 丟/進墓地（矩陣 pairwise，abs 防反向，附錄 C-2/C-6）
      • DSR：trial count 從 research_ledger 撈該標的**同 interval** 歷史累計（同質子集，非混異質 T；附錄 C-3）
  → 過關 → 寫候選特徵庫（atomic write，見 3.5）+ 證據卡；未過 → 進墓地 + 記死因
觸發：按需（config 化，預設首推 ETH —— 正卡 ceiling）+ nightly cron
      nightly 有 compute/token 預算 + early stopping
```

**每因子產證據卡**：Gross IC / Net IR / ic_nonoverlap / regime 分解 / 分年 / DSR（同質 trial）/ PBO / turnover / 相關性 / 死活與死因。

→ 分小階段，每步**停、等核准**。

### Phase 2+ —（v1 不做，列為後續）

- 選幣自動化（`coin_scout` 升級）
- pipeline 編排自動化
- 優化自動化（只提案）
- paper 晉級推薦（proposal job，非 inline 推倉——附錄 C-9）
- 實盤推薦（人工閘）
- **stage3 診斷 LLM 白名單唯讀查詢工具**（v1 維持靜態摘要）
- **alpha decay 監控** —— 因子 IC 非靜態（crypto 半衰期可能數週），衰退因子回墓地
- hermes-agent「虛擬研究員」實驗（斷網容器，write-file 提交）

---

## 7. 已知限制（誠實記錄，別假裝解決）

- **倖存者偏差（survivorship bias）** —— 只測現存幣有偏；完整下架合約史 v1 恐不可行。`coin_scout` 標註風險，本文明列為已知限制。
- **Alpha decay** —— 因子 IC 隨時間衰退；v1 不建監控，Phase 2+ 補。
- **LLM 產碼良率** —— 有界修復迴圈緩解但不消除；預算封頂 + 墓地記帳吸收失敗。

---

## 附錄 A：agy 對抗式審查 12 點加固（全採納）

關鍵五點已編入 3.4/3.6：①結構性 PIT 防 look-ahead（不只靠 IC 啟發式）②防 OOS 洩漏（Foundry 只在 pre-oos_start 切，pipeline OOS 鎖死）③隔離候選庫（不寫 production）④LLM 產碼沙盒⑤Phase 0 硬化先於 Phase 1。精修：Net IC 扣換手、per-regime IC、DSR+PBO、Spearman>0.7 丟棄、compute 預算。註記：倖存者偏差、alpha decay 為已知限制。

## 附錄 B：9 決策速查

D1 自動到 paper、實盤人工核准 · D2 混合大腦（確定性骨架 + LLM 只做創意）· D3 既有欄位發明 + 接已知未接源（不上網）· D4 v1=Factor Foundry · D5 因子來源全選（zoo/LLM/高IC衍生/學術）· D6 複用評估層 + 正交/去重/墓地 + 多重校正 · D7 既有資料自動、新源 fetcher 人工核准 · D8 按需 + nightly cron · D9 優化只提案、先設計後分階段。

---

## 附錄 C：agy 二審加固（2026-07-08，11 findings 全採納）

agy 對抗式審查本設計 + 交叉核對既有 code（`resolve_anchor_date` / `research_ledger` / `final_holdout` / `RedFlagCode.OOS_WINDOW_OVEREVALUATED` / `factor_metrics` **引用全查核無誤**）。11 點漏洞全採納並折入上文：

**實質方法論修正**
- **C-1 Net IC 維度錯（數學無效）** —— 原 `Net IC = IC − turnover×cost` 把無單位相關係數減 bps 損耗，不成立。此錯源頭在 `fable5-prompt.md §6` mandate 原文。
  - **~~初版修法~~**（agy 前輪）：先算 net forward return（gross − 換手×成本）再算 IC。
  - **⚠️ 2026-07-08 逆轉（agy Phase 1A 二審，權威）**：「把成本扣在 forward return 上再算 rank-corr」統計上仍**無意義**（扭曲報酬分佈）。**廢除 net_ic 概念**。正解：**訊號品質＝Gross IC**（factor vs 原始 h-horizon 報酬）；**成本把關＝Net IR/Sharpe**，從**正確對齊的 1-period 重平衡淨值序列**算（`weights_t × ret1_{t+1} − turnover_t × cost`；turnover 來自權重＝P1；1-period 報酬避免 h-period 重疊→自相關→Sharpe 假暴增）。1E `EvidenceCard.net_ic` 欄改名 `gross_ic`。詳 `docs/talos/plans/2026-07-08-talos-phase1a-gatekeeper.md`（agy-v2）。
- **C-2 abs 防反向因子** —— LLM 產舊因子×−1 即讓 Spearman<−0.7、IC 轉負，繞過 >0.7 / >0.1 門檻。改 `abs(Spearman)>0.7`、`abs(IC)>0.1`。→ 3.3、3.4、3.6。
- **C-3 DSR 跨夜試錯洩漏** —— nightly 只算當晚 trial 會嚴重低估多重檢驗債務=自動過擬合機。Foundry DSR 改從 `research_ledger.jsonl` 撈家族累計 trial 全局懲罰。→ 3.4、3.6。
- **C-4 漏 Newey-West / 非重疊 IC** —— Part A A5（`optimization-report.md:96,254`）明列併入 Foundry 守門，設計初稿遺漏。補回 `ic_nonoverlap` + t-stat Newey-West 校正。→ 3.6。

**繞過硬化**
- **C-5 bfill 穿越** —— AST 只禁負 shift，漏 `bfill`/`backfill`/逆向 `fillna` 無聲拿未來值補現在。gate 封殺之。→ 3.4、Phase 0-1。
- **C-6 墓地字串比對可繞** —— LLM 換變數名即繞過名單比對。改對死因子**歷史數值序列** `abs(Spearman)>0.7` 數學過濾。→ 3.3、3.6。
- **C-7 Foundry lag 審計架空** —— 只算 IC 就寫候選，忽略 entry-lag 鐵律。1H signal 的 IC 用 lagged signal；促晉級進 pipeline 繼承 stage3 lag-stress/intrabar 審計。→ 3.6。
- **C-8 候選 parquet 併發損毀** —— parquet 無 native append，nightly 併發直寫毀檔。強制 `factor_io` atomic write（Part A 確認已存在）。→ 3.5。

**矛盾釐清**
- **C-9 paper 自動 vs Phase2+ 推薦矛盾** —— D1「自動到 paper」vs Phase2+「晉級推薦」。統一：v1 不做 paper；安全模型准自動但能力屬 Phase 2+，且走 write-file→reconcile proposal job 非 inline 推倉。→ 3.1、3.2、3.6。
- **C-10 沙盒「唯讀 FS 但能寫」自相矛盾** —— 明定 root FS 唯讀 + 單一可寫 output mount（tmpfs/bind-mount）。→ 3.3、Phase 0-3。
- **C-11 新 fetcher 需網路 vs ETL 斷網衝突** —— 切兩階段：data fetching（沙盒外/人工核准/可連網）vs feature transform（斷網沙盒，禁網路請求）。→ 3.6、Phase 0-3。

---

## 交付狀態

- [x] Part A 優化報告（`docs/talos/optimization-report.md`）
- [x] 憲法地基 A1（成本模型）/ A2（window freeze）/ A3（ledger + final_holdout）落地 + 部署
- [x] **Part B 全景設計（本文件）**
- [x] Part C Phase 0 硬化 + 假資料單測 —— 完成（`research/hermes/` 5 模組，六輪審查）
- [x] Part C Phase 1 Factor Foundry —— 完成（1A gatekeeper → 1B hypothesis queue → 1C forge → 1D orchestrator → 1E evidence card，全 merged）
- [x] Foundry 首次真容器全鏈跑通（88dc898）+ 花費帳本/每日 LLM 上限 + LLM provider 泛化（OpenRouter/OpenAI）
- [x] Foundry OHLCV refresh（對真 `research/manifests/` 跑的最後一哩）—— 完成（`2cacd5a` + cron wrapper `9de1927`）
- [x] Foundry→pipeline 橋（斷点 B）+ 自動排程（斷点 A）—— 完成（`bc907d1` / `43a9d86` 起）
- [x] 因子轉正 induction（Foundry 碼→production 因子庫，`induct.py`）—— 完成（`b5d93f1`→`551e574`）
- [x] Foundry LLM ideation（panel schema→`ideator.py`→`run_foundry`）—— 完成（`8ebebcd`→`fd00758`）
- [ ] **Foundry 偵測門檻校準** —— spec/plan 已 commit（`f5c23ea`→`d51a47f`），**實作未動**。positive control 量出最低偵測門檻 ≈ 年化 Sharpe 5；三個統計錯誤待修
- [ ] Phase 2+（選幣自動化 / pipeline 編排 / paper 晉級推薦）—— 未開始

> 2026-07-12 更新：本節原停在「等核准」；設計其後已核准並實作完畢，checklist 補記至真實進度。
> 2026-07-17 更新：checklist 再次補記（OHLCV refresh / 斷点 A+B / induction / ideation 皆已完成；新增校準待辦）。真實進度以 `PROJECT_STATE.md` 為準。
