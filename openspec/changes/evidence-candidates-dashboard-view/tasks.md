## 1. 後端 schema

- [ ] 1.1 `dashboard/server/schemas.py` 新增 `EvidenceEntry`（feature_key/category(既有 Literal)/ic_by_horizon/ir/sample_size）與 `EvidenceTable`（symbol/generated_at/horizons_h/entries）pydantic 模型
- [ ] 1.2 單元測試 `dashboard/server/test_schemas_evidence.py`:合法 evidence JSON 驗證通過、缺必填欄 raise、非法 category raise

## 2. 後端 loader 與 endpoint

- [ ] 2.1 `dashboard/server/artifacts.py` 新增 `load_evidence_table(repo_root, symbol)` 與 `list_evidence_tables(repo_root)`，沿用既有 graceful-None 模式
- [ ] 2.2 `artifacts.py` 新增 `load_candidates_manifest(repo_root, symbol)` 與 `list_candidates_manifests(repo_root)`（重用既有 `CandidatesManifest`）
- [ ] 2.3 `dashboard/server/main.py` 新增 `GET /api/evidence`（全 symbol 陣列 + 選填 `?symbol=`）
- [ ] 2.4 `main.py` 新增 `GET /api/candidates`（全 symbol 陣列 + 選填 `?symbol=`）
- [ ] 2.5 endpoint 測試 `dashboard/server/test_main.py`（或新檔）:有產物回 200 + 正確 body、缺產物回空陣列不 500、`?symbol=` 過濾正確

## 3. sample_data 範例

- [ ] 3.1 `dashboard/sample_data/research/manifests/` 補 `evidence_btc.json` 與 `candidates_btc.json` 範例（含 feature_key/economic_logic），供本機 `REPO_ROOT=../sample_data` demo

## 4. 前端 API client

- [ ] 4.1 `dashboard/web/src/lib/api.ts` 新增 `EvidenceEntry`/`EvidenceTable`/`FactorCandidate`/`CandidatesManifest` 型別（mirror schemas.py）
- [ ] 4.2 `api.ts` 新增 `api.evidence(symbol?)` 與 `api.candidates(symbol?)` client method

## 5. 前端 Discovery 頁

- [ ] 5.1 新增 `dashboard/web/src/pages/Discovery.tsx`:evidence 整池 IC 排名表（列=feature_key,欄=category/ir/sample_size + 各 horizon IC），預設依 max |IC| 排序
- [ ] 5.2 IC 配色重用 `FactorReport` 的 `IcCell` 門檻（≥0.10 強 / ≥0.05 中）— 抽共用元件或複製常數
- [ ] 5.3 caveat banner（IC 僅篩選用途、未做交易成本與 multiple-testing 校正）+ 多 symbol tab 切換 + 大表 top-N 預設/展開
- [ ] 5.4 candidate provenance 區塊:每 candidate 顯示 name/feature_key/category/expected_ic_sign/economic_logic;feature_key 命中 evidence 時標示其 IC/排名
- [ ] 5.5 空狀態:無 evidence/candidates 時顯示提示（指引先跑 stage0a / stage0 swarm），不報錯

## 6. 路由與導覽

- [ ] 6.1 `dashboard/web/src/router.tsx` 加 Discovery 路由（`path: "discovery"`）
- [ ] 6.2 `dashboard/web/src/components/layout/Layout.tsx` `NAV` 加 Discovery 連結（lucide icon）
- [ ] 6.3 確認既有 `/`、`/factors`、`/testnet`、`strategies/:id` 行為不變

## 7. 驗證

- [ ] 7.1 `cd dashboard/server && pytest -q` 全綠（含新 evidence/candidates 測試）
- [ ] 7.2 本機 `REPO_ROOT=../sample_data` 啟動前後端,Discovery 頁正確顯示 evidence 排名 + candidate provenance + caveat
- [ ] 7.3 回歸:Compare/FactorReport/StrategyDetail/Testnet 四頁行為不變
