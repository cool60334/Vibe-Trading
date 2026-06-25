# Massive 跨場域溢價因子 — 設計 (option C)

- **日期**：2026-06-25
- **分支**：quant-trading-dashboard
- **狀態**：設計已批准，待寫 implementation plan
- **二審**：agy (Gemini 3.1 Pro) 已審 order-flow 判決 + 本設計，意見已併入

---

## 1. 背景與動機

研究主線在 2026-06-25 [intraday new-factor scout] 收斂為「盤中 6 類因子全封盤、1H 為地板」。
評估 Massive (原 Polygon.io) 作為新資料源時，確認三件事：

1. Massive crypto = **純現貨**，聚合 Coinbase / Bitfinex / Bitstamp / Kraken，**Binance 2021 起不在內**，
   **無永續 → 無 funding / OI / basis**。其盤中 tick / TA 指標對應到已測死的因子類，crypto 盤中不採用。
2. **修正既有認知**：現有 `basis_rel` = `(OKX 永續 close − OKX 現貨 close) / OKX 現貨`
   （`research/lib/derived_factors.py:16`、`research/pipeline/stage0a_features.py:551-561`），
   兩腿都是 OKX、都以 **USDT** 計價。它**已是真現貨基差，不是被平滑的 index**。
   因此「用乾淨現貨去平滑化」的原始動機前提錯誤。
3. Massive 的真正增量 = **真美元 (USD) 現貨**，與 OKX 的 **USDT** 現貨相減，露出兩個現有因子沒有的維度：
   - **USDT 脫鉤溢價**（USDT vs 真美元；連結既有 stablecoin 因子家族）。
   - **美國法幣溢價 / Coinbase premium**（美國法幣入金口 vs 離岸）。

→ option C 的因子不是「換個現貨源算同一個 basis」（那與 `basis_rel` 高度共線、無用），
而是**隔離出跨場域溢價**，大機率正交既有因子。

### agy 二審納入的關鍵修正
- **訊號 sign 衝突**：USDT 脫鉤推溢價為正、Coinbase 爆買推為負，兩驅動相反，揉一個 z-score 會自我抵銷
  → **拆成兩個因子**，並以脫鉤率去混淆 (deconfound) 純法幣溢價。
- **正交防錯對象**：真正共線的是 `funding_z` + `basis_rel`（同批壓力事件三者齊飛），不是
  `stablecoin_supply_z`（低頻 mint/burn）→ kill-gate 控制變數改為 funding_z + basis_rel。
- **1H 解析度 / 可交易性**：跨所價差套利半衰以分鐘計 → 快價差版本大機率像 order-flow 一樣死於 entry-lag；
  只有慢 regime（脫鉤 / 溢價狀態持續數小時–天）可能存活 → **entry-lag 審計設為 Phase 1 決勝閘**。
- **成本檢查前移**：turnover（翻轉率）+ 滑點壓測從 Phase 2 提前到 Phase 1 gate。
- **timestamp 對齊**：跨源算 spread，bar 邊界差幾秒即引入 lookahead / 微噪音 → 列為明確 data hazard。

---

## 2. 目標 / 非目標

### 目標
- 新增**隔離的跨場域溢價因子**（拆 USDT 脫鉤 + 純法幣溢價兩支），跑既有 stage0a→1 量測其 alpha。
- 以**便宜、嚴格的 Phase 1 kill-gate** 快速判 GO / No-Go，符合既有研究紀律。

### 非目標 (YAGNI)
- **不碰** tick / BBO / order-book / WebSocket。**只用 1H aggregate bar**（免費級 5 rpm 足夠）。
- **不取代** `basis_rel`；並排新增，evidence 決定去留。
- Phase 1 **只 BTC / ETH / SOL**（Coinbase / Kraken 覆蓋最佳）。擴充幣 + BNB 覆蓋問題延後到 Phase 2。
- Phase 2（live 保鮮 + 自動建策略 + 部署）**只在 Phase 1 過閘後才設計**。

---

## 3. 因子定義

令：
- `P_okx` = OKX `BTC-USDT` 現貨 close（USDT 計價，既有 `spot_close`）。
- `P_usd` = Massive `X:BTCUSD` 現貨 close（真 USD）。
- `R` = USDT/USD 匯率（1 USDT 值多少 USD）。來源優先序：Massive `X:USDTUSD` →
  否則 ccxt（Kraken / Coinbase `USDT/USD`，與 Massive 無關，**移除對 Massive stablecoin 覆蓋的硬依賴**）。

兩個因子：

```
# 1) 純 USDT 脫鉤
depeg          = R - 1
depeg_z        = rolling_z(depeg, 30d × bars_per_hour)          # category = "stablecoin"

# 2) 脫鉤校正後的純跨場域 / 法幣溢價
fiat_prem      = (P_okx * R) / P_usd - 1                        # 把 OKX 的 USDT 報價換回 USD，脫鉤成分抵消
fiat_prem_z    = rolling_z(fiat_prem, 30d × bars_per_hour)      # category = "basis"
```

- z-score 視窗沿用既有慣例 `SCREEN_ZSCORE_DAYS * 24`（見 `derived_factors.py` 的 `_rolling_z`）。
- 方向 (sign) 不預設，z-score 後由 stage1 evidence IC 決定多空。
- 兩因子分開測；可能一支有料一支沒有，或都沒有。
- **類別選擇**：`depeg_z → stablecoin`、`fiat_prem_z → basis`，兩者皆屬 `factor_selector._ALLOWED_CATEGORIES`
  既有成員（`research/pipeline/lib/factor_selector.py:65`）→ **factor_selector 無需改動**。

---

## 4. 架構與元件

| # | 元件 | 檔案 | 職責 |
|---|------|------|------|
| 1 | 資料源 | `research/lib/massive_data.py`（新） | `fetch_spot_bars(usd_ticker, days, interval) -> pd.DataFrame \| None`。打 Massive REST aggregates（`X:BTCUSD` 1H bar）。`.env` 讀 `MASSIVE_API_KEY`；429 指數退避（仿 `loop.py call_with_retry`）；網路重試；缺覆蓋 / 失敗 → 回 `None`。回傳對齊既有 `okx_data.fetch_candles` 形狀（UTC DatetimeIndex + OHLCV）。`next_url` 分頁（1H × 2yr ≈ 17.5k rows < 50k，單請求即足，分頁僅防呆）。 |
| 2 | 因子 | `research/lib/derived_factors.py`（擴） | 新函式 `cross_venue_premium_factors(okx_spot, massive_usd, usdt_usd) -> dict`，回 `{depeg, depeg_z, fiat_prem, fiat_prem_z}`。ffill 對齊、NaN 處理、截斷穩定（仿既有 basis 測試 `test_derived_factors.py:62`）。 |
| 3 | 接線 | `research/pipeline/stage0a_features.py`（擴） | 既有 spot fetch（`:551-564`）後加：(a) Massive USD fetch，(b) USDT/USD 匯率 fetch，皆 try/except → None。傳入 `build_feature_dict`（新增選用 kwarg `massive_usd_close`、`usdt_usd_close`，預設 None）。在 `build_feature_dict` 內，三者皆非 None 才 `features.update(cross_venue_premium_factors(...))`，守衛同 `:291`。**失敗 → 因子缺席 → 1H 主線不受影響**（同 orderflow / oi pattern `:569-594`）。 |
| 4 | 符號映射 + registry | 符號 config dataclass + `research/lib/sources.py` | config 加選用欄 `massive_usd: str \| None = None`（btc→`X:BTCUSD`）；無覆蓋（BNB 大概沒）留空 → 因子自動跳過。`sources.py` `SOURCE_REGISTRY` 加 `massive_spot_usd` 條目（純加法驗證）。 |

stage0 / stage1 / evidence / discovery **零碼改** —— 既有機制自動撈新因子 IC（前提：feature_key 寫進 feature store，見 `factor_selector.py:223`）。

---

## 5. 資料風險 (data hazards)

1. **timestamp 對齊（最高風險）**：OKX 與 Massive 是不同來源，1H bar 的時間戳標籤慣例（open vs close、UTC）必須一致。
   - 對策：兩源都正規化到 **UTC bar 開盤邊界**；建因子前 assert 兩 index 對齊；ffill 僅在確認同邊界後做。
   - 加 lookahead 檢查：確保 `fiat_prem[t]` 只用 ≤ t 的資料（無未來 bar 洩漏）。
2. **免費級 15 分鐘延遲**：對 1H bar 回測 + 每小時 live 皆無妨；僅當未來轉盤中才有影響（本設計不轉）。
3. **聚合 vs 純 Coinbase**（agy #6a，本設計微調）：Massive aggregate 含 Bitfinex / Kraken 噪音。
   單所 bar 需拆 tagged tick（破壞免費級簡單性），且 aggregate 按量加權本就 Coinbase 為主。
   → **Phase 1 先用 aggregate**，記下噪音 caveat；IC 邊際才退而抓 Coinbase-tagged。

---

## 6. Phase 1 kill-gate（純回測，免費級，不碰 tick）

### 步驟
1. TDD 建元件 1–4（用錄製 fixture，**不需 API key**）。
2. 取得免費 key → 抓 BTC / ETH / SOL 的 USD 現貨 1H ~2yr + USDT/USD 匯率。
3. **備份 `research/manifests/{btc,eth,sol}` 的 evidence/manifest**（前例 `manifests/_bak_pre_derived/`）後，
   跑 `stage0a→1`（measurement 模式，無策略部署）。
4. 讀 evidence，套下列**全部** gate。

### Gate 條件（全過才 GO Phase 2）
- **(a) 絕對 IC**：`|IC@top|` 過既有 evidence 門檻（`min_abs_ic` / `min_abs_ir`，`factor_selector.py:213-214`）。
- **(b) partial IC（正交性）**：對未來收益迴歸，**強制控制 `funding_z` + `basis_rel`**，新因子 beta 仍顯著
  → 證明非 funding/basis 換皮。（不只看絕對 IC。）
- **(c) ⭐ entry-lag 審計（決勝閘）**：lag0（訊號收盤進）vs lag1（下一根進）IC。
  晚進一根 IC 塌 → 快價差、死（同 order-flow [[project_orderflow_poc_results]]）；
  不塌 → 慢 regime、可交易。
- **(d) turnover / 自相關**：因子翻轉率；翻太快 → 手續費吃光。
- **(e) 滑點壓測**：扣單邊 5–10 bps 後 incremental IC 是否歸零 → 歸零即 No-Go，不進 Phase 2。
- **(f) quantile 單一事件查**：IC 是否只靠 2–3 天脫鉤撐（仿 `orderflow_eval`）。

### 判決
- 全過 → Phase 2。
- 任一失敗 → **記負面結果進 memory，收手**（符合既有 No-Go 紀律）。

---

## 7. Phase 2（延後概要，只在 Phase 1 過才設計）

- **live 保鮮**：每小時 Massive USD + USDT/USD fetch 接既有 `live_refresh` scheduler（免費級 15min 延遲對 1H 足夠）。
- **自動建策略**：archetype router 純確定性建策略（無碼，見 [[project_positioning_strategy_autobuild]]）→ stage2–5。
- **promote 閘**：OOS sharpe ≥ 1 / trades ≥ 30 / DD < 15% / cost-stress > 0（既有）。
- **deploy-freshness 閘**：部署前確認因子能 live 保鮮（[[project_coin_expansion_triage]]）。

---

## 8. 對既有流程的衝擊（已查 code）

**Phase 1 接近零破壞，純加法。** 唯一真影響見下。

| 改動 | 衝擊 |
|------|------|
| 新檔 / 加函式 / 加選用 kwarg / 加 config 選用欄 / 加 registry 條目 | 全非破壞（預設 None、守衛、既有 caller 用 keyword） |
| `build_feature_dict` 既有 caller + 測試 | 不受影響（kwarg 預設 None，不傳就沒事） |
| feature store 寫入 | 加欄；既有欄同資料 → 數值不變；atomic 寫（commit 279c051） |

**唯一真影響**：跑 Phase 1 discovery 會重生 BTC/ETH/SOL 的 evidence/feature-store。
`factor_selector` 是**全域按 |IC| 搶前 6**（`:243` sort、`:60` MAX=6），非分類別配額 →
**新因子若 IC 夠強，可能擠掉某既有因子的候選名次**，改變那 3 幣 stage1+ 的候選/manifest。
- **緩解**：Phase 1 = 量測；先備份 manifest 或 observe-only 跑、直接讀 evidence IC。無策略自動上線。

**live 交易零風險**：eth_s5 (live) / sol_s1 (paper-forward) 從已 commit 的 signal_engine.py + control.json 跑，
trader 獨立容器解耦（[[backlog_trader_deploy_decoupling]]）；discovery re-run 不改它們的碼或因子欄（加法）。

---

## 9. 測試 (TDD)

- `test_massive_data.py`（新）：從錄製 JSON fixture 解析 bar（無網路 / 無 key）、符號映射、429 退避、缺覆蓋 → None、`next_url` 分頁。
- `test_derived_factors.py`（擴）：`cross_venue_premium_factors` 數學（depeg / fiat_prem 公式、sign）、ffill 對齊、NaN、截斷穩定、**bar 邊界對齊 assert**。
- `test_stage0a_features.py`（擴）：`build_feature_dict` 給三個 spot 腿時吐出 4 個新 key；缺任一 → 因子缺席、其餘不變。
- 註：research 與 dashboard 的 pytest **分開跑**（[[backlog_pipeline_minor_cleanups]]）。

---

## 10. 風險與開放問題

1. **USDT/USD 匯率來源**：優先 Massive `X:USDTUSD`，否則 ccxt（Kraken / Coinbase）。Phase 1 step 2 確認可得；
   無 `R` 則無法 deconfound（agy #1 未解）→ 退回單一糊溢價並標註。
2. **Massive USD 覆蓋**：BTC/ETH/SOL OK；XRP/DOGE/ADA/LTC/BCH 在 Coinbase/Kraken 應 OK；**BNB 大機率無**（Binance 系）。Phase 1 驗。
3. **死亡機率偏高**：entry-lag 審計很可能判此因子死（如 order-flow）。這是設計本意 —— 便宜、死得明白。
4. **聚合純度**：Bitfinex/Kraken 噪音；IC 邊際才退抓 Coinbase-tagged。
