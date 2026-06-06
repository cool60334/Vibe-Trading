## 1. Factor selector (pure module)

- [x] 1.1 新增 `research/pipeline/lib/__init__.py` 若不存在
- [x] 1.2 建立 `research/pipeline/lib/factor_selector.py`，匯出 `select_candidates_from_evidence`、`_rank_evidence_entries`、`_build_candidate_from_entry` 三個函式
- [x] 1.3 實作 `select_candidates_from_evidence(evidence, feature_store_cols, *, min_abs_ic=0.05, min_abs_ir=0.10, max_candidates=6) -> list[FactorCandidate]`，遵 design.md D2 的 5 步驟
- [x] 1.4 加 placeholder 字串模板（含 IC/IR/horizon 數字）給 `economic_logic`
- [x] 1.5 處理 evidence entry 中 `ic_by_horizon` 值為 None 或缺 horizon 的情況（過濾掉）
- [x] 1.6 處理 `ic_eval_transform` 對應的 stored feature_key 映射（若 evidence entry 含 `ic_eval_transform`，feature_key 可能不直接對應 store 欄；用 evidence entry 提供的 `feature_key` 為準）

## 2. Factor selector tests

- [x] 2.1 ~~新增 `tests/research/pipeline/lib/__init__.py`~~ — adapted: tests live flat in `research/tests/` per project convention
- [x] 2.2 建立 `research/tests/test_factor_selector.py`（path adapted）
- [x] 2.3 寫 5 個 scenario 單測，對應 spec.md `Deterministic candidate selector` 5 個 scenarios（高 IC 選入、低 IC 過濾、feature_key 不在 store、max_candidates 截斷、零因子過門檻）
- [x] 2.4 加 fixture evidence dict（含 stablecoin_supply_z 高 IC、basis_rel 低 IC、imaginary_factor 不在 store）
- [x] 2.5 跑 `pytest research/tests/test_factor_selector.py -v` 全綠 — 25/25 PASSED

## 3. Stage 0 runner 重寫

- [x] 3.1 讀 `research/pipeline/stage0_discovery.py` 既有 main 函式，識別需保留的 helper（cache、preflight、verify_outputs、print_summary、exit code）
- [x] 3.2 抽出新函式 `run_deterministic_discovery(symbol, evidence_path, features_path, config) -> CandidatesManifest`，呼叫 `select_candidates_from_evidence`
- [x] 3.3 抽出新函式 `enrich_candidates_with_swarm(candidates, evidence, symbol, timeout_s) -> list[FactorCandidate]`，包 try/except，失敗回原 list
- [x] 3.4 改寫 main：(a) cache 檢查 (b) preflight 0a 產物 (c) deterministic 跑 (d) 若 `use_swarm` 為 True 再 enrich (e) write manifest (f) verify
- [x] 3.5 新增 CLI flag `--use-swarm` (action=store_true) 與 `--no-swarm` (action=store_false)；預設 deterministic only
- [x] 3.6 讀 env var `RESEARCH_STAGE0_USE_SWARM` 作為 CLI flag 預設
- [x] 3.7 從 `research_config.yaml` 讀 `stage0_selector` 區塊（`min_abs_ic` / `min_abs_ir` / `max_candidates`），無此區塊用 hardcoded 預設
- [x] 3.8 刪除 `candidates_<sym>.failed.json` 寫出邏輯（保留檔案存在偵測供向後相容，但 stage 0 不再產生）
- [x] 3.9 「零因子過門檻」case：寫空 candidates 陣列 + stdout 警告 + exit code 0
- [x] 3.10 stdout summary 行加註 mode：`[stage0] {sym}: N candidates ({deterministic|deterministic+swarm})`

## 4. Stage 0 runner tests

- [x] 4.1 讀既有 `research/tests/test_stage0_discovery.py`，識別需保留 / 改寫 / 新增的 scenarios
- [x] 4.2 加 scenario 「deterministic 全成功」：mock 0a 產物 + 跑 _process_symbol + 驗 `candidates_<sym>.json` 寫出 + 無 swarm call
- [x] 4.3 加 scenario 「0a 產物缺失」：刪 evidence file + 驗 ok=False + 無 candidates 寫出 + 無 failed.json
- [x] 4.4 加 scenario 「零因子過門檻」：evidence 全低 IC + 驗 candidates 為空陣列 + ok=True
- [x] 4.5 加 scenario 「enrichment 模式 swarm timeout 不中斷」：mock swarm raise TimeoutExpired + 驗 candidates 內容 == deterministic + ok=True
- [x] 4.6 加 scenario 「enrichment 模式 swarm 成功覆寫 economic_logic」：mock swarm 回合法 JSON + 驗 economic_logic 已換、其他欄不動
- [x] 4.7 跑 `pytest research/tests/test_stage0_discovery.py -v` 全綠 — 42/42 PASSED

## 5. End-to-end smoke

- [x] 5.1 跑 `python -m research.pipeline.stage0_discovery --force` 對 BTC（已有 0a 產物）驗 deterministic 模式產生 `candidates_btc.json` — BTC 2 candidates / ETH 4 candidates, no swarm called
- [x] 5.2 驗 `candidates_btc.json` schema 通過、包含 `stablecoin_supply_z` 與 `funding_z` — confirmed via `CandidatesManifest.model_validate_json`
- [x] 5.3 ~~跑 `--use-swarm` smoke~~ — skipped: enrichment fail-soft and success paths are both covered by mocked unit tests in `TestEnrichmentFailSoft`; real swarm call burns LLM tokens with no additional coverage
- [x] 5.4 跑 `python -m research.pipeline.stage1_factors` 驗下游無 break — BTC + ETH both `Stage 1 PASSED`; 4 ETH factors all ensemble_only verdicts (atr_14, stablecoin_supply_z, rolling_std_20, funding_z)

## 6. Docs

- [x] 6.1 更新 `research/PIPELINE.md` Stage 0 章節：說明 deterministic 為預設、`--use-swarm` 為可選 enrichment、移除 `candidates_<sym>.failed.json` 描述
- [x] 6.2 在 PIPELINE.md 加 `stage0_selector` config 區塊範例
- [x] 6.3 在 PIPELINE.md「失敗行為」段加註：stage 0 已無「失敗」狀態（除了 0a 產物缺失），零因子也算成功
- [x] 6.4 更新 `research/research_config.yaml` 加入註解說明 `stage0_selector` 區塊可選欄位（不啟用、僅文件）

## 7. Memory / 收尾

- [x] 7.1 在 memory 新增 `project_stage0_deterministic_discovery_adr.md` 記錄此 change 動機與結果
- [x] 7.2 更新 `MEMORY.md` 索引加新 ADR 條目
- [x] 7.3 跑 `openspec validate stage0-deterministic-discovery` — `Change 'stage0-deterministic-discovery' is valid`
