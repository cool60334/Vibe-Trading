## Why

`evidence-driven-factor-discovery` 變更新增兩個「證據驅動」流程的核心產物,但 dashboard 完全看不到它們:

- `evidence_<sym>.json` — 整批指標池對 forward return 的多 horizon IC/IR 排名表。這是「agent 看到的真實證據」,是整個翻轉(盲提案→證據驅動)的關鍵憑證,目前前端無任何呈現。
- `candidates_<sym>.json` — researcher agent 的提案(含 `economic_logic`、`feature_key`、`expected_ic_sign`)。schema 已在 `dashboard/server/schemas.py`,但無 API endpoint、無頁面。

結果:使用者只能在 `FactorReport` 看到 stage1 最終存活的因子,看不到 (1) 背後整池證據長相、(2) agent 為何挑這些、(3) 候選→最終因子的 provenance。本變更把這兩個產物接上後端 API 與前端頁面,讓證據與探索決策可視。

## What Changes

- **後端**:`dashboard/server/artifacts.py` 新增 `load_evidence_table(repo_root, symbol)` 與 `load_candidates_manifest(repo_root, symbol)` loader(沿用既有 graceful-None 模式);`main.py` 新增 `/api/evidence`、`/api/candidates` 兩條 read-only route;`schemas.py` 新增 `EvidenceTable` / `EvidenceEntry` pydantic 模型(對應 `evidence_<sym>.json` JSON 契約)。
- **前端**:`api.ts` 新增型別與 client method;新增 **Discovery 頁**呈現 (a) evidence 整池 IC 排名表(可依 horizon/IC 排序、heatmap 配色、caveat banner)與 (b) candidate provenance 區塊(`economic_logic` / `feature_key` / `expected_ic_sign` / `category`);`Layout.tsx` 加導覽連結、`router.tsx` 加路由。
- **空狀態**:無 evidence/candidates 產物時顯示提示(先跑 stage0a / stage0 swarm),不報錯。
- **不動**:pipeline、stage0a/stage0/stage1 程式、既有 4 頁(Compare/FactorReport/StrategyDetail/Testnet)行為不變;evidence/candidates 為純讀取展示。

## Capabilities

### New Capabilities
- `evidence-dashboard-view`: 將 `evidence_<sym>.json`(整池 IC 排名)與 `candidates_<sym>.json`(agent 提案 + 經濟邏輯 + feature_key)經 FastAPI read-only endpoint 暴露,並在前端 Discovery 頁以排序表 + caveat banner + provenance 區塊呈現的契約。

## Impact

- 依賴:`evidence-driven-factor-discovery`(產出 `evidence_<sym>.json` / `candidates_<sym>.json` 與 `FactorCandidate.feature_key`);本變更只消費其 JSON 契約。
- 程式:`dashboard/server/artifacts.py`、`dashboard/server/main.py`、`dashboard/server/schemas.py`、`dashboard/web/src/lib/api.ts`、新增 `dashboard/web/src/pages/Discovery.tsx`、`dashboard/web/src/router.tsx`、`dashboard/web/src/components/layout/Layout.tsx`。
- 測試:`dashboard/server/` 新增 evidence/candidates loader + endpoint 測試;sample_data 補 `evidence_<sym>.json` 範例。
- 不影響:pipeline 全部 stage、signal_engine、既有 dashboard 頁面行為。
