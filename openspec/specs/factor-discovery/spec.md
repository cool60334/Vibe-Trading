# factor-discovery Specification

## Purpose

Stage 0 swarm-based factor discovery: structured candidate schema contract, swarm exploration runner, the `crypto_factor_lab` preset, and the dynamic stage 1 that reads candidates from the feature store.

## Requirements

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

### Requirement: Deterministic candidate selector

系統 SHALL 提供 `research/pipeline/lib/factor_selector.py` 純函式模組，導出 `select_candidates_from_evidence` 函式。該函式 MUST 為 pure / 無 IO（僅讀傳入 evidence dict + feature store columns set），可被單測獨立呼叫。

`select_candidates_from_evidence(evidence, feature_store_cols, *, min_abs_ic, min_abs_ir, max_candidates)` MUST 依下列規則挑因子：

1. 對每個 evidence entry 計算 `top_horizon = argmax_h |IC[h]|` 與 `top_abs_ic`。
2. 過濾 `top_abs_ic ≥ min_abs_ic` AND `|IR| ≥ min_abs_ir`。
3. 過濾 `feature_key ∈ feature_store_cols`。
4. 依 `top_abs_ic` 降冪排序，取前 `max_candidates`。
5. 為每個 entry 構造 `FactorCandidate`：
   - `expected_ic_sign` 取自 `sign(IC[top_horizon])`（`+` / `-`）
   - `horizons_h` 含 `top_horizon` 與同向 |IC| > `min_abs_ic/2` 的鄰近 horizon
   - `economic_logic` 為 deterministic placeholder（含 IC/IR/horizon 數字）
   - `category` 取自 evidence entry
   - `name` = `feature_key`
6. 回傳 `list[FactorCandidate]`，長度 ∈ [0, max_candidates]。

門檻參數 MUST 可由 `research_config.yaml` 之 `stage0_selector` 區塊覆寫；預設值 `min_abs_ic=0.05`、`min_abs_ir=0.10`、`max_candidates=6`。

#### Scenario: 高 IC 因子被選入

- **GIVEN** evidence 含 feature `stablecoin_supply_z` IC@168=+0.091, IR=+4.43
- **AND** `stablecoin_supply_z` 為 feature store 欄位
- **WHEN** `select_candidates_from_evidence(...)` 以預設門檻呼叫
- **THEN** 回傳 list MUST 含一個 `FactorCandidate` 之 `feature_key == "stablecoin_supply_z"`
- **AND** 該 candidate 之 `expected_ic_sign == "+"`
- **AND** `horizons_h` MUST 含 168

#### Scenario: 低 IC 因子被過濾

- **GIVEN** evidence 含 feature `basis_rel` IC@8=-0.019, IR=-0.674
- **WHEN** `select_candidates_from_evidence(...)` 以預設 `min_abs_ic=0.05` 呼叫
- **THEN** 回傳 list MUST NOT 含 `feature_key == "basis_rel"` 之 candidate

#### Scenario: feature_key 不在 feature store

- **GIVEN** evidence 含 feature `imaginary_factor` IC@72=+0.1
- **AND** `imaginary_factor` 不在 `feature_store_cols`
- **WHEN** `select_candidates_from_evidence(...)` 呼叫
- **THEN** 回傳 list MUST NOT 含此因子

#### Scenario: max_candidates 截斷

- **GIVEN** evidence 有 20 個過門檻因子
- **WHEN** `select_candidates_from_evidence(..., max_candidates=6)` 呼叫
- **THEN** 回傳 list 長度 MUST == 6
- **AND** 6 個因子 MUST 為 `top_abs_ic` 排名前 6

#### Scenario: 零因子過門檻

- **GIVEN** evidence 中所有因子 |IC| < 0.05
- **WHEN** `select_candidates_from_evidence(...)` 以預設門檻呼叫
- **THEN** 回傳 list 長度 MUST == 0
- **AND** 函式 MUST NOT raise

### Requirement: Stage 0 swarm 探索流程

系統 SHALL 提供 `research/pipeline/stage0_discovery.py` 作為 stage 0 runner，其 main 函式 MUST 對 `research_config.yaml` 中每個 symbol 執行一次因子探索。本流程 MUST 以 Stage 0a 證據建置（`feature-evidence-build` capability）之產物為前置依賴：`features_<sym>.parquet` 與 `evidence_<sym>.json` MUST 已存在，否則該 symbol MUST 視為失敗。

runner 之預設模式 (`--no-swarm` / `RESEARCH_STAGE0_USE_SWARM=0`，為預設) MUST 採 **deterministic 路徑**：(1) 檢查快取；(2) 確認 0a 產物存在；(3) 載入 evidence_<sym>.json 與 features_<sym>.parquet 欄名；(4) 呼叫 `select_candidates_from_evidence`；(5) 驗證 schema；(6) 寫 `candidates_<sym>.json`；(7) verify outputs。**MUST NOT 呼叫 LLM swarm。**

runner 之 enrichment 模式 (`--use-swarm` / `RESEARCH_STAGE0_USE_SWARM=1`) MUST 在 deterministic 路徑產出 candidates 後，**呼叫 swarm 為每個 candidate 補 `economic_logic` 散文**。enrichment MUST 採以下 fail-soft 規則：
- swarm 呼叫失敗（subprocess 非 0、timeout、輸出空） → log warning、保留 deterministic placeholder、繼續寫 manifest
- swarm 回傳 JSON 解析失敗 → log warning、保留 placeholder、繼續
- swarm 回傳結構部分缺欄 → 個別欄位 fallback 至 placeholder、其他成功欄位採用
- 整個 enrichment 區塊 MUST 包在 try/except，**任何例外 MUST NOT 中斷 pipeline 或導致非 0 exit code**

runner MUST 採 thin orchestration shell + 抽離 pure-logic helpers（如 `parse_candidates_json`、`verify_outputs`、`compute_exit_code`、`print_summary`、`validate_feature_keys`、`select_candidates_from_evidence`、`enrich_candidates_with_swarm`）以供單元測試。

#### Scenario: 全部 symbols deterministic 成功（預設模式）

- **GIVEN** 所有 symbols 之 0a 產物齊備
- **AND** 未傳 `--use-swarm` 旗標、`RESEARCH_STAGE0_USE_SWARM` 未設或為 `0`
- **WHEN** stage 0 runner 執行
- **THEN** runner MUST NOT 呼叫 swarm
- **AND** 每個 symbol MUST 產出合法 `candidates_<sym>.json`
- **AND** runner exit code MUST 為 0
- **AND** stdout MUST 顯示每 symbol 「candidates: N (deterministic)」

#### Scenario: 0a 產物缺失

- **GIVEN** 某 symbol 之 `evidence_<sym>.json` 或 `features_<sym>.parquet` 不存在
- **WHEN** stage 0 runner 執行
- **THEN** 該 symbol MUST 視為失敗且 stdout 印明顯錯誤訊息要求先跑 Stage 0a
- **AND** runner exit code MUST 為 1
- **AND** runner MUST NOT 寫該 symbol 之 `candidates_<sym>.json`

#### Scenario: 零因子過門檻（不算失敗）

- **GIVEN** 某 symbol 之 evidence 所有因子 |IC| 皆小於門檻
- **WHEN** stage 0 runner 執行（deterministic）
- **THEN** runner MUST 寫出 `candidates_<sym>.json` 其 `candidates` 陣列為空
- **AND** stdout MUST 含警告 `0 factors passed IC/IR threshold; lower thresholds or expand feature pool`
- **AND** runner exit code MUST 為 0
- **AND** schema 驗證 MUST 通過（空陣列為合法）

#### Scenario: enrichment 模式 swarm 失敗 — pipeline 不中斷

- **GIVEN** `--use-swarm` 已啟用且 0a 產物齊備
- **WHEN** swarm 呼叫超時或回傳無法解析的內容
- **THEN** runner MUST log warning（含失敗原因）
- **AND** `candidates_<sym>.json` MUST 仍寫出（含 deterministic placeholder economic_logic）
- **AND** runner exit code MUST 為 0
- **AND** 候選因子陣列 MUST 等同純 deterministic 模式之輸出

#### Scenario: enrichment 模式 swarm 成功 — economic_logic 被覆寫

- **GIVEN** `--use-swarm` 已啟用且 swarm 成功為 K 個候選因子回傳合法 JSON
- **WHEN** stage 0 runner 處理 swarm 回應
- **THEN** 對 feature_key 成功匹配的 candidate，其 `economic_logic` MUST 為 swarm 提供之文字
- **AND** 其他結構欄位（`expected_ic_sign`、`horizons_h`、`category`、`feature_key`、`name`）MUST 與 deterministic 路徑一致，**MUST NOT 被 swarm 覆寫**

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
