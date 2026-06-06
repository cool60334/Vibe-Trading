## ADDED Requirements

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

## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: Swarm preset crypto_factor_lab

**Reason**: 此 change 把 swarm 從 stage 0 主導角色降為可選 enrichment role。原 requirement 規範了 swarm 必須包含的 agent 結構與 prompt 內容；新預設模式不呼叫 swarm，且 enrichment 模式對 swarm output schema 容忍度更高（部分失敗不算錯）。原 preset 結構在本 change 不動但不再是規範性需求。

**Migration**: 既有 `agent/src/swarm/presets/crypto_factor_lab.yaml` 保留不動；enrichment 模式仍會呼叫它。若未來重寫 swarm 介面（tool-use schema 強制 JSON 等），會在新 change 中重新定義其需求。原 requirement 之 scenarios（preset 載入無誤 / prompt 不含寫死先驗 / 無 LLM formatter）改為 swarm preset 自身單元測試，不再阻塞 stage 0 規範。
