## Why

Stage 0 因子探索目前以 LLM swarm (`crypto_factor_lab`) 作為主導；swarm 偶爾不吐 ```` ```json ```` fence 而 JSON 解析失敗（已知 issue，memory `feedback_swarm_json_fence_fail`）。BTC 本次跑 retry 才過、ETH 跑兩次都掛、SOL 之前也撞同樣問題。失敗 fallback 是 legacy hardcoded 3 因子或人工手寫 `candidates_<sym>.json`，前者跳過真實 evidence 等於浪費 Stage 0a，後者需 claude 介入無法自動化。

根因是「LLM 寫結構化 JSON」這條路徑本質不確定，再多 prompt 工程也救不到 100%。翻轉預設讓 deterministic 從 evidence 直接挑因子為主、swarm 退為文字 enrichment（寫 economic_logic 散文）就能拔掉 fail mode、同時保留 LLM 的解讀價值。

## What Changes

- **Stage 0 預設改 deterministic top-K from evidence**：新增 `research/pipeline/lib/factor_selector.py` 純函式模組，依 `|IC|` 門檻 + IR 一致性 + 跨 horizon 穩定性自動挑 K 個因子，產出符合 `FactorCandidate` schema 的物件。
- **Swarm 降為 enrichment role**：`stage0_discovery.py` 先 deterministic 挑因子組好結構，再（可選）呼叫 swarm 為每個因子寫 `economic_logic` 散文與 `expected_ic_sign` 標註；swarm 失敗 → `economic_logic` 留 placeholder 字串、`expected_ic_sign` 取自 evidence IC 符號、pipeline 繼續，**不再 raise / 不再產 failed.json**。
- **Legacy hardcoded 3-factor fallback 移除**：原 `RESEARCH_LEGACY_FACTORS` 環境變數行為改為 no-op（保留變數避免破環境配置），deterministic 模式為唯一路徑。
- **Schema 與下游介面不變**：`CandidatesManifest` / `FactorCandidate` schema 與 `candidates_<sym>.json` 檔名/位置/內容結構 **不動**，stage 1+ 全鏈零改動。
- **PIPELINE.md 更新**：說明新預設行為、swarm 角色降級、`--use-swarm` flag 控制是否走 enrichment。

## Capabilities

### New Capabilities

（無 — 本改是既有 capability 的行為修正）

### Modified Capabilities

- `factor-discovery`: Stage 0 swarm 探索流程的主導邏輯翻轉為 deterministic 主、swarm enrichment 副；新增 deterministic candidate builder 需求；移除「swarm 失敗 → exit 1」要求改為「swarm 失敗 → enrichment 留空、繼續」。

## Impact

**改動範圍**（僅 research/ 子樹）:
- `research/pipeline/stage0_discovery.py` — 重寫主流程
- `research/pipeline/lib/factor_selector.py` — 新增純函式 (`select_candidates_from_evidence`、`rank_features`、`derive_horizons`)
- `tests/research/pipeline/test_stage0_discovery.py` — 加 deterministic 路徑測試
- `tests/research/pipeline/lib/test_factor_selector.py` — 新單測檔
- `research/PIPELINE.md` — Stage 0 章節改寫

**不變**:
- `dashboard/server/schemas.py` (`FactorCandidate` / `CandidatesManifest`)
- `research/pipeline/stage1_factors.py` 與後續所有階段
- `agent/swarms/crypto_factor_lab.*` swarm 定義（可選 follow-up 簡化，不在本 change 範圍）
- `backtest/runner.py`、`dashboard/` UI、`vibe-trading run` CLI
- `candidates_<sym>.json` 檔案結構與位置

**Breaking changes**: 無。Stage 1+ 看不到差異。CLI flag `--force` 行為不變、加新 `--use-swarm/--no-swarm` flag（預設 `--no-swarm` 為純 deterministic、`--use-swarm` 啟用 enrichment）。

**風險**:
- deterministic 挑因子規則需保留可調性（不能寫死 K=5），避免日後若加新因子需動 code。改用 `research_config.yaml` 區塊 `stage0_selector` 控制 K、|IC| 門檻、IR 門檻。
- 移除 legacy fallback 後若 evidence 本身爛（例：feature store 空、全 NaN），需明確 error message 而非靜默產 0 candidates。
