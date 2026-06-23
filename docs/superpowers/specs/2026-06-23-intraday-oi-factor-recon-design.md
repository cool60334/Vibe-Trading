# 盤中 OI 因子 Recon（Intraday OI Factor Recon）— 設計 v3

- **日期**：2026-06-23
- **分支**：quant-trading-dashboard
- **狀態**：設計已核准（經兩個 subagent 對抗審 + 自驗 reconcile），待寫實作計畫
- **脈絡**：P2（你 3 大目標之一：15m/30m 因子）。**便宜 recon 探 GO/NO-GO，不直接建。** 趁 6 幣 pipeline 在 server 跑的空檔平行做（純研究腳本、不碰 production pipeline）。

---

## 0. 誠實先驗（先講結論可能是死的）

持倉因子是**慢訊號**（1H 預測力峰在 72–168h）。盤中（30m）取樣極可能對 1H **零增量** → recon **大概率 NO_GO**。本 recon 的價值＝用一個決定性數字（incremental IC vs 自身 1H）confirm-and-close，符合本專案「量了才算死」傳統（OHLCV / order-flow 都是測死的，非猜死）。

---

## 1. 背景

目標：找盤中（15m/30m）有真預測力的因子。已知死路：盤中 OHLCV（bid-ask bounce 噪音）、order-flow taker（edge 只在成形那根、maker 抓不到）。funding/穩定幣太慢。

持倉因子（`global_ls_acct_z`/`toptrader_ls_z`/`ls_divergence`/`taker_buysell_ratio`）來自 Binance daily-metrics archive（5-min snapshots），**只在 1H 量過**、隨幣異（global_ls 強 BTC/SOL 死 ETH；ls_divergence 三幣強 SOL −0.10 single-use；toptrader ETH −0.088）。OI 是 5-min native（不像 funding 會攤平）→ 值得一探盤中。

### 雙 AI 審摘要（agent1 批、agent2 驗並推翻 agent1 部分過修）
- live endpoint（`fetch_live_ls_ratios`/`_LIVE_LS_COLS` `oi_metrics.py:165`）**只吐 global_ls + toptrader** 兩欄；OI/oi_usd/taker 無 live sub-hour 源 → 盤中**不可部署**。⇒ deployable 臂只剩 3 個 L/S 因子。
- lag0-vs-lag1 entry-lag gate 對 24-48h 慢 z-score **空轉**（≈1.0 必過）；團隊早改 24h staleness gate（`scripts/phase0_lag_gate.py`）。
- **agent1 兩過修，agent2 用 repo 證據推翻**：「跨幣一致」gate 會**否決 sol_s1**（唯一部署贏家，coin-specific）→ 不用；IC≥0.05 太高（1H 最佳僅 0.02-0.10）→ 維持 ≥0.03；horizon 限 0.5-2h 太窄（峰 72-168h）→ 跑到 ~72h 看峰。
- funding 當正交對照盤中無用 → 改對照「因子自身 1H 值（causal）」+ 短窗報酬。

---

## 2. ⚠️ 承重 data hazard（已用真 cached ZIP 驗證 — 最重要）

Binance OI archive 的 `create_time` **跨年不一致**：
- **標記**：BTCUSDT 2020-2024 = OPEN-labeled（first=00:00:00）；**2025+ = CLOSE-labeled**（first=00:05:00）。
- **cadence**：2020-2021 = **2.5-min**（576 列/日）；2022+ = 5-min（288 列/日）。

1H 下被 `aggregate_to_hourly` 的 resample 洗掉沒事；**15m/30m 會咬**（1-bar mislabel、方向還不一致 + 每 bar 列數變動）→ 盤中 IC 全 corrupt。

**修（recon 第一步、阻塞其餘）**：載 raw → `normalize_create_time(df)`（per-file 偵測：first-ts==午夜→OPEN→平移半個 cadence 或統一改標 close-of-window；==00:05→已是 close）→ 統一成「window-close、known-after-close」戳，再 sub-hour 聚合。

---

## 3. 目標 / 非目標

### 目標
- 對 3 個 live-feed L/S 因子（盤中可部署子集），在 15m/30m 量「盤中是否有對 1H **增量** alpha」，吐 per-coin GO/NO_GO + 證據。
- 決定性核心數字：**incremental IC vs 因子自身 1H 值（causal）**。
- 唯讀、便宜、不碰 production `derived_factors`/`stage0a`。

### 非目標（YAGNI）
- 寫進 production / 建策略 / 跑全 pipeline（等 recon GO 再說）。
- 跨幣一致 gate（持倉 coin-specific，會誤殺真單幣 edge）。
- OI-velocity/taker 當部署候選（無 live feed）；僅作標明「研究 only / 對照」臂。

---

## 4. 設計

### 4.1 因子（interval-correct，窗口 ×`bars_per_hour`）

**Deployable 臂（headline，live-feed 2 欄衍生）**：
- `global_ls_z_s`、`toptrader_ls_z_s` — 短窗 z（24–48h，非 30 天）
- `ls_divergence_s` = global_ls_z_s − toptrader_ls_z_s

**研究 only 臂（標明不可部署、archive-only）**：
- `oi_mom_1h`、`oi_accel`、`oi_price_div`（用 **magnitude** 形 `pct_change·pct_change`，非 sign 形）
- `taker_imbalance` — 純對照（已知死、確認「是否又是 order-flow」）

### 4.2 模組

- `research/lib/oi_metrics.py`（改）：
  - `normalize_create_time(df) -> df`（per-file OPEN/CLOSE 偵測 + cadence 處理，統一 close-of-window）
  - `aggregate_to_interval(df, freq)` — 由 `aggregate_to_hourly` 泛化（snapshot=last / flow=mean、`label="left",closed="left"`、**不 ffill、NaN gap 保留**）；`aggregate_to_hourly` 改成 `freq="1h"` 薄包（零回歸，沿用既有測試）。
- `research/lib/intraday_oi_factors.py`（新）：純 builder，吃 OI frame + close + interval，窗口用 `lib.timeframe.bars_per_hour` 縮放。
- `research/scripts/intraday_oi_recon.py`（新 driver）：給 symbol+interval → 載 raw 5-min（`oi_metrics`）→ normalize → `aggregate_to_interval` → 建因子 + 載 candles（`okx_data.fetch_candles(bar=interval)`）→ 評估 → 寫 `research/manifests/<interval>/intraday_oi_recon_<sym>.json` + 印表。

### 4.3 評估（全部重用 `orderflow_eval`，零重造）

對每因子：
1. **decay profile**：`decay_profile` IC@ {0.5,1,2,4,8,24,48,72}h（縮放成 bars）+ `half_life_bars`。看 IC 在哪 horizon 峰。
2. **核心：incremental IC vs 自身 1H**：`incremental_ic(factor_30m, control=factor_1h_causal, price, fwd_bars)`。`factor_1h_causal` = 同因子在 1H 算出 → **causal ffill**（1H 值 stamped 於 hour H 只在 H 收盤後可知 → 對 30m bar 用「最近**已收盤** 1H bar」值，即 1H 序列先 shift 一根再 reindex/ffill 到 30m）→ 殘差對 fwd return 的 IC。≈0 = 盤中只是 1H 讀更密（死）。
3. **staleness-lag IC**（非 lag0-vs-lag1）：`execution_ic(entry_lag_bars=realistic)`，realistic = live 5-min snapshot 落地後可進的下一根。對照 lag-0 看真實可捕捉度。
4. **正交**：`incremental_ic` vs (a) 自身 1H（同 2）、(b) 短窗報酬（確認非重推死掉的 OHLCV 均值回歸）。

### 4.4 GO / NO_GO 準則（per-coin，常數）

- 真 edge：某 horizon |IC| ≥ **0.03**（block-bootstrap 下界 > 0 為佳）。
- **核心**：incremental IC vs 自身 1H **顯著 > 0**（≈0 → NO_GO，盤中無增量）。
- 過 staleness-lag（realistic entry 後 IC 不崩）。
- 峰 horizon 要落在「盤中說得通」範圍（若只有 72h 峰 → **FAIL**，那是 1H/多日慢訊號變裝）。
- **不要求跨幣一致**；穩健性靠該幣 walk-forward OOS（下游）+ cost-stress。
- GO = 「值得 cost-aware 回測」，**非可部署**（盤中高換手 fee-illusion 風險）。

---

## 5. 測試（TDD）

- `aggregate_to_interval`：`freq="1h"` 結果 == 既有 `aggregate_to_hourly`（回歸）；30m/15m bar 數正確；snapshot=last/flow=mean；**不 ffill、NaN gap 保留**（沿用 measurement-layer 教訓）；log 每 interval NaN-bar 比例。
- `normalize_create_time`：OPEN-labeled 輸入（first 午夜）與 CLOSE-labeled（first 00:05）→ 正規化後同一語義；對真 cached ZIP（BTC 2024 OPEN vs 2025 CLOSE）各一個 assertion。
- factor builder：interval 縮放正確（30m 窗 = 2× bars、已知輸入 IC）。
- causal 1H control：建構不含 lookahead（1H 值在收盤後才對齊到 30m）—— 單測一個刻意 case（若用 containing-hour close 會 leak，用 shifted ffill 不 leak）。
- driver smoke：mock 小資料跑通、輸出 JSON 結構合法。
- `orderflow_eval` 已測，不重測。

> 註：research/tests 與 dashboard/server pytest 分開跑（[[backlog-pipeline-minor-cleanups]]）。本案只動 research/。

---

## 6. 成功標準

跑 `python research/scripts/intraday_oi_recon.py --symbol sol --interval 30m`（btc/eth/sol raw 已本地快取）→

1. 印 per-factor 表：IC@horizons │ 峰 horizon │ half-life │ **incremental-vs-1H** │ staleness-lag 留存 │ 判決。
2. `normalize_create_time` 處理掉跨年標記/cadence、NaN-bar 比例合理（log 出）。
3. 決定性：若 incremental-vs-1H ≈0（預期）→ 明確 NO_GO、盤中持倉封盤；若顯著 >0 → 標 GO 候選待 cost-aware 回測。
4. research 新測全綠、零打真網路（探測/載入 mock 或讀本地快取）。

---

## 7. 已定決策（雙審 reconcile）

- 範圍：便宜 recon、僅 3 個 live-feed L/S 因子為部署候選；OI-velocity/taker 研究 only。
- data hazard：per-file create_time 正規化 + `aggregate_to_interval`（**阻塞第一步**）。
- 核心測試：incremental IC vs 自身 1H（causal）。
- 門檻 ≥0.03（非 0.05）；horizon 掃到 72h 看峰；不要求跨幣一致；正交對照用自身1H+短報酬（非 funding）；lag 用 staleness（非 lag0/1）。
- GO ≠ 可部署（fee-illusion 戒律）。
- 連 [[project_intraday_oi_recon_review]]、[[project_oi_data_source_binance_archive]]、[[project_intraday_ohlcv_class_dead]]、[[project_ls_positioning_recon]]、[[project_sol_paper_forward_phase0]]。
