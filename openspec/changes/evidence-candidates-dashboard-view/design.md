## Context

`evidence-driven-factor-discovery` 把因子發掘翻成證據驅動,新增 `evidence_<sym>.json`(整池 IC 排名)與 `candidates_<sym>.json`(agent 提案)。dashboard 既有架構:FastAPI(`artifacts.py` loader + `main.py` route + `schemas.py` pydantic)+ React/Vite(`api.ts` client + `pages/*` + `router.tsx` + `Layout.tsx` nav)。既有 `FactorReport` 只讀 `factor_<sym>.json`。新產物無 endpoint、無頁面。

約束:read-only 展示(不寫 pipeline);沿用既有 graceful-None loader 與 `/api/factor-analysis` route 模式;schema 來源單一(`dashboard/server/schemas.py`);本變更不得改既有 4 頁行為。

## Goals / Non-Goals

**Goals:**
- evidence 整池 IC 排名可在前端檢視(排序、horizon 切換、IC 配色)。
- candidate 提案的經濟邏輯與 feature_key provenance 可視。
- caveat 明示(IC 僅篩選用途、未做交易成本與 multiple-testing 校正)。

**Non-Goals:**
- 不改 pipeline / stage0a / stage0 / stage1 程式。
- 不畫 features parquet 原始序列(資料量大,非展示資料)。
- 不做 evidence/candidates 的編輯或回寫(純讀)。
- 不改既有 Compare/FactorReport/StrategyDetail/Testnet 行為。

## Decisions

**D1. Loader 沿用既有 artifacts.py 模式。**
新增 `load_evidence_table` / `load_candidates_manifest`,與既有 `get_factor_manifest` 一致:讀 JSON、pydantic `model_validate`、失敗回 `None`/空。理由:零新風險,與既有測試風格一致。

**D2. 兩條 read-only endpoint,mirror /api/factor-analysis。**
`/api/evidence`(可選 `?symbol=`,預設回全部 symbol 陣列)、`/api/candidates`(同)。理由:前端依 symbol tab 切換,陣列回傳最省往返。

**D3. 獨立 Discovery 頁,而非塞進 FactorReport。**
evidence 整池可達數十至上百列,與 FactorReport 的「精選存活因子」性質不同。獨立頁含兩區:(a) evidence IC 排名表、(b) candidate provenance 清單。理由:關注點分離、FactorReport 不被撐爆;同頁並列讓「整池證據 → agent 挑選」敘事連貫。替代案「全塞 FactorReport」會混淆精選與全池,否決。

**D4. EvidenceTable schema 落在 dashboard/server/schemas.py。**
與既有 `FactorManifest`/`CandidatesManifest` 同檔,維持 schema 單一來源。欄位對齊 `evidence-driven-factor-discovery` 的 JSON 契約(`feature_key`/`category`/`ic_by_horizon`/`ir`/`sample_size`)。研究端若另有 pydantic 模型,兩者共享同一 JSON 契約即可,不需共用 class。

**D5. caveat banner 重用既有 amber banner 樣式。**
沿用 `FactorReport` cross-regime 警示的 amber banner 視覺,明示 IC 僅篩選用途。理由:一致 UX、低成本傳達過擬合風險。

**D6. 配色/排序重用既有 IcCell 邏輯。**
IC 配色門檻(≥0.10 強 / ≥0.05 中)直接重用 `FactorReport` 的 `IcCell`,抽成共用元件或複製常數。理由:跨頁一致解讀。

## Risks / Trade-offs

- **evidence 列數大 → 表格效能** → mitigation:預設只顯示 top-N(依 max|IC|,沿用 evidence 表既有排序)+ 「展開全部」;前端不做重運算。
- **依賴上游產物格式** → mitigation:loader graceful-None + 前端空狀態,缺檔/格式變不報錯;endpoint 測試覆蓋缺檔情境。
- **schema 漂移(兩變更各持一份 evidence model)** → mitigation:以 JSON 欄位契約為準,dashboard 測試固定範例 JSON;欄位變動兩邊同步。
- **sample_data 缺 evidence 範例 → 前端 demo 空白** → mitigation:本變更補 `sample_data` evidence/candidates 範例檔。

## Migration Plan

加法式、可獨立部署(不影響既有頁):
1. 後端:加 schema + loader + 2 endpoint + 測試。
2. 前端:加 api.ts 型別/method + Discovery 頁 + route + nav。
3. 補 sample_data 範例,本機 `REPO_ROOT=../sample_data` 驗證頁面。
4. 既有 4 頁回歸(行為不變)。

回退:移除 nav link 與 route 即可隱藏新頁,後端 endpoint 為純讀無副作用,可留可移。

## Open Questions

- evidence 表 heatmap vs 純數字表:首版用數字表 + IC 配色,heatmap 視覺待實測列數後再決定。
- candidate → factor 的 provenance 連結:首版以 `feature_key` 文字對應,是否在 FactorReport 加反向連結待使用者回饋。
