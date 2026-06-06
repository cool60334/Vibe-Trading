## 1. emit_manifest helper extraction

- [x] 1.1 在 `research/emit_manifest.py` 加新函式 `emit_manifest_for_strategy(strategy_id: str, entry: StrategyRunsEntry, runs_root: Path, manifests_dir: Path) -> Path`
- [x] 1.2 helper 內呼叫既有 `build_strategy_manifest(...)`、mkdir、JSON serialize、write、return path
- [x] 1.3 重構 `main()` 改用此 helper，刪除重複 write 邏輯
- [x] 1.4 跑既有 `research/tests/test_emit_manifest.py`（若有）驗 backwards compat

## 2. emit_manifest helper tests

- [x] 2.1 在 `research/tests/test_emit_manifest.py`（或新建）加 `TestEmitManifestForStrategy` class
- [x] 2.2 scenario：valid inputs → returned path 等於 expected，檔案存在，內容 validates against `StrategyManifest`
- [x] 2.3 scenario：strategy 目錄不存在 → 自動建立後寫入
- [x] 2.4 跑 pytest 全綠

## 3. Stage 5 emission hook

- [x] 3.1 在 `research/pipeline/stage5_select.py::main()` 寫完 `selection.json` 之後、`sys.exit(...)` 之前加 emission loop
- [x] 3.2 import `emit_manifest_for_strategy` from `research/emit_manifest.py`（注意 sys.path bootstrap）
- [x] 3.3 遍歷 `strategy_runs.json` 全部 strategies（不只 eligible），每個包 try/except Exception
- [x] 3.4 失敗時 print 紅字警告 `[stage5] {sid}: manifest emit failed ({exc})`、continue
- [x] 3.5 成功時 print `  [OK] {sid} → {relative_path}`
- [x] 3.6 完成後 print summary `Emitted: N/M manifests successfully.`
- [x] 3.7 runner exit code 規則不動（仍以 `selection.json` 有效性決定，per-strategy 失敗 NOT 影響）

## 4. Stage 5 emission tests

- [x] 4.1 在 `research/tests/test_stage5_select.py` 加 `TestManifestEmission` class
- [x] 4.2 scenario：3 strategies 全成功 → 3 個 manifest.json 寫出 + selection.json 仍寫出 + exit code 0
- [x] 4.3 scenario：idempotent — 跑兩次 stage 5、第二次 manifest.json 被覆寫成新內容
- [x] 4.4 scenario：one strategy raise（mock `build_strategy_manifest` 對某 sid raise）→ 其他 sid 仍寫出 + exit code 0 + stderr 含警告
- [x] 4.5 scenario：stdout 含 `Emitted: N/M manifests successfully` 行
- [x] 4.6 跑 pytest 全綠（保留既有 test_stage5_select.py 既有 tests 不退化）

## 5. End-to-end smoke

- [x] 5.1 跑 `python -m research.pipeline.stage5_select`
- [x] 5.2 驗 `research/manifests/eth_s5_half_size/manifest.json` 存在且 schema OK
- [x] 5.3 驗 `research/manifests/btc_s9_stablecoin_funding_gated_tuned/manifest.json` 存在
- [x] 5.4 驗 `research/manifests/eth_s3_dynamic_exit/manifest.json` 存在
- [x] 5.5 ~~dashboard smoke~~ — deferred: requires live server; manifest.json emitted+schema-validated; promote endpoint confirmed at dashboard/server/main.py:194
- [x] 5.6 ~~POST promote smoke~~ — same deferral; manifest present for all 8 strategies

## 6. Docs

- [x] 6.1 更新 `research/PIPELINE.md` Stage 5 章節：說明 stage 5 完會自動 emit `manifest.json`、dashboard 因此可見
- [x] 6.2 加註：`manifest.json` 為 derived artefact、勿手動編輯（會被下次 stage 5 覆寫）
- [x] 6.3 在「執行順序」表加說明 stage 5 額外產出物含 per-strategy manifest

## 7. Memory / 收尾

- [x] 7.1 在 memory 新增 `project_stage5_emit_strategy_manifest_adr.md`
- [x] 7.2 更新 `MEMORY.md` 索引
- [x] 7.3 跑 `openspec validate stage5-emit-strategy-manifest` 驗合法
