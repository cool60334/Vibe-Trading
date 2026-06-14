# 15m / 30m 盤中 K 線策略 Pipeline — 設計文件

- **日期**: 2026-06-14
- **狀態**: 設計定案，待寫實作計畫
- **作者**: Eric（brainstorming with Claude）
- **追蹤**: backlog `backlog_pipeline_minor_cleanups.md` 的 C1（多時間級別 horizon→bar 重構）

---

## 1. 背景與問題

現有研究 pipeline（stage0a → stage5）整條綁死 1 小時 K 線：

- `research_config.yaml`：`interval: "1H"`、`horizons_h` 單位是「小時」、`engine: "daily"`
- 數據層 `fetch_candles(bar=...)` 已參數化，OKX 支援 `"15m"` / `"30m"`（**無需改**）
- 年化 `calc_bars_per_year(interval, source)` 已內建 15m/30m 對照（**無需改**）
- 核心耦合 bug：`add_forward_returns` 用 `shift(-h)` 直接位移 `h`，隱含「1 bar = 1 小時」。換到 15m bar，`ret_24h` 會變成 24 bars × 15m = 6 小時，前向報酬全錯。

目前唯一有效的 alpha 來自 **funding_z（8h 結算）** 與 **stablecoin 供給（日級）**——都是慢速宏觀/部位因子。把它們重採樣到 15m bar 不會生出新 alpha，只多噪音與 funding 拖累。因此本案的真正目的是**引入新的盤中（intraday）因子家族**，而非把舊慢因子搬到細 bar。

## 2. 已定案的決策（brainstorming 三問）

1. **目標**：找新的盤中因子（最大範圍）。慢因子保留但不是重點。
2. **因子家族**：只做 **OHLCV 衍生**因子。order-flow / 微結構（盤口失衡、CVD、taker buy/sell、OI delta）因公開 REST 拿不到歷史 tick/盤口 → 無法回測，**排除**。
3. **timeframe 選擇**：**Per-run env**（新增 `RESEARCH_INTERVAL`，仿現有 `RESEARCH_ONLY_SYMBOL`）。一趟 pipeline 跑一個 timeframe；15m / 30m 各跑一趟；現有 1H 策略完全不動。
4. **實作哲學**：方案 1 — 輕量轉換 helper（非 BarSpec 抽象、非獨立 pipeline）。
5. **分階段**：Phase 1 基建先、Phase 2 因子後。
6. **Phase 2 首批 symbol**：ETH + BTC。

## 3. 目標 / 非目標

**目標**
- pipeline 能以 15m、30m 正確執行（前向報酬、年化、funding 全部 bar-correct）。
- 新增一組可回測的盤中 OHLCV 因子，走現有 deterministic discovery 自動評估。
- 現有 1H / 日級策略（eth_s5 等）零回歸。

**非目標**
- 不引入 order-flow / 微結構 / 逐筆數據。
- 不做即時（live）盤中下單改動——本案只到研究/回測。
- 不重構成多 interval 同跑（`interval: [..]`）或 BarSpec 抽象。
- 不碰 4H / 1m（helper 設計上可擴，但本案不交付）。

## 4. 架構：核心耦合點與改法

| # | 位置 | 現況（綁 1H） | 改法 |
|---|------|--------------|------|
| 1 | `research/lib/factor_metrics.py:28` `add_forward_returns` | `out[f"ret_{h}h"] = close.shift(-h)/close - 1` 假設 1 bar = 1 小時 | `shift(-(h * bars_per_hour))`；欄名 `ret_{h}h` 維持（單位仍是小時，時間錨穩定） |
| 2 | 年化 `agent/backtest/metrics.py` `calc_bars_per_year` | 已內建 15m/30m × okx/ccxt | 不改邏輯，只確認 runner 把 `cfg.interval` 串到底 |
| 3 | Funding `agent/backtest/engines/_market_hooks.py:148-194` `calc_crypto_funding_fee` | hour ∈ {0,8,16} 觸發，`else` 分支每日 fallback 一次 | sub-hour 下確認只在 00:00/08:00/16:00 bar 結算（不可每 bar、不可 4×）；`else` 日級 fallback 僅 1D bar 該觸發 → 修 gating；確認真實 funding_rate 有 ffill 到 sub-hour index |
| 4 | 因子視窗 `research/lib/indicators.py` `compute_indicator_pool` | `rsi_14` = 14 bars（1H 下 = 14h） | 盤中因子用 **bar-count 原生語意**（盤中因子本就以 bar 定義，正確）；慢因子靠 `apply_ic_eval_transform`（funding 原生 8h、stablecoin 日級重採樣）維持時間錨 |
| 5 | config 單一全域 `interval` | 寫死 1H | 新增 `RESEARCH_INTERVAL` env override；artifact 以 interval 命名空間隔開 |

**單一真相源 helper**

新增 `bars_per_hour(interval: str) -> int`（建議放 `research/lib/`，或與 `calc_bars_per_year` 同檔對齊）：

```
"15m" -> 4, "30m" -> 2, "1H" -> 1
```

本案僅交付 15m / 30m / 1H 三鍵，皆 ≥1 整數（小時 → bar 為整數倍，避開分數 bar）。4H / 1D 不在範圍——若日後要加，再決定分數 bar 或反向 `hours_per_bar` 的設計。所有「小時 → bar」轉換都呼叫此 helper，集中收斂。

`interval` 命名規範：沿用 OKX bar 字串（`"15m"`、`"30m"` 小寫 m、`"1H"` 大寫 H），與 `calc_bars_per_year` 的 `_BARS_PER_DAY` 鍵一致。`RESEARCH_INTERVAL` 必須驗證 ∈ {15m, 30m, 1H}，否則 `ValueError` 列出合法值（仿 `_apply_symbol_filter` 的 unknown-symbol 處理）。

## 5. Phase 1 — Timeframe 基建（讓 15m/30m 正確且可跑）

目標：不加任何新因子，只讓既有管線在 15m/30m 下數學正確、可執行。以 **null-result smoke** 結尾證明機器對。

1. **`bars_per_hour(interval)` helper + 單測**（涵蓋 15m/30m/1H；未知 interval 報錯）。
2. **修 `add_forward_returns`**：小時 → bar 轉換；補測試證明 1H 行為不變、15m 下 `ret_24h` = `shift(-96)`。
3. **`RESEARCH_INTERVAL` env override**：
   - `research/pipeline/config.py` 加 `_apply_interval_override`（仿 `_apply_symbol_filter`），由 `load_config` 套用；驗證合法值。
   - `dashboard/server/pipeline_manager.py:41-46` 每步注入/清除 `RESEARCH_INTERVAL`（仿 `RESEARCH_ONLY_SYMBOL`）。stage 5 / 2b 等不需 interval 的步驟比照既有 symbol 處理慣例。
4. **Funding sub-hour 正確性**：
   - 先釐清「真實 funding」實際走哪條路徑（engine `on_bar` 的 scalar `funding_rate` vs 研究端向量化 merge 真實 funding history；memory 有「向量版漏算 funding 灌水」與 `eth_s5_real_funding_recompute.py` 線索）。
   - 確認 sub-hour bar 下每 8h 只結算一次、金額不因 bar 變細而放大。
   - 修 `else` 日級 fallback 的 gating：僅當 bar 級距 ≥ 1D（無 intraday 時戳）才該觸發；sub-hour/1H 連續資料含 00/08/16 bar 時不可額外加一次。
   - 真實 funding_rate 序列 ffill 到 sub-hour candle index。
5. **重採樣存活驗證**：`apply_ic_eval_transform`（`research/pipeline/stage0a_features.py:147`）與 `research/factor_regime.py` / `research/lib/regime.py` 的重採樣在 sub-hour index 下不爆、結果合理（funding 仍 8h、stablecoin 仍日級）。
6. **Artifact 命名空間**：15m / 30m / 1H 的 evidence manifest、candidates、runs、strategy_runs 等輸出不互相覆蓋。決定命名規則（建議 artifact 路徑帶 interval 後綴或子目錄），更新讀寫兩端。
7. **Smoke 驗收**：`RESEARCH_INTERVAL=15m RESEARCH_ONLY_SYMBOL=eth` 跑 stage0a → stage1，用**現有** indicator pool。期望：管線通、`ret_{h}h` 欄正確、年化用 35040 bars/年；慢因子在短 horizon ≈ 0 IC（正確 null 結果，證明 wiring 對，不是證明有 alpha）。

**Phase 1 完成定義**：15m/30m 能跑完 stage0a→1 不崩、數學經單測與 smoke 驗證、現有 1H 測試全綠。

## 6. Phase 2 — 盤中因子庫 + 發掘

1. **擴 `compute_indicator_pool` + `indicator_pool` config**，加盤中 OHLCV 因子（bar-count 原生）：
   - 短期動量 / 反轉：N-bar 報酬、N-bar RSI（短窗）
   - 已實現波動突破：rolling realized vol 的 z-score / breakout
   - 量能：volume z-score、量能脈衝
   - 區間 / ATR 擴張：range expansion、ATR ratio
   - 時段效應：hour-of-day（UTC）週期 dummy / 季節項
   - 視窗用適合 15m/30m 的 bar 數（如 4/8/16/32 bars），非沿用日級的 14/20/30。
2. **config 加短 horizons**：如 `horizons_h: [1, 2, 4, 8, ...]`，讓盤中因子在短 horizon 有 IC、慢因子在長 horizon 有 IC，單一 pool 兼容兩者（前向報酬 helper 已保證任何 bar 正確）。
3. **跑全 stage0a → stage5** @15m 與 @30m，symbol = ETH + BTC，讓 deterministic discovery（`select_candidates_from_evidence`）自動撈出有 IC 的盤中因子，後續 stage 照常 sweep / backtest / select。
4. **Dashboard**：視需要加 interval 維度（策略徽章 / 過濾器），讓 15m/30m/1H 策略在 UI 可分辨。non-blocking，依 Phase 2 實跑結果再定。

**為何分兩階段**：Phase 1 先用 null-result smoke 證明 timeframe 機器數學正確；基建錯則因子全白做。Phase 2 才投入因子研究。

## 7. 數據考量

- **資料量**：15m × N 年 ≈ 96 bars/日 × 365 × N。`fetch_candles` 每頁 100 bar、`sleep 0.15s`。15m × 2 年 ≈ 70k bar ≈ 700 頁 ≈ ~105s/symbol（純抓 K 線）。可接受但慢。
- **歷史跨度**：盤中因子每日樣本多，**不需**像日級那樣長日曆跨度。建議預設 15m/30m 用較短跨度（如 1–2 年），由 `period`（天）控制；統計顯著性靠 bar 數而非年數。
- **funding 結算對齊**：funding 小時 0/8/16 的 minute=0，15m（96/日）與 30m（48/日）邊界都精準落點，無對不齊問題。
- **parquet 體積**：sub-hour 檔案明顯變大；artifact 命名空間隔離後，注意磁碟與（部署時）gitignored runtime data 上傳成本（參 `deploy_dashboard_testnet_runbook`）。

## 8. 測試策略

- **research 與 dashboard 的 pytest 必須分開跑**（既有慣例，見 `backlog_pipeline_minor_cleanups`）。
- 新增單測：`bars_per_hour`、`add_forward_returns`（1H 不回歸 + 15m 正確）、`load_config` 的 `RESEARCH_INTERVAL` override（合法 / 非法值）、funding sub-hour cadence。
- 既有測試（1H 路徑）全綠 = 零回歸的硬門檻。
- Phase 1 smoke 為手動驗收（非 CI），列指令於 PR 描述。

## 9. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 「小時→bar」轉換散落、漏改某處 | 全部走單一 `bars_per_hour` helper；grep `shift(-` / `* 24` / `/ 24` / 硬編碼 bar 數做稽核 |
| funding 在 sub-hour 重複計或放大 | Phase 1 task 4 先釐清真實 funding 路徑，cadence 單測釘死 8h 一次 |
| 慢因子重採樣在 sub-hour index 行為改變 | task 5 明確驗證 funding 仍 8h、stablecoin 仍日級 |
| 盤中因子全是噪音、無 alpha | 預期可能發生；deterministic discovery 的 IC 門檻會自然濾掉；null 結果也是有效結論（記入 memory） |
| artifact 命名衝突弄壞現有 1H 策略 | 命名空間隔離 + 既有 1H 測試全綠把關 |
| 抓 sub-hour 多年資料慢 / API 限流 | 縮短跨度；沿用既有 `call_with_retry` 退避（`project_trader_ratelimit_backoff`）；分 symbol 跑 |

## 10. 不在本案範圍

- order-flow / 微結構 / 逐筆數據因子。
- 多 interval 同跑、BarSpec 抽象、4H / 1m。
- live 盤中下單 / trader 改動。
- 為 sub-hour 重設交易成本模型（沿用既有 config 費率）。
