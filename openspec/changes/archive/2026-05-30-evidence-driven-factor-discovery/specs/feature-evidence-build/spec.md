## ADDED Requirements

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
