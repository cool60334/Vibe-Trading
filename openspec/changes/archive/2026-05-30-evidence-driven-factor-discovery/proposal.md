## Why

現況因子發掘是「盲提案 → 後驗證」:`crypto_factor_lab` swarm 在沒看過任何資料下、從固定小選單(4 source × 8 transform)提案因子,且 `factor_proposer` system prompt 寫死 funding 反向先驗,等於把答案餵給 agent。真實 IC 要到 stage1 才算,agent 提案時毫無實證依據。狹窄搜尋空間 + 人為先驗已被證實導致 ETH funding+momentum+vol 4 年實測全負 sharpe 的過擬合死路。本變更把流程翻轉成「先用真實數據算證據 → agent 看證據自由探索選/組因子」,並拿掉寫死先驗、擴大搜尋空間。

## What Changes

- 新增 **Stage 0a 確定性特徵與證據建置**:抓 OHLCV(現有 ccxt fetcher)+ 非價格資料(funding/OI/stablecoin 現有 fetcher),用純 python 的 **pandas-ta** 算一整批價格指標池,全部序列寫進 feature store `features_<sym>.parquet`,並對 forward return 算多 horizon IC/IR → 排名證據表 `evidence_<sym>.json`。
- `crypto_factor_lab` swarm 由 3 agents 改 **2 agents**:`researcher`(讀證據表 + bash 工具讀 features parquet、可自算複合因子寫回 feature store → 提案;prompt 改為方法紀律護欄,**不寫死方向/先驗**)+ `skeptic`(過擬合/正交性/經濟邏輯審查)。**移除 LLM `output_formatter`**,候選 JSON 改由 stage0 程式 `parse_candidates_json` + pydantic 確定性處理。
- **BREAKING** `FactorCandidate` schema:`data_source`/`transform` 由必填單一鍵改為選填/說明性,新增 `feature_key`(指向 feature store 欄名);stage0 驗證規則由「比對 SOURCE/TRANSFORM registry」改為「`feature_key` 須存在於 feature store」。
- Stage 1(`factor_extended`)改為**從 feature store 按 `feature_key` 取序列**,不再 re-fetch+transform;沿用既有 forward-return / IC / verdict 邏輯。
- **不安裝 freqtrade**:OHLCV 用 repo 現有 ccxt fetcher,指標用 pandas-ta(免 TA-Lib 編譯)。新增依賴 `pandas-ta`。
- 下游 `stage2b` / `signal_engine` 不動(仍 `load_factor_values` 按名讀 parquet)。

## Capabilities

### New Capabilities
- `feature-evidence-build`: Stage 0a 確定性步驟 — 指標池計算(pandas-ta 價格指標 + 非價格 fetcher)、feature store `features_<sym>.parquet` 持久化、forward-return 多 horizon IC/IR 排名證據表 `evidence_<sym>.json` 的產出契約。

### Modified Capabilities
- `factor-discovery`: swarm 改 2-agent 證據驅動(researcher 方法紀律護欄無寫死先驗 + skeptic),移除 LLM formatter 改程式確定性 JSON 處理;`FactorCandidate` schema 放寬 + `feature_key`;stage0 驗證改為 feature_key 存在性;stage1 改從 feature store 取序列。
- `factor-values-io`: 由「僅 stage1 寫 factor_values」擴充為「stage0a 寫 features store + evidence 表」;新增 `features_<sym>.parquet` / `features_<sym>.meta.json` / `evidence_<sym>.json` 的讀寫 helper 與 agent 複合因子 append 約定。

## Impact

- 程式:`agent/src/swarm/presets/crypto_factor_lab.yaml`、新增 `research/pipeline/stage0a_features.py`、新增 `research/lib/indicators.py`、`research/lib/factor_io.py`、`research/pipeline/stage0_discovery.py`、`research/factor_extended.py`、`research/pipeline/config.py`、`dashboard/server/schemas.py`。
- 依賴:新增 `pandas-ta`(註:numpy 2.0 相容雷,踩到則 pin numpy 或退 `ta` 套件)。
- 產物:新增 `features_<sym>.parquet` / `features_<sym>.meta.json` / `evidence_<sym>.json`;`candidates_<sym>.json` 含 `feature_key`。
- 測試:`research/tests/` 新增 feature build / evidence / indicators 測試,更新 `test_stage0_discovery`、`test_factor_extended_dynamic`。
- 不影響:stage2/2b/3/4/5、signal_engine、dashboard。
