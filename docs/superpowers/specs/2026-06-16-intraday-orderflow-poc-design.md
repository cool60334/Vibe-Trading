# Intraday Order-Flow 因子 POC — Design Spec

- **Date**: 2026-06-16
- **Status**: Approved (brainstorming), pending implementation plan
- **Scope**: 單一 POC — 證明 Binance aggTrades 衍生的 intraday order-flow 因子在 ETH @ 30m 有無 IC。**不含** robust 多幣 ingestion pipeline、不含 live trading、不含 L2 盤口。
- **Branch**: quant-trading-dashboard

## 1. 動機

幾輪 intraday 研究結論固定:30m/15m 用**現有因子**榨不出 alpha。原因是結構性的:

- intraday 新增的因子**只有 OHLCV 衍生那批 → 已證是噪音**(mom_4/8/16 弱反向 IC≈−0.05,其餘噪音)。
- 慢因子(funding 8h native、stablecoin 日頻)切到 30m **IC 不變**(IC eval 用原生頻率算),被 ffill 灌水。
- 30m ETH 唯一存活因子 = `funding_z`(IC@24h −0.062,ensemble_only,薄)。

**突破口**:真正的 intraday-native 因子需要微結構資料(逐筆成交 / 盤口 / 爆倉)。其中**逐筆成交可從 Binance 免費公開 dump(data.binance.vision aggTrades)取得**,不撞資料牆。order-flow 因子從 bar 內成交直接算,**天生對齊、零 ffill** → 切到 30m/15m 真的會變,解開慢因子困境。

本 POC 目標:用最低成本驗證「intraday order-flow 因子到底有沒有 IC」,有則上 production / 考慮付費 L2,無則收手。

### 決策(brainstorming 鎖定)

| 維度 | 決定 |
|---|---|
| 資料源 | Binance 免費公開 dump(aggTrades 逐筆),零成本起手 |
| 範圍 | POC 先驗 IC,**不**直接建完整 pipeline |
| 目標 | ETH @ 30m,近 12 個月 |
| 整合 | 重用現有 stage0a/stage1 評估機器(方案 A) |
| 成功判準 | 沿用 stage1 verdict gate:任一因子 `|IC| ≥ 0.03` 且 cross-regime 不翻號 → go;全 `reject` → no-go |

## 2. 架構與資料流

```
┌─ 一次性 ingestion (新, 重) ──────────────────────────────┐
│ 1. downloader: 抓 data.binance.vision ETHUSDT aggTrades   │
│    月檔 (近 12 月, .zip CSV) + 驗 CHECKSUM, idempotent     │
│ 2. aggregator (Polars, chunked): 逐筆 → [T,T+30m) 左閉右開  │
│    bucket → bar-level order-flow 特徵                       │
│ 3. 落快取: research/data/orderflow/of_eth_30m_v1.parquet   │
│    + .meta.json (version, git SHA, logic hash, 窗, raw chksum) │
│    raw 保留 (不刪, 供回頭調因子定義)                          │
└────────────────────────────────────────────────────────────┘
                     │ parquet (bar-indexed, tz-aware UTC)
                     ▼
┌─ factor eval (重用現有) ─────────────────────────────────┐
│ stage0a loader: 讀快取 OF parquet (interval-aware)         │
│   → build_feature_dict(..., orderflow_df=...)              │
│ orderflow_factors() family: OF df → 具名 Series, 對齊 candle_idx │
│ stage1: IC / IR / cross-regime / verdict ← 同一把尺        │
│   產 factor_eth.json (manifests/30m/)                      │
│ sources.py: + binance_orderflow registry entry (stage0 candidate 驗證) │
└────────────────────────────────────────────────────────────┘
                     ▼
        dashboard Factors 頁 (?interval=30m) 直接看
```

核心切分:

- **重的一次性預處理**(下載+聚合)獨立成 util,**不進 stage0a 熱路徑** — stage0a 只讀已算好的 parquet。
- **新 code 只有左半邊**(downloader + aggregator + family + loader 接線)。右半邊全是現有機器。
- 介面契約:order-flow 以 **feature family** 接入(鏡像現有 `funding_factors` / `oi_factors` / `basis_factors`),非單序列 fetcher。

## 3. 元件拆解

| # | 元件 (新檔) | 職責 | 介面 | 依賴 |
|---|---|---|---|---|
| 1 | `research/lib/binance_dump.py` **downloader** | 抓 data.binance.vision ETHUSDT-aggTrades 月檔、驗 CHECKSUM、快取 raw、idempotent(已下載跳過) | `download_aggtrades(symbol, months) -> list[Path]` | httpx, zipfile |
| 2 | `research/lib/orderflow.py` **aggregator** | raw aggTrades → bar-level OF 特徵 parquet。look-ahead-safe `[T,T+iv)` 左閉右開 bucket。Polars lazy/chunked(防 OOM) | `aggregate(raw, interval) -> pl.DataFrame`；`write_cache(df, sym, iv, ver)` 帶 meta | polars |
| 3 | `orderflow_factors()` **feature family** | bar-level OF df → 具名特徵 Series,對齊 candle_idx;rolling 窗照 `bars_per_hour` 縮放 | `orderflow_factors(of_df, candle_idx, interval) -> dict[str, Series]`,鏡像 `funding_factors` | pandas |
| 4 | stage0a **loader + 接線** | 讀快取 OF parquet(interval-aware)→ 傳進 `build_feature_dict`(新增 `orderflow_df` 參數) | `build_feature_dict(..., orderflow_df=None)` | 現有 |
| 5 | `research/lib/sources.py` registry entry **(POC 可選)** | `binance_orderflow`(available)— 讓 stage0 discovery 能提 order-flow candidate;新 category `"orderflow"`。**POC 不嚴格需要**:`build_feature_dict` 對 feature_dict 內**每個**特徵都算 IC(不靠 stage0 candidate),所以 OF 特徵只要進 dict 就被評。此 entry 是「promote 到 discovery 建策略」時才需 | SourceSpec | 現有 |

### 隔離性

- 元件 1、2 純離線(吃檔吐檔),小 fixture 可單測,CI 不碰真網路(downloader mock HTTP)。
- 元件 3 純函數(df→dict),fixture 測 + look-ahead 洩漏專測。
- 元件 4、5 是現有檔小接線(<20 行),跟 funding/oi 同模子。
- 新 code 集中在檔 1–3。

## 4. Order-Flow 因子定義

aggTrades 欄位:`agg_trade_id`、`price`、`quantity`、`transact_time`(ms)、`is_buyer_maker`。

**符號約定(反直覺,最易寫錯)**:
- `is_buyer_maker = false` → 買方是 taker = **主動買**(market buy,成交在 ask)
- `is_buyer_maker = true` → 買方是 maker、賣方 taker = **主動賣**(market sell,成交在 bid)

每 30m bar 內把成交分主動買量/賣量、買筆數/賣筆數,衍生 4 個 POC 因子:

| 因子 | 公式 (每 bar) | 白話 | 性質 |
|---|---|---|---|
| `trade_imbalance` | (主動買量 − 主動賣量) / (買 + 賣),範圍 [−1,1] | 量的多空失衡,歸一化 | 對極端穩、利 cross-regime |
| `trade_count_imbalance` | (主動買**筆數** − 賣筆數) / 總筆數,[−1,1] | 筆數失衡(≠量失衡)。量易被單鯨干擾,筆數反映散戶 vs 機構博弈 | IC 常更穩 |
| `large_trade_ratio` | 大單量 / 本 bar 總量。大單 = **USD-notional 分桶**(`<$10k/$10-50k/$50-200k/>$200k`,單筆 USD=price×qty),預設 edge **>$50k**(取高二桶) | 大戶/知情單佔比 | **bar-level USD binning**:單遍掃 raw、look-ahead-safe、不用回頭掃 raw 即可改門檻、USD 計價不隨幣價漂移 |
| `price_impact` | bar 報酬 / bar 總量(Kyle-λ proxy) | 單位量推價力;小量大幅推價 = 流動性枯竭/知情流 | 與前三不共線 |

### 設計重點

- **真 intraday-native**:每因子從 bar 內成交算,天生對齊、零 ffill。切到 30m/15m 真的會變 — 解開慢因子困境。
- **誠實邊界**:真正的 OFI(Cont 定義)需 L2 盤口的 bid/ask 量變化。aggTrades 只有成交、沒盤口 → 我們做的是「成交流不平衡」變體,非教科書 OFI。夠驗 intraday 有無 alpha;若有但不夠強,再上 Tardis L2 補真 OFI / microprice / depth imbalance。
- **共線性檢查**:POC 第一步先算 4 因子相關矩陣,確認兩兩 < 0.85 再信 IC(已砍掉與 `trade_imbalance` 共線的 `cvd_delta_z`、與 large_trade 重疊的 `avg_trade_size_z`)。

## 5. 儲存佈局

```
research/data/
  raw/binance/ETHUSDT-aggTrades-2025-06.zip   ← 下載快取, 數十 GB, gitignored, 保留
  orderflow/of_eth_30m_v1.parquet             ← bar-level 特徵, KB-MB, gitignored
           /of_eth_30m_v1.meta.json           ← version / git SHA / logic hash / 窗 / generated_at / raw checksum / 因子清單
```

- **raw 保留**(不刪):磁碟便宜,order-flow 因子常回頭調定義(大單閾值 / price_impact 窗),重下載成本遠高於存磁碟。可移廉價儲存。
- bar-level parquet gitignored,**跟現有 `factor_values_*.parquet` 同慣例**(runtime data,不進 git,server 單獨產)。
- **版本鎖**:meta 帶 `version` + **git commit SHA** + **聚合邏輯 hash**(純手動 version 易忘 bump,自動綁代碼狀態保重現)。stage0a loader 檢查預期 version,不符 → **ValueError**(防誤讀舊快取出錯結論)。
- 全程 **UTC**,僅展示層轉時區(免夏令時造成 bar 長度不一)。

## 6. 測試策略(TDD,research suite)

| 測 | 重點 |
|---|---|
| aggregator 單測 | 小合成 aggTrades fixture → 手算 bar 特徵對拍 |
| **look-ahead 洩漏專測** | T+30m 邊界那筆**必落下一根**(左閉右開);加未來 bar 不改過去值 |
| `is_buyer_maker` 符號測 | 構造買/賣單,斷言 `True`=主動賣、imbalance 符號對(反直覺,加強斷言) |
| `rolling_large_trade_ratio` 前視測 | 斷言只用過去資料(非全樣本分位)— 末尾值不隨未來 bar 變 |
| **重複 ID 測** | Binance 分頁常吐重複 `agg_trade_id` → 驗 `drop_duplicates` |
| **空 bar 測** | 零成交 bar(流動性枯竭)→ imbalance 分母 0 不爆 `ZeroDivision`/`Inf`,回 NaN |
| downloader 測 | mock HTTP + checksum 驗證 + idempotent skip,CI 不碰真網路 |
| family 函數測 | df→dict、對齊 candle_idx、窗照 `bars_per_hour` 縮放 |
| 整合測 | fixture parquet → `build_feature_dict` 含 OF 特徵 → `compute_evidence_entries` 回 IC(鏡像現有 stage0a 整合測) |

注意:research 與 dashboard pytest **必須分開跑**(現有慣例,合跑 sys.path 衝突)。

## 7. 工程坑預警

- **記憶體**:逐筆量大,aggregator 用 Polars lazy scan / chunking,不一次載整月 → 防 OOM。
- **時區/夏令時**:Binance 統一 UTC,堅持 UTC 存儲與運算。
- **浮點精度**:`price_impact` 數值極小,注意浮點精度(必要時 scale)。
- **缺月處理**:某月 dump 不存在 → downloader 明確報錯/跳過,不靜默產生破洞。

## 8. 成功判準與後續

**Go / No-Go**(POC 出口):

- **Go**:≥1 因子 `|IC| ≥ 0.03` 且 cross-regime 不翻號(verdict `single_use` / `ensemble_only`)。→ 下一步:長成 robust ingestion(多幣 / 15m)、考慮 stage2-5 建策略、評估是否上付費 L2 補真 OFI。
- **No-Go**:全 `reject`。→ 收手,intraday 微結構此資料層級無 alpha,不上付費盤口。

**out of scope**(本 POC 不做):robust 多幣 ingestion、live trading 接線、L2 盤口、爆倉資料、15m(IC 確認後再擴)。

## 9. 對既有程式的改動清單

- 新檔:`research/lib/binance_dump.py`、`research/lib/orderflow.py`
- 改:`research/pipeline/stage0a_features.py`(`build_feature_dict` 加 `orderflow_df` 參數 + `orderflow_factors` family + loader 接線)
- 改(POC 可選,promote 到 discovery 才需):`research/lib/sources.py`(`binance_orderflow` registry entry)+ FactorCandidate.category Literal 擴 `"orderflow"`
- 新 deps:`polars`(聚合)、`httpx`(下載,若未有)
- 新測:對應上述 §6
