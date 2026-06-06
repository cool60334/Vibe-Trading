## MODIFIED Requirements

### Requirement: 候選因子 schema 契約

系統 SHALL 以 Pydantic 模型 `FactorCandidate` 與 `CandidatesManifest` 作為 stage 0 → stage 1 的結構化資料契約，並 SHALL 將兩個模型定義於 `dashboard/server/schemas.py`，與既有 `FactorManifest` 同檔案以保持 schema 來源單一。

`FactorCandidate` MUST 包含以下欄位：`name`（字串，因子識別名）、`formula`（字串，自然語＋虛擬公式）、`feature_key`（字串，指向 feature store `features_<sym>.parquet` 中的欄名）、`expected_ic_sign`（`"+"` / `"-"` / `"?"`）、`economic_logic`（字串）、`horizons_h`（int 陣列）、`category`（既有 Literal 集合）。`data_source` 與 `transform` 欄位 SHALL 改為選填（`Optional[str]`，預設 `None`，僅作說明性註記），以向後相容既有 manifest；新流程 MUST 以 `feature_key` 作為下游取值依據，不再要求 `data_source`/`transform` 為 registry 合法 key。

`CandidatesManifest` MUST 包含：`schema_version`（int，預設 1）、`symbol`（字串）、`generated_at`（datetime）、`source_swarm_run`（字串或 null）、`candidates`（`FactorCandidate` 陣列）。

#### Scenario: candidates JSON 通過 schema 驗證

- **WHEN** stage 0 寫入 `research/manifests/candidates_<sym>.json` 後
- **THEN** 該檔 MUST 能以 `CandidatesManifest.model_validate_json(text)` 驗證通過
- **AND** 每個 `FactorCandidate.category` MUST 屬於既有合法 category 集合
- **AND** 每個 `FactorCandidate.feature_key` MUST 為 `features_<sym>.parquet` 中存在的欄名

#### Scenario: 舊 manifest 向後相容

- **WHEN** 載入一份僅含 `data_source`/`transform`、無 `feature_key` 的舊 `candidates_<sym>.json`
- **THEN** Pydantic 驗證 MUST 通過（`feature_key` 由舊欄推導或標記為待補）
- **AND** schema 變更 MUST NOT 因缺 `feature_key` 而對既有歸檔 manifest raise

#### Scenario: feature_key 不存在於 feature store 被拒

- **WHEN** swarm 輸出之某 candidate 的 `feature_key` 不在 `features_<sym>.parquet` 欄名集合內
- **THEN** stage 0 解析後 MUST 丟棄該 candidate（不寫入 manifest），並於 stdout print 警告

### Requirement: Stage 0 swarm 探索流程

系統 SHALL 提供 `research/pipeline/stage0_discovery.py` 作為 swarm 探索階段 runner，其 main 函式 MUST 對 `research_config.yaml` 中每個 symbol 執行一次因子探索流程。本流程 MUST 以 Stage 0a 證據建置（`feature-evidence-build` capability）之產物為前置依賴：`features_<sym>.parquet` 與 `evidence_<sym>.json` MUST 已存在，否則該 symbol MUST 視為失敗並走 fallback。

runner MUST 遵循下列順序：(1) 檢查快取；(2) 確認 0a 產物存在；(3) 呼叫 swarm（注入 evidence 表與 feature store 路徑）；(4) 解析 JSON；(5) 驗證 schema 與 `feature_key` 存在性；(6) 寫 manifest；(7) verify outputs + 適當 exit code（0 全成功、1 任一失敗）。

runner MUST 採與既有 stage1_factors.py 相同的設計模式：thin orchestration shell + 抽離 pure-logic helpers（如 `parse_candidates_json`、`verify_outputs`、`compute_exit_code`、`print_summary`、`validate_feature_keys`）以供單元測試。JSON 格式化 MUST 由程式確定性處理（`parse_candidates_json` + pydantic），MUST NOT 依賴 LLM formatter agent。

#### Scenario: 全部 symbols 成功

- **WHEN** 對所有 symbols 0a 產物齊備且 stage 0 swarm 都成功產生有效 `candidates_<sym>.json`
- **THEN** runner exit code MUST 為 0
- **AND** stdout MUST 包含每個 symbol 的「candidates: N」摘要

#### Scenario: 0a 產物缺失

- **WHEN** 某 symbol 的 `evidence_<sym>.json` 或 `features_<sym>.parquet` 不存在
- **THEN** stage 0 swarm MUST 不呼叫 swarm，視該 symbol 失敗並寫 `candidates_<sym>.failed.json`
- **AND** runner exit code MUST 為 1

#### Scenario: 部分 symbol 失敗

- **WHEN** 任一 symbol 之 swarm 解析失敗且 retry 後仍失敗
- **THEN** runner exit code MUST 為 1
- **AND** 失敗 symbol MUST 同時產生 `candidates_<sym>.failed.json` 紀錄錯誤摘要

### Requirement: Swarm preset crypto_factor_lab

系統 SHALL 提供 swarm preset `crypto_factor_lab`（預設路徑 `agent/src/swarm/presets/crypto_factor_lab.yaml`），其中 MUST 定義恰好兩個 agent：(1) `researcher` 讀取證據表與 feature store、提案候選因子；(2) `skeptic` 對候選因子進行過擬合、正交性與經濟邏輯審查。preset MUST NOT 包含 LLM `output_formatter` agent（JSON 整理改由 stage 0 程式確定性處理）。

`researcher` 的 system_prompt MUST 為方法紀律護欄式，MUST NOT 寫死任何特定因子的 IC 方向或先驗答案（例如不得預先斷言 funding rate 為反向訊號）。該 prompt MUST 要求：禁止 look-ahead / 資料洩漏、每個因子須附經濟邏輯、偏好低相關正交因子、對異常高 IC 保持懷疑、誠實回報 IC。`researcher` MUST 具備 bash 與檔案讀寫工具，可讀 `features_<sym>.parquet`、可自算複合因子並將序列寫回 feature store（新增具名欄）後再以該欄名作為 `feature_key` 提案。

該 preset MUST 宣告下列變數：`target_universe`、`horizons_h`、`evidence_path`（指向 `evidence_<sym>.json`）、`features_path`（指向 `features_<sym>.parquet`）。

#### Scenario: preset 載入無誤

- **WHEN** 以齊備變數執行 `vibe-trading --swarm-run crypto_factor_lab '<vars_json>'`
- **THEN** swarm 運作時 MUST 不因變數缺失 raise
- **AND** swarm 完成後 stdout MUST 至少包含一個合法 ```json fenced code block

#### Scenario: prompt 不含寫死先驗

- **WHEN** 檢視 `researcher` 的 system_prompt
- **THEN** prompt MUST NOT 含任何斷言特定因子 IC 方向的句子
- **AND** prompt MUST 含方法紀律護欄（禁 look-ahead、要求經濟邏輯、過擬合懷疑）

#### Scenario: 無 LLM formatter

- **WHEN** 解析 `crypto_factor_lab.yaml` 的 agents 清單
- **THEN** agents MUST 恰為 `researcher` 與 `skeptic` 兩者
- **AND** MUST NOT 含 `output_formatter`

### Requirement: Stage 1 改為動態讀取 candidates

系統 SHALL 修改 `research/factor_extended.py` 使其 `run_symbol` 函式不再硬編 `["funding_rate", "oi_change_24h", "fng"]` 三個因子名，改為先讀 `research/manifests/candidates_<sym>.json` 並依候選清單動態計算 IC/IR。每個候選 MUST 透過其 `feature_key` 自 feature store（`features_<sym>.parquet`）讀取已算好的因子序列，再餵 `evaluate_factor`；stage 1 MUST NOT 在此路徑重新呼叫 source fetcher 或重算 transform。

stage 1 MUST 支援 legacy fallback：若環境變數 `RESEARCH_LEGACY_FACTORS=1` 或 candidates JSON 缺失（且 `RESEARCH_LEGACY_FACTORS` 未設為 `0`）→ 退化為原硬編 3 因子模式；其餘情境（candidates 存在且 legacy 未設定）→ 採新模式。stage 1 完成後 MUST 將選定因子序列收斂寫出 `factor_values_<sym>.parquet`（僅含選定因子欄），供下游 stage2b 按名讀取。

#### Scenario: 新模式從 feature store 取值

- **WHEN** `candidates_btc.json` 存在含 5 個候選因子，且 `features_btc.parquet` 含對應 5 個 `feature_key` 欄
- **AND** stage 1 對該 symbol 執行
- **THEN** 產出之 `factor_btc.json` 之 `factors` 陣列長度 MUST 等於 5
- **AND** 每個 factor 之 IC MUST 由 feature store 既有序列計算得出，過程 MUST NOT 呼叫 source fetcher

#### Scenario: feature_key 在 store 缺欄

- **WHEN** 某 candidate 的 `feature_key` 不在 `features_btc.parquet` 欄內
- **THEN** stage 1 MUST 跳過該因子並 print 警告，MUST NOT 中斷其餘因子計算

#### Scenario: candidates 缺失走 legacy fallback

- **WHEN** `candidates_btc.json` 不存在
- **AND** 環境變數 `RESEARCH_LEGACY_FACTORS` 未設或設為 `1`
- **THEN** stage 1 MUST 以原硬編 3 因子模式執行
- **AND** stdout MUST print 警告
