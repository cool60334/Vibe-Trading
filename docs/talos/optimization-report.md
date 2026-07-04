# Part A — 架構審查 + 優化提案報告

> 產出：Fable 5（Talos 專案 Part A）· 2026-07-04
> 範圍：`research/` + `dashboard/`（quant pipeline 私有系統）；`agent/` 僅作為被依賴的回測引擎審視，不提議修改上游。
> 性質：**只提案、不實作**。每項附優先序，等使用者挑選後才動工。
> 伺服器觀察：已透過唯讀 SSH（fable-ro@&lt;server-ip&gt;，IP 不進版控；零 mutate、零觸碰秘密）折入 runtime 面。

---

## 0. 審查方法與證據來源

- 精讀：`stage0a_features` / `stage3_backtest` / `stage4_optimize` / `stage4_cpcv` / `stage5`（文件）/ `signal_compiler` / `factor_metrics` / `deflated_sharpe` / `cpcv` / `emit_manifest`（gate 區段）/ `agent/backtest/engines/base.py + crypto.py` / dashboard `pipeline_jobs` / `pipeline_manager` / `freshness_scheduler` / `supervisor` / trader `loop` / `paper_broker` / `freshness`。
- codebase-memory-mcp 索引全 repo（20,985 nodes / 79,523 edges）輔助定位。
- 伺服器唯讀觀察：資源、git drift、factor 新鮮度、pipeline job 佇列、三個 paper trader 狀態。

### 先講「已經做對、不用動」的部分（避免重複造輪）

審查過程確認以下設計**已經健全**，後續 Talos 應直接複用，不要重寫：

| 環節 | 現況 | 結論 |
|---|---|---|
| 執行時序 | 引擎 `_align()` 把訊號 `shift(1)` 後用**次根 K 線開盤價**成交（`base.py:126,586`）| 無同根 look-ahead，健全 |
| Walk-forward OOS | stage4 只在 train 掃參，自動用 best params 跑 held-out OOS | 正確的樣本外驗證 |
| 多重檢驗（sweep 內）| stage4 已算 **DSR（Deflated Sharpe）**，以全部 combos 為 trial 母體 | 已有，Foundry 直接複用 |
| CPCV | `stage4_cpcv` + `lib/cpcv.py`（purge + embargo）已實作，gate 已接 | 已有，Foundry 直接複用 |
| 成本/延遲壓測 | `--stress`（2x/3x 費率）、`--lag-stress`（延遲 1/2 根進場）、`--intrabar-audit` 皆已 merge 且進 gate | 已有 |
| 併發安全 | `factor_io` 用 mkstemp + `os.replace` **atomic write**，trader 讀 parquet 不會讀到半寫檔 | 已有 |
| 解耦模式 | dashboard 只寫 `job.json`/`control.json`，獨立 runner/manager reconcile；部署 dashboard 不殺 trader | 已有，Talos 的骨架 |
| 資料快取（回測側）| `agent/backtest/loaders/okx.py` 有磁碟快取（同日同窗共用），stage4 60 combos 不會重抓 60 次 | 已有 |
| 雙保險 | trader 端 killswitch（DD pause/terminate）+ factor 新鮮度 pause + refresh-stall 告警 | 已有 |
| 滑價 | 引擎雙邊固定 5bps 滑價（`apply_slippage`）；1H 頻率下固定值可接受 | 已有（動態滑價列為 Foundry 期精修，非現缺）|
| 強平/保證金 | `crypto.py` 有 liquidation check（`check_crypto_liquidation`）+ funding 逐 bar 侵蝕 capital；研究一律 1x 槓桿故非約束 | 已有 |

---

## 1. 研究正確性 / 統計效度

### A1. 回測成本模型三重失真（funding / 平倉費率 / config fees 未接）

- **環節**：Stage 3/4 所有回測 → `agent/backtest/engines/crypto.py`。
- **白話**：funding fee（資金費率）是永續合約每 8 小時多空之間互付的錢，實際上隨市場劇烈變動、且**跟你的訊號相關**（行情極端時 funding 也極端）。
- **現況問題**（三件事疊在一起）：
  1. **funding 用固定費率**：引擎把 funding 模擬成固定 `0.0001/8h`（`crypto.py:39`），方向永遠「多方付、空方收」。真實 funding 是歷史序列，會變號。對 funding 類策略（如 funding_z contrarian —— 在 funding 極負時做多）失真最大：真實世界那筆倉位是**收** funding 的，回測卻在**付**；反之空單在 funding 為負時實際要付錢，回測卻讓它收錢。
  2. **平倉收 maker 費率**：`calc_commission` 假設「開倉 taker 0.0005、平倉 maker 0.0002」（`crypto.py:52-59`），但 live trader（`paper_broker.py` 與真單路徑）**雙邊都是市價單 taker 0.00055**。回測每個來回比 paper/實盤便宜約 4 bps。
  3. **config 的 fees 根本沒接進 pipeline**：`research_config.yaml` 寫 taker 0.00055，但 `stage3_backtest.build_run_config()` 只有在 stress（`fee_multiplier`）時才注入 fee keys（`stage3_backtest.py:196-198`）；base/sweep/OOS run 全部用引擎內建預設（taker 0.0005）。config 的 `fees:` 區塊目前只有幾支 legacy `setup_*.py` 腳本在用。
- **合併影響**：一個 ~150 trades/年 的策略，光費率差就低估 ~0.6%/年成本，funding 失真另計（可能同量級或更大、且**有方向性偏誤**）。所有既有 Sharpe/return 的絕對值系統性偏樂觀；策略間相對排序大致仍可信（同一套偏誤套在所有策略上），但 **gate 門檻（如 OOS sharpe ≥ 1.0）實際上是用打了折的成本在過關**。
- **怎麼優化**：
  1. 引擎加 `funding_series` 模式：base run 的 config.json 帶 funding 歷史檔路徑（資料**已經在** feature store 的 `funding_rate_raw`，零新抓取），`calc_crypto_funding_fee` 逐 8h 結算點查歷史值；查無值才 fallback 固定費率。**PIT 警告（agy 二審補強）**：注入歷史 funding 本身可能引入新的時間穿越 —— 結算值必須在「結算時點之後的 bar」才可見/生效，時間戳對齊要有專屬單測，不可讓回測在結算 bar 開盤就「預知」該筆 funding。
  1b. 滑價現況：引擎已有雙邊固定 5bps —— 1H 頻率下可接受，**不在本項範圍**；動態（vol/volume-based）滑價模型留給未來更高頻研究，避免本項範圍膨脹。
  2. `calc_commission` 加 config key（如 `close_as_taker: true`），研究 pipeline 一律雙邊 taker。
  3. `build_run_config()` 無條件注入 `research_config.fees`（單一事實來源），stress 乘數疊在其上。
  4. 改完**重跑全部 baseline**，比對新舊指標差距，重新校準 gate 門檻。
- **影響範圍**：`agent/backtest/engines/crypto.py`、`_market_hooks.py`（上游檔案 —— 需以「不破壞上游行為」方式加：新 config key 預設維持舊行為）、`research/pipeline/stage3_backtest.py`、`stage4_optimize.py`、假資料單測。
- **風險**：既有策略指標全面下修，可能有策略跌破 gate（這是**誠實化**，不是變壞）；動上游引擎檔需保證 default 行為不變（用 opt-in config key）。
- **優先序**：**P0**。這是「回測數字可不可信」的根，也是 Talos 拿回測結果做自主決策前的前提。

### A2. OOS 重複窺視（peeking）無登記、無家族層級懲罰

- **環節**：整條 pipeline 的迭代工作流（stage4 walk-forward → 3-diag → 人看結果 → 改策略 → 再跑）。
- **白話**：OOS（樣本外）之所以可信，是因為「沒看過」。但每迭代一版策略就看一次同一段 OOS（例：eth_s1→s2→s3→s4→s5 五連迭代、sol_s1 重跑），OOS 就慢慢變成「第二個 train」—— 你留下的版本正是碰巧在這段 OOS 表現好的版本。這叫 selection bias on the holdout。
- **現況問題**：DSR 只懲罰 stage4 sweep **內部**的 trials 數，不知道「這個因子家族已經第 5 次看這段 OOS」。記憶中 sol_s1 的「OOS 污染需重驗」正是此問題的實例。
- **怎麼優化**（agy 二審後重排 —— 機制強度由高到低）：
  1. **終極 holdout（主要機制、強制）**：保留最近 2-3 個月完全不進任何迭代，只在 promote-to-paper 前跑**一次**（一次性、結果進 manifest）。sequential peeking（人看過 OOS 再改策略）帶路徑依賴，其過擬合速度超過任何平行試驗假設 —— **數學懲罰擋不住它，只有「真的沒看過的資料」擋得住**。
  2. **全局研究記帳（ledger + trial registry）**：不只記 OOS 窺視 —— 從 stage0a 篩了幾個因子、stage1 淘汰幾個、stage4 掃了幾組，到 walk-forward 跑了第幾次，整條漏斗的 trial 數都 append 進 `research/manifests/research_ledger.json`（family、因子集 hash、時間、窗、結果摘要）。**現況 DSR 只罰 stage4 sweep 內部，上游 feature-selection 的 data snooping 完全沒被計入**（agy 補抓的缺口）。
  3. family-wise 標示：gate/報表明示「此 OOS 已被本家族看過 N 次、全漏斗累計 M 次 trial」。定位是**透明化警示，不是嚴格統計懲罰**（把窺視次數塞進 DSR trial 數只是 heuristic，統計正當性不足，不當作硬 gate）。
- **影響範圍**：`stage4_optimize.py` / `stage0a`、`stage1`（寫 ledger）、`emit_manifest.py`（gate 讀 ledger + 終極 holdout 結果）、新 lib 模組 + 單測。
- **風險**：低（記帳性質）；終極 holdout 會縮短可用 OOS 窗（可接受的代價）。
- **優先序**：**P1**；但**若 Talos 自動迭代（Phase 1+）啟動，此項視同 P0 前置** —— 自動化會把窺視頻率放大百倍，沒有 ledger + 終極 holdout 就是自動化過擬合機器。

### A3. 回測窗口隨執行日漂移（不可重現的跨期比較）

- **環節**：`stage3_backtest.build_run_config()`：`start = today - period days`。
- **現況問題**：同一策略今天跑和下週跑，train 窗起點差 7 天 —— 單一 run 可重現（config.json 有記錄），但**跨時間的 run 之間不可比**：sharpe 變了 0.05，你分不清是改動造成還是窗口漂移造成。Talos 自動化後會大量做「改動前後對比」，這個噪音源會直接污染它的決策。
- **怎麼優化**（agy 二審修正版）：**不是**固定 `train_start` 錨點（那會讓 train 窗隨時間越長越不平穩、自由度漂移），而是「**固定窗長 + 凍結窗口**」：`research_config.yaml` 加可選 `window_end` 凍結參數 —— 一個迭代週期內所有 run 用同一組 (window_end, period) → start/end 全同、可比；週期結束（如每季或每輪 selection 後）人工滾動 `window_end`。未設時維持現行為（end=today）。Talos 憲法規定：**做 A/B 對比的 run 必須同窗（start 與 end 都同）**。
- **影響範圍**：`pipeline/config.py`、`stage3_backtest.py`、`stage4_optimize.py`。
- **風險**：極低；凍結期間最新資料不進回測（本來就該如此 —— 那是 OOS/實盤的事）。
- **優先序**：**P1**。

### A4. Sub-hour 時間級別的兩顆地雷（目前無實害，但會無聲出錯）

- **環節**：`signal_compiler.py` + `stage4_optimize.py` / `stage4_cpcv.py`。
- **現況問題**：
  1. compiler 把 `percentile_<n>d` / `zscore_<n>d` 的 rolling 窗寫死為 `n*24` 根 K（`signal_compiler.py:191,198,322`），並把 `max_hold_hours` 直接當「根數」比（`bars_held >= max_hold_hours`）。30m 級別下「90 天窗」實際只有 45 天、「持有 24 小時」變 12 小時 —— **不會報錯，只會默默算錯**。
  2. stage4 與 stage4_cpcv 用寫死的 `research/manifests`，而 stage3/3diag/1/2.5/emit_manifest 都用 `active_manifests_dir()`（interval 命名空間）。`RESEARCH_INTERVAL=30m` 跑 stage4 會**讀寫 1H 的 manifests**，交叉污染。
- **背景**：目前政策是「1H 為研究地板」，所以無現行損害；但 15m/30m 管線號稱「全 arc ✅」，這兩處與該聲明矛盾，未來任何人重開盤中研究就會踩。
- **怎麼優化**：compiler 全部改走 `bars_per_day(interval)` / `bars_per_hour(interval)`（模板注入 interval）；stage4 兩支改用 `active_manifests_dir()`。補 30m 假資料單測。
- **影響範圍**：`signal_compiler.py`、模板、`stage4_optimize.py`、`stage4_cpcv.py`。
- **風險**：需重編譯既有 signal_engine 驗證 1H 輸出 byte-identical（interval=1H 時 `bars_per_day=24`，數學上不變）。
- **優先序**：**P2**（1H 地板政策下），若重啟盤中研究則自動升 P0。

### A5. IC 顯著性被重疊樣本灌水

- **環節**：`factor_metrics.py` / stage0a evidence。
- **白話**：用 1H 步距去算「未來 72h 報酬」的相關性，相鄰兩個樣本有 71 小時是同一段行情 —— 名義上 3 萬個樣本，有效樣本可能只有幾百個。IC 的「顯著性」因此看起來比實際強。
- **現況緩解**：evidence 檔已附 multiple-testing caveat；IR 用 rolling-window 分佈；`apply_ic_eval_transform` 已處理 ffill 灌水（funding/stablecoin 已按原生頻率取樣）—— 方向正確，但**價格類指標的重疊問題仍在**。
- **怎麼優化**：Foundry Phase 1 的統計守門一併做：IC 對每個 horizon 用**非重疊子取樣**（每 h 小時取一點）補一欄 `ic_nonoverlap`，或對 IC t-stat 做 Newey-West 校正。篩選門檻改看校正後數字。
- **影響範圍**：`factor_metrics.py`、`stage0a_features.py`（evidence schema 加欄位，向後相容）。
- **風險**：低；門檻可能因此變嚴（誠實化）。
- **優先序**：**P2**（與 Foundry Phase 1 守門合併做最省）。

### A6. 已知限制（誠實記錄，不假裝解決）

- **倖存者偏差**：只研究現存活躍合約；下架/歸零幣不在樣本 → 因子族群層面的績效估計偏樂觀。v1 不解，`coin_scout` 註記 + 本報告列為 known limitation。
- **positions.csv 是目標權重非成交路徑**：intrabar audit 已自知並在 trade count mismatch 時告警（`stage3_backtest.py:510-515`），維持現狀即可。

---

## 2. 效能 / 成本

### B1. stage0a 每次全量重抓 + 全量重算（每日 ~2.3h、每小時 live_refresh 12–22 分/幣）

- **環節**：`stage0a_features._process_symbol()` + `lib/okx_data.py` / `ccxt_data.py` / `defillama_data.py`。
- **現況問題**：每次 refresh（包括**每小時**的 live_refresh）都重新從 OKX 抓 4 年 OHLCV（perp+spot）、Coinbase 抓 2 條 USD 序列、ccxt 抓 4 年 funding、DefiLlama 全史 —— 然後重算全部 27 個特徵、重算全部 IC。伺服器實測：daily 全符號批次跑 ~2.3 小時；hourly live_refresh 單幣 12–22 分鐘（其中絕大多數是重抓與重算歷史，只為了更新最後幾根 K）。研究側 `lib/okx_data.py` **沒有**回測側 loader 那種磁碟快取。
- **怎麼優化**：
  1. 原始資料層增量快取：`research/data/ohlcv/` 等 parquet append（比照既有 `research/data/oi/` 的做法 —— OI 已經是增量的，樣板現成）。
  2. live_refresh 快路徑：只更新尾部窗（最長 rolling 窗 + buffer），特徵尾段重算後拼接；每週跑一次全量重建做對帳（checksum 比對），防增量漂移。
  3. **緊急控制閥（agy 二審補強）**：快取帶 TTL/世代標記，外加「一鍵全量抹除重建」開關（env 或 job 參數）—— API 斷點、伺服器重啟造成的無聲資料損壞，第一時間能以全量重建自救，不用等每週對帳。
- **影響範圍**：`lib/okx_data.py`、`ccxt_data.py`、`defillama_data.py`、`stage0a_features.py`、假資料單測（增量 vs 全量一致性測試是硬要求）。
- **風險**：**中** —— 增量邏輯 bug 會無聲污染 feature store（實盤在讀）。緩解：對帳測試 + 全量重建 fallback + 先在 candidate store 試跑（見 Talos Phase 0 隔離庫，兩者可共用機制）。
- **優先序**：**P1**。這也是 Talos nightly Foundry 的算力預算前提 —— 現在的成本結構下，nightly 多因子掃描會被資料重抓吃光預算。

### B2. 已停策略仍在燒 hourly refresh

- **環節**：`freshness_scheduler.py`。
- **現況問題**：`FRESHNESS_SYMBOLS` 是靜態 env（伺服器實測 sol+xrp 每小時各跑一輪 live_refresh），但 xrp_s2 已於 07-01 停止（control.json `desired_state: stopped`）。每天白燒 ~5-8 小時等效算力給沒人用的因子。
- **怎麼優化**：`tick()` 掃 `runs/testnet/*/control.json`，只為 `desired_state == running` 的 symbol 排 refresh（env 白名單保留為 override）。
- **影響範圍**：`freshness_scheduler.py` + 單測。
- **風險**：低。注意 xrp 若計畫重啟，重啟時第一輪 refresh 會慢（可接受，trader 本就有 stale-pause 保護）。
- **優先序**：**P2**。

### B3. runs/ 與 pipeline_jobs/ 無保留政策

- **環節**：`runs/`（sweep 每策略 60+ 目錄、CPCV 每策略 400 目錄）、`runs/pipeline_jobs/`（伺服器實測已 517 個 job 目錄）。
- **現況問題**：只增不減。磁碟目前 32% 無立即危險，但 CPCV/sweep 目錄含全套 ohlcv csv，一次 stage4+cpcv 可寫數 GB；job 目錄線性成長拖慢 `list_jobs()`（每次 tick 全掃）。
- **怎麼優化**：retention 腳本（如保留最近 N 天 + 被 strategy_runs.json 引用的 run 永久保留），job 目錄 90 天歸檔。以 job 形式跑（複用 runner），不用 cron 另起爐灶。
- **影響範圍**：新增小工具 + `pipeline_jobs.list_jobs` 可選 index。
- **風險**：誤刪被引用 run → 白名單邏輯要有單測。
- **優先序**：**P2**。

---

## 3. 可維護性 / 結構

### C1. 巨檔與單檔多職責

- **環節**：`stage2_strategies.py`（1399 行）、`stage3_backtest.py`（1298 行：base + stress + lag-stress + intrabar-audit 四個 driver 同檔）、`emit_manifest.py`（1132 行）、`stage0_discovery.py`（930）、`stage3_diagnose.py`（903）。
- **現況問題**：對人還可忍，對 Talos（LLM 要讀懂再決策）與對測試隔離都是負擔；stage3 的四個 driver 共用 module state，改一個容易碰另一個。
- **怎麼優化**：不搬邏輯、只拆檔：`stage3_backtest/` 拆成 `base.py` / `stress.py` / `lag.py` / `intrabar.py` + 薄 orchestrator（保持 `python -m research.pipeline.stage3_backtest` 入口不變）。emit_manifest 拆 gate / blocks / io。
- **影響範圍**：對應檔案 + import 路徑；測試不需重寫（純搬家）。
- **風險**：低（機械性重構），但要一次到位避免 import 混亂。
- **優先序**：**P2**（建議排在 Talos Phase 1 之前做 stage3 那份，其餘隨手）。

### C2. research → agent 上游引擎的無合約耦合

- **環節**：`stage3/4` subprocess 呼叫 `backtest.runner`；`signal_compiler` 直接 import `backtest.runner` 的 AST 私有函式（`_validate_class_body` 等）。
- **現況問題**：`agent/` 是上游開源專案（會 pull 更新）。上游若改引擎細節（費率預設、對齊邏輯、AST 規則），研究結果會**默默**改變 —— 沒有任何測試會抓到「同一策略同一資料，指標變了」。
- **怎麼優化**：**golden-run 合約測試**：固定一份小型合成 OHLCV + 固定 signal_engine，快照 metrics.csv 關鍵欄位；每次跑 research tests 驗 byte/數值一致。上游 pull 後跑一次即知有無行為漂移。
- **影響範圍**：`research/tests/` 新增一測 + 一份 fixture。
- **風險**：無。
- **優先序**：**P1**（成本極低、保護極大，尤其 A1 要動引擎，先有 golden run 才能證明「只改了該改的」）。

### C3. 檔案權限不一致（mkstemp 0600）+ 唯讀觀察者 ACL 失效

- **環節**：`factor_io._atomic_to_parquet`（`tempfile.mkstemp` 固定建 0600，`os.replace` 保留權限）。
- **現況問題**：伺服器上新寫的 sol/xrp parquet 是 `-rw-------`，daily 批次寫的是 `-rw-r-----+`（不同執行路徑/舊版程式）→ 唯讀帳號（fable-ro）與任何非 root 消費者對「新檔」讀取權不穩定；runbook 的 ACL 維護規則也因此形同虛設。trader 以 root 讀不受影響（所以無實害），但這是觀測性的慢性侵蝕。
- **怎麼優化**：`_atomic_to_parquet` 在 replace 前 `os.chmod(tmp, 0o644)`（或依 umask）。
- **影響範圍**：`factor_io.py` 兩個 atomic helper + 單測。
- **風險**：無。
- **優先序**：**P2**（一行修）。

---

## 4. 自動化就緒度（Talos 前置）+ 伺服器 runtime 觀察

### 伺服器現況快照（2026-07-04，唯讀）

| 項目 | 觀察 | 判定 |
|---|---|---|
| 資源 | 8GB RAM 用 1.3GB、碟 32%、load ~1（stage1 執行中） | 健康，有 headroom |
| Factor 新鮮度 | 全 9 幣 daily 批次 23:37–23:56 UTC 重寫；sol/xrp 另有 hourly live_refresh（09:27 UTC 批正常跑） | 健康 |
| Job 佇列 | 517 jobs：499 succeeded / 16 failed / 1 running / 0 queued；hourly 節奏 07:27→08:27→09:27 穩定 | 健康（3% fail 待歸因） |
| Trader | eth_s5 + sol_s1 paper running；xrp_s2 已停（07-01）。**兩個運行中策略 equity 皆 10000 整、0 筆交易** | 活著，但見 D2 |
| 部署 drift | server = `f509164`，為本地 HEAD 祖先、**落後 18 commits**（缺 intrabar-audit 全系列）；與 origin 亦不同步 | 見 D3 |
| Refresh 路徑 | daily = root cron 跑 `refresh_factors.sh`（job 系統外）；hourly = freshness_scheduler → job runner（系統內） | 雙路徑並存，見 D4 |

> 觀察過程的教訓（自首）：伺服器 `ls` 顯示本地時（UTC+2）、job.json 是 UTC，我一度把「6 分鐘前開始的正常 stage1」誤判成「卡 2 小時的 zombie」。人會犯的錯，自主 agent 更會犯 → 直接催生 D1 的 UTC 統一健康端點提案。

### D1. 缺單一 machine-readable 健康/結果彙總（Talos 的眼睛）

- **環節**：跨 `runs/pipeline_jobs/`、`research/manifests/*/manifest.json`、`selection.json`、`runs/testnet/*/`、git 版本。
- **現況問題**：「這輪 pipeline 跑完結論是什麼？線上現在健康嗎？」要讀 4+ 種分散 artifact，且時間戳 UTC/本地混用。人讀費勁，Talos 讀 = 每次燒 token 重建現場、還可能像我一樣誤判。
- **怎麼優化**：
  1. **`pipeline_summary.json`**：stage5 收尾時 emit 單檔（每策略一行：verdict / gate / OOS 指標 / 缺哪些驗證），全 UTC。
  2. **`ops_health.json`**：dashboard server 定期聚合（factor 新鮮度、job 佇列狀態、trader heartbeat、**部署 git hash**），全 UTC。
- **影響範圍**：`stage5_select.py` / `emit_manifest.py`、`dashboard/server/main.py`（或獨立小 writer）、schema + 單測。
- **風險**：低（純新增產物）。
- **優先序**：**P1**（Talos Phase 2 的直接輸入；沒有它，Talos 每個決策都要自己爬檔案）。

### D2. Trader 缺「預期 vs 實際」監控（alpha-decay 監控的地基）

- **環節**：`dashboard/trader/loop.py` 狀態輸出。
- **現況問題**：eth_s5 上線近一個月 **0 筆交易**、sol_s1 亦 0 筆。可能完全正常（低頻策略 + regime mask），也可能是壞掉（例如 regime 檔 stale 導致永遠 mask、或因子分佈漂移讓條件永不觸發）—— **現在沒有任何機制區分這兩者**。killswitch 只管虧損，不管「沉默」。
- **怎麼優化**（拆兩段，agy 二審後加急前段）：
  1. ~~即刻一次性診斷~~ **已執行（2026-07-04），確診：結構性壞死** —— 兩個 paper trader 自部署起數學上不可能成交。因果鏈：策略用 `percentile_90d`（rolling 2160 根、min_periods 1080）→ engine 先把因子 reindex 到 ohlcv 索引再 rolling → live trader 只餵 200 根 K（loop 預設，manager 不傳 lookback）→ percentile 全 NaN → 條件全 False → 訊號恆 0。回測餵數年資料所以沒事 = backtest-live parity 缺口。regime/factor 檔皆新鮮、迴圈心跳正常 —— 全部現有監控都看不到「該交易而未交易」。完整證據鏈與修復方案（F1 分頁抓 K + F3 fail-loud guard 建議立即做；F2 engine 重排併入 A1 重驗）見 `docs/talos/d2-diagnosis-2026-07-04.md`。**修復本身為新 P0 項（P0-2）**，等核准。
  2. **常設監控（寫 code）**：trader 每 bar 記錄訊號值分佈摘要；status 加 `expected_trades_per_30d`（從 manifest.backtest 導出）vs `actual_trades_30d`，偏離（如 P(observed|expected)<5%）發 warning alert。順手把「regime 檔案 age」也納入 stale 檢查（現在只查 factor parquet，`regime_<sym>.json` 沒查 —— 而 regime overlay 正是 eth_s5 的 alpha 來源）。
- **影響範圍**：診斷段 = 唯讀觀察；監控段 = `trader/loop.py`、`trader/signal.py`、`freshness.py`、schema、單測。
- **風險**：低。
- **優先序**：診斷段**即刻**（下一步就做）；監控段 **P1**。此監控直接演化成 Talos Phase 2+ 的因子衰減監測。

### D3. 部署 drift 無偵測

- **環節**：server 部署流程。
- **現況問題**：server 落後 18 commits（含 intrabar-audit 全系列 —— 即 server 上的 gate 少一種驗證）。無任何地方記錄「線上跑的是哪個 commit」，dashboard 也不顯示。研究結論（本地新碼）與線上行為（舊碼）可能靜默分歧。
- **怎麼優化**：部署腳本寫 `version.json`（git hash + 部署時間, UTC）到 repo 部署目錄；`ops_health.json`（D1）納入；dashboard 頁腳顯示。Talos 憲法加：**版本不明或落後超過閾值 → 不做 promote 推薦**。
- **影響範圍**：部署腳本 + D1 的 health writer。
- **風險**：無。
- **優先序**：**P1**（一小時工作量）。

### D4. Refresh 雙路徑並存（cron 繞過 job 系統）

- **環節**：root cron `refresh_factors.sh`（daily 全符號）vs freshness_scheduler→job runner（hourly 單符號）。
- **現況問題**：cron 路徑無 job.json、無 log 聚合、無 cancel 能力、失敗只能翻 cron mail；且與 job runner 理論上可同時寫同一 parquet（atomic write 保護了讀者，但兩個寫者互相覆蓋仍浪費且難追因）。這也是伺服器上檔案權限兩種模式的來源之一。
- **怎麼優化**：daily 全符號 refresh 改為 scheduler 每日 enqueue 一個 `live_refresh`（全符號版）job —— cron 只留一行 `curl` 或乾脆讓 freshness_scheduler 帶 daily tick。單一執行路徑 = 單一觀測面。
- **影響範圍**：`freshness_scheduler.py`（+daily 邏輯）、刪 cron 條目（**部署動作，需使用者執行**，我不碰伺服器）。
- **風險**：低；切換期間保留 cron 一週做 fallback。
- **優先序**：**P2**。

### D5. Promote 流程半自動（Talos promote 推薦的落地介面缺口）

- **環節**：`supervisor.start()` / dashboard promote API。
- **現況問題**：伺服器上 sol/xrp 的 `control.json` 帶手工調的 `env` overlay（KILL_PAUSE_DD、FACTOR_MAX_AGE_DAYS…）與手算的 `qty` —— trader manager 支援這 schema，但 **supervisor API 寫不出來**（不含 env），等於正式介面缺了實務上必用的欄位；qty sizing（名目金額 notional → 合約數）靠人腦。
- **怎麼優化**：promote API/`supervisor.start()` 增 `env: dict` 與 sizing 參數；sizing 從 manifest（size_mult、目標 notional、幣價）確定性推導，人只按確認。
- **影響範圍**：`supervisor.py`、`main.py` API、前端表單、單測。
- **風險**：低。
- **優先序**：**P1**（Talos Phase 2 的 promote 推薦要能落地成一個結構化 artifact，這是它的寫入格式）。

---

## 5. 優先序總表

| # | 提案 | 軸 | 優先序 | 一句話 |
|---|---|---|---|---|
| **D2-fix** | live trader lookback 200<1080 致訊號恆 0（F1 分頁抓 K + F3 fail-loud） | 自動化/實盤 | **P0 → ✅已實作（本地，89 tests 綠；待部署）** | 兩個 paper trader 自部署起無法成交 |
| A1 | 回測成本模型三重失真（funding 歷史序列 / 雙邊 taker / config fees 接線） | 效度 | **P0** | 回測數字可信度的根 |
| A2 | 終極 holdout（強制）+ 全漏斗研究記帳 ledger + family-wise 透明標示 | 效度 | P1（Talos 自動迭代前 = P0） | 「OOS 為權威」的機器可執行化 |
| A3 | 回測窗口固定錨點 | 效度 | P1 | A/B 對比去噪 |
| C2 | 引擎 golden-run 合約測試 | 維護 | P1 | 動 A1 前的安全網，半天工 |
| B1 | stage0a 增量抓取/計算 | 效能 | P1 | 2.3h→分鐘級；nightly Foundry 的算力前提 |
| D1 | pipeline_summary.json + ops_health.json（全 UTC） | 自動化 | P1 | Talos 的眼睛 |
| D2 | trader 預期 vs 實際交易率監控 + regime 檔 stale 檢查（一次性診斷即刻先行） | 自動化 | 診斷即刻；監控 P1 | 區分「沉默=正常」vs「沉默=壞掉」 |
| D3 | 部署 version beacon | 自動化 | P1 | 一小時工，堵 18-commit 級 drift |
| D5 | promote API 補 env/qty 參數化 | 自動化 | P1 | Talos promote 推薦的落地介面 |
| A4 | sub-hour 兩地雷（compiler *24、stage4 manifests dir） | 效度 | P2 | 現無害；重啟盤中研究前必修 |
| A5 | IC 非重疊/Newey-West 校正 | 效度 | P2 | 併入 Foundry 守門 |
| B2 | 停用策略不再 hourly refresh | 效能 | P2 | 省 5-8h/天等效算力 |
| B3 | runs/ retention | 效能 | P2 | 防患 |
| C1 | 巨檔拆分（先 stage3） | 維護 | P2 | LLM 可讀性 + 測試隔離 |
| C3 | mkstemp chmod 644 | 維護 | P2 | 一行修 |
| D4 | refresh 單一路徑化 | 自動化 | P2 | 單一觀測面 |

**建議施工順序**（若全採納；D2-診斷已完成並確診）：**D2-fix（F1+F3，修活 paper trader）** → C2（安全網）→ A1（重跑 baseline，F2 併入）→ D3+D1（觀測）→ A2+A3（效度）→ D2-監控+D5（trader/promote）→ B1（效能）→ 其餘 P2。
其中 C2/D3/C3 極小，可夾帶；A1 是唯一會**改變歷史結論數字**的項，需使用者確認後才動。

### 二次意見（agy/Gemini 對抗式審查）處置紀錄

- **採納**：A2 終極 holdout 升為主要強制機制、family-wise 降為透明標示（sequential peeking 非平行試驗，DSR 加 trial 數僅 heuristic）；新增全漏斗 trial registry（stage0a 因子篩選的 data snooping 原本沒被任何 DSR 計入）；A3 改固定窗長+凍結窗口（原固定錨點會讓窗長漂移）；A1 補 funding 序列 PIT 對齊警告；B1 補 TTL+一鍵重建閥；D2 拆出即刻診斷段並提前到施工順序之首。
- **駁回（agy 無 code 可見性所致）**：「滑價全缺」—— 引擎已有雙邊固定 5bps；「強平/保證金全缺」—— 引擎已有 liquidation check + funding 侵蝕，且研究一律 1x 槓桿；「trader 已癱瘓」—— equity 心跳每小時正常寫入，0 筆交易是待診斷、不是已判死。

---

## 6. 與 Talos 的關係（為什麼這些排在 Part B/C 之前值得做）

- A1/A2/A3 = Talos 憲法「OOS 為權威」「回測必含 funding」的**可信度地基** —— 大腦再聰明，餵它失真數字就是自動化地產生錯誤決策。
- D1/D3 = Talos 的感知層：沒有 machine-readable、UTC 統一的現場快照，每個自主決策都從「爬檔案+猜」開始（本次審查我自己就差點因時區誤判 —— 這是實證）。
- B1 = nightly Factor Foundry 的預算可行性。
- D2 = Phase 2+ alpha-decay 監控的最小內核，先以告警形式存在。
- D5 = Talos「推薦進 paper」輸出的落地格式。

---

*報告完。等使用者挑選項目後才進入實作；下一步（Part B Talos 設計文件）亦按 §8 檢查點等核准。*
