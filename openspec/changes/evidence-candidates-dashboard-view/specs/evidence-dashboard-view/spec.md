## ADDED Requirements

### Requirement: Evidence 表後端讀取與 schema

系統 SHALL 在 `dashboard/server/schemas.py` 新增 `EvidenceEntry` 與 `EvidenceTable` pydantic 模型,對應 `research/manifests/evidence_<sym>.json` 之 JSON 契約。`EvidenceEntry` MUST 含 `feature_key`（字串）、`category`（既有 Literal 集合）、`ic_by_horizon`（horizon→IC 對映,值允許 null）、`ir`（float）、`sample_size`（int）。`EvidenceTable` MUST 含 `symbol`、`generated_at`、`horizons_h`、`entries`（`EvidenceEntry` 陣列）。

系統 SHALL 在 `dashboard/server/artifacts.py` 新增 `load_evidence_table(repo_root, symbol)` 與 `list_evidence_tables(repo_root)`，沿用既有 graceful 模式（讀 JSON → `model_validate` → 失敗回 `None`/略過），不得因單一檔損壞而中斷其餘。

#### Scenario: loader 讀回合法 evidence 表

- **WHEN** `research/manifests/evidence_btc.json` 存在且結構合法
- **AND** 呼叫 `load_evidence_table(repo_root, "btc")`
- **THEN** MUST 回傳 `EvidenceTable`，其 `entries` 數 MUST > 0
- **AND** 每筆 `EvidenceEntry.feature_key` MUST 為字串

#### Scenario: 缺檔或損壞不拋例外

- **WHEN** `evidence_btc.json` 不存在或 JSON 損壞
- **THEN** `load_evidence_table(repo_root, "btc")` MUST 回傳 `None`
- **AND** MUST NOT 拋出例外

### Requirement: Evidence 與 Candidates read-only API

系統 SHALL 在 `dashboard/server/main.py` 新增兩條 read-only route:`GET /api/evidence` 與 `GET /api/candidates`。兩者 MUST 預設回傳所有 symbol 的陣列,並 SHALL 支援選填 `?symbol=<sym>` 過濾單一 symbol。`/api/candidates` MUST 重用既有 `CandidatesManifest` schema 與 loader。

當對應產物不存在時,endpoint MUST 回傳空陣列（或 symbol 過濾下回 404 / null,擇一並於測試固定行為），MUST NOT 回 500。

#### Scenario: evidence endpoint 回傳全 symbol 陣列

- **WHEN** `repo_root` 下存在 `evidence_btc.json`
- **AND** `GET /api/evidence`
- **THEN** 回應 status MUST 為 200
- **AND** body MUST 為陣列且至少含一個 `symbol == "btc"` 的 `EvidenceTable`

#### Scenario: candidates endpoint 依 symbol 過濾

- **WHEN** 存在 `candidates_btc.json`
- **AND** `GET /api/candidates?symbol=btc`
- **THEN** 回應 MUST 為 200，且回傳之 candidates 之 symbol MUST 為 `btc`
- **AND** 每筆 candidate MUST 含 `feature_key` 與 `economic_logic` 欄

#### Scenario: 無產物不報 500

- **WHEN** `repo_root` 下無任何 `evidence_*.json`
- **AND** `GET /api/evidence`
- **THEN** 回應 status MUST 為 200
- **AND** body MUST 為空陣列

### Requirement: 前端 Discovery 頁 — evidence IC 排名呈現

系統 SHALL 新增前端頁面 `dashboard/web/src/pages/Discovery.tsx`，並在 `api.ts` 新增對應型別與 client method（`api.evidence()`、`api.candidates()`）。Discovery 頁 MUST 呈現 evidence 整池 IC 排名表:每列一個 `feature_key`，欄含 `category`、`ir`、`sample_size` 與各 horizon 的 IC;IC MUST 依強度配色（沿用 `FactorReport` 的 ≥0.10 強 / ≥0.05 中 門檻);預設 MUST 依 max |IC| 由大到小排序。

頁面 MUST 顯示 caveat banner，明示「IC 僅篩選用途、未做交易成本與 multiple-testing 校正」。多 symbol 時 MUST 提供 symbol 切換 tab。無 evidence 資料時 MUST 顯示空狀態提示（指引先跑 stage0a），MUST NOT 顯示錯誤。

#### Scenario: 顯示 evidence 排名表

- **WHEN** `/api/evidence` 回傳含 `btc` 的 `EvidenceTable`
- **AND** 使用者開啟 Discovery 頁
- **THEN** 頁面 MUST 渲染一個排名表，列數等於該 symbol entries 數
- **AND** 表格 MUST 預設依 max |IC| 由大到小排序
- **AND** caveat banner MUST 可見

#### Scenario: 無資料顯示空狀態

- **WHEN** `/api/evidence` 回傳空陣列
- **THEN** Discovery 頁 MUST 顯示空狀態提示文字
- **AND** MUST NOT 顯示錯誤樣式

### Requirement: 前端 candidate provenance 呈現

Discovery 頁 SHALL 在 evidence 表之外另設 candidate provenance 區塊，呈現 `candidates_<sym>.json` 的每個提案:MUST 顯示 `name`、`feature_key`、`category`、`expected_ic_sign` 與 `economic_logic`（agent 的經濟邏輯說明）。當某 candidate 的 `feature_key` 存在於該 symbol 的 evidence 表時,該區塊 SHOULD 標示其在整池中的 IC 位置（如對應 IC 值或排名）。

#### Scenario: 呈現 candidate 經濟邏輯與 feature_key

- **WHEN** `/api/candidates?symbol=btc` 回傳含 N 個 candidate
- **AND** 使用者檢視 Discovery 頁 provenance 區塊
- **THEN** MUST 顯示 N 個 candidate 條目
- **AND** 每條 MUST 同時可見 `feature_key` 與 `economic_logic`

### Requirement: 導覽與路由接入

系統 SHALL 在 `dashboard/web/src/router.tsx` 新增 Discovery 頁路由（如 `path: "discovery"`），並在 `dashboard/web/src/components/layout/Layout.tsx` 的 `NAV` 陣列新增對應導覽連結。新增 MUST NOT 改動既有 `/`、`/factors`、`/testnet`、`strategies/:id` 路由之行為。

#### Scenario: 導覽列出現 Discovery 連結

- **WHEN** 使用者開啟 dashboard 任一頁
- **THEN** 頂部導覽 MUST 含一個指向 Discovery 頁的連結
- **AND** 點擊後 MUST 路由至 Discovery 頁

#### Scenario: 既有路由不受影響

- **WHEN** 新增 Discovery 路由後開啟 `/factors`
- **THEN** `FactorReport` 頁 MUST 維持原行為與內容
