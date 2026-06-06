# feature-evidence-build Specification

## Purpose

Stage 0a evidence building: the config-driven indicator pool, the persisted feature store contract, the IC-ranked evidence table, and the Stage 0a runner that produces both artifacts per symbol.

## Requirements

### Requirement: 指標池計算模組

系統 SHALL 提供 `research/lib/indicators.py`，公開一個 config 驅動的價格指標池計算函式（例 `compute_indicator_pool(candles: pd.DataFrame, config) -> dict[str, pd.Series]`），其 MUST 以純 python 的 `pandas-ta` 計算指標，MUST NOT 依賴 TA-Lib C 函式庫。指標池清單 MUST 由 config 驅動（預設涵蓋動量 RSI/MACD/ROC/Stoch、趨勢 EMA/SMA cross/ADX、波動 ATR/BB width/rolling std、量 OBV/MFI/volume z），每個指標 SHALL 以唯一具名 key 輸出一條與輸入 candle index 對齊的 `pd.Series`。

所有指標計算 MUST 為因果（僅使用當下與過去資料、rolling 窗），MUST NOT 引入 look-ahead。

#### Scenario: 指標池計算回傳對齊序列

- **WHEN** 以一段 hourly candle DataFrame 呼叫指標池計算
- **THEN** 回傳之每條 series MUST 與輸入 candle index 同 index、同長度
- **AND** 序列數 MUST 等於 config 啟用之指標數

#### Scenario: 無 TA-Lib 依賴

- **WHEN** 在未安裝 TA-Lib C 函式庫的環境 import `research/lib/indicators.py`
- **THEN** import MUST 成功且指標計算 MUST 可執行

### Requirement: feature store 持久化契約

系統 SHALL 在 Stage 0a 完成後，將價格指標池與非價格因子（funding/OI/stablecoin，經現有 fetcher + transform）之 hourly 時間序列合併持久化於 `research/manifests/features_<sym>.parquet`，並 SHALL 同目錄產出 sidecar `features_<sym>.meta.json`。

parquet schema MUST：索引欄為 UTC tz-aware hourly DatetimeIndex；每個特徵為一 `float64` 欄、欄名為其唯一 feature key；允許 NaN（warm-up / 缺資料）；採 pyarrow engine、snappy 壓縮。meta.json MUST 含 `schema_version`、`symbol`、`generated_at`、`feature_names`（與 parquet 欄名一致）、`index_start`、`index_end`、`n_rows`。

#### Scenario: 0a 寫出 features parquet 與 meta

- **WHEN** `Stage 0a` 對 `btc` 完成執行
- **THEN** `research/manifests/features_btc.parquet` MUST 存在
- **AND** `research/manifests/features_btc.meta.json` MUST 存在
- **AND** 兩檔的 feature 名集合 MUST 一致且 row 數 > 0

#### Scenario: 含價格與非價格特徵

- **WHEN** 檢視 `features_btc.parquet` 欄名
- **THEN** MUST 同時包含至少一個價格指標 key（如 `rsi_14`）與至少一個非價格 key（如 funding 衍生因子）

### Requirement: evidence IC 排名證據表契約

系統 SHALL 在 Stage 0a 對 feature store 中每個特徵相對 forward return（多 horizon，沿用 `research/lib/factor_metrics`）計算 Spearman IC 與 IR，並 SHALL 將結果寫為 `research/manifests/evidence_<sym>.json` 證據表。證據表 MUST 為可被 pydantic 驗證的結構，每筆 MUST 含 `feature_key`、`category`、`ic_by_horizon`（horizon→IC 對映）、`ir`、`sample_size`，並 SHALL 依 max |IC| 排序。

證據表 MUST 標註 caveat：IC 為篩選用途、未做交易成本與 multiple-testing 校正，後續仍由 skeptic 與 stage 1 verdict 把關。

#### Scenario: evidence 表涵蓋全部特徵

- **WHEN** Stage 0a 對 `btc` 完成
- **THEN** `evidence_btc.json` MUST 存在且可被 pydantic 驗證
- **AND** 其條目數 MUST 等於 `features_btc.parquet` 之欄數
- **AND** 每筆 `feature_key` MUST 對應 parquet 中存在的欄

#### Scenario: 依 IC 強度排序

- **WHEN** 讀取 `evidence_btc.json`
- **THEN** 條目 MUST 依 max |IC| 由大到小排序

### Requirement: Stage 0a runner

系統 SHALL 提供 `research/pipeline/stage0a_features.py` 作為新 pipeline 階段 runner，對 `research_config.yaml` 中每個 symbol 執行：(1) 抓 OHLCV（現有 ccxt fetcher）與非價格資料（現有 fetcher）；(2) 計算指標池與非價格因子；(3) 寫 feature store；(4) 算多 horizon IC 寫 evidence 表；(5) verify outputs + 適當 exit code。runner MUST 採 thin orchestration shell + 可單元測試之 pure-logic helpers 模式。

#### Scenario: runner 成功

- **WHEN** 執行 `python -m research.pipeline.stage0a_features` 且所有 symbol 成功
- **THEN** runner exit code MUST 為 0
- **AND** 每個 symbol 之 `features_<sym>.parquet` 與 `evidence_<sym>.json` MUST 存在

#### Scenario: 單一 symbol 失敗不阻斷其餘

- **WHEN** 某 symbol 之資料抓取失敗
- **THEN** runner MUST 對該 symbol 記錄失敗並以非零 exit code 結束
- **AND** 其餘 symbol 之產物 MUST 仍正常寫出

### Requirement: 永續衍生因子族（basis / funding / OI 衍生）

系統 SHALL 提供 `research/lib/derived_factors.py`，公開三個純函式（無網路、無檔案 I/O、可單元測試）計算永續合約衍生因子並各回 `dict[str, pd.Series]`：

- `basis_factors(perp_close, spot_close)` → `basis_rel = (perp−spot)/spot`、`basis_z`（basis_rel 之 rolling z-score）、`basis_mom`（basis_rel 之 N 小時差分）。
- `funding_factors(funding_on_candle)` → `funding_z`（rolling z-score）、`funding_mom`（N 小時差分）。
- `oi_factors(oi_on_candle, close)` → `oi_z`（rolling z-score）、`oi_price_divergence`（OI 變化 × 價格報酬）、`oi_mom`（較長窗 OI 動量）。

所有 rolling / 動量 window MUST 由模組層級 config 常數定義（`SCREEN_ZSCORE_DAYS`、`SCREEN_MOM_HOURS`、`OI_MOM_HOURS`），並 MUST 以 `N 天 = N×24` bar 換算對齊 1H candle grid。所有計算 MUST 為因果（僅用當下與過去資料），MUST NOT 引入 look-ahead，且每條輸出 series MUST 與輸入 index 對齊。

Stage 0a SHALL 取得 basis 所需之**現貨 close**：以 perp instId 去除 `-SWAP` 後綴經既有 OHLCV fetcher 取得（同 endpoint、同 bar），並於取得失敗時 graceful 跳過 basis 家族而不阻斷其餘因子。這 8 個衍生因子 MUST 與既有價格/非價格因子一併寫入 `features_<sym>.parquet`（1H 對齊）並納入 `evidence_<sym>.json` IC 證據表。

funding 衍生因子（`funding_z`、`funding_mom`）之**篩選 IC** MUST 以 funding 原生 8h 結算頻率量測（measurement-layer subsample，比照 `funding_rate_raw`），以避免 8h→1H ffill 造成的樣本灌水與自相關高估；其所**儲存**之 series 仍為 1H 對齊版，不受量測層影響。

#### Scenario: 衍生因子寫入 feature store 與 evidence

- **WHEN** Stage 0a 對某 symbol 成功取得 perp、spot、funding、OI 資料並完成執行
- **THEN** `features_<sym>.parquet` MUST 含 `basis_rel`、`basis_z`、`basis_mom`、`funding_z`、`funding_mom`、`oi_z`、`oi_price_divergence`、`oi_mom` 八個欄
- **AND** `evidence_<sym>.json` MUST 對此八個 feature_key 各有一條 IC 條目，且其 `category` 分屬 `basis` / `funding` / `oi`

#### Scenario: funding 衍生以原生頻率量測 IC

- **WHEN** 對 `funding_z` 或 `funding_mom` 計算篩選 IC
- **THEN** 量測層 MUST 將序列 subsample 至 8h 原生頻率（非 1H ffill 全格）
- **AND** evidence 條目之 `ic_eval_transform` MUST 標示其原生頻率轉換（如 `native_8H`）

#### Scenario: basis 資料缺失不阻斷其餘因子

- **WHEN** 現貨 close 取得失敗
- **THEN** Stage 0a MUST 記錄 warning 並跳過 `basis_*` 三因子
- **AND** funding/OI 衍生因子與既有所有因子 MUST 仍正常寫出

#### Scenario: 衍生因子為因果且對齊

- **WHEN** 以一段 hourly 序列呼叫任一家族函式
- **THEN** 回傳之每條 series MUST 與輸入 index 同 index、同長度
- **AND** 任一時點之值 MUST NOT 依賴該時點之後的資料（無 look-ahead）
