## Context

Stage 0 探索目前流程：

```
Stage 0a evidence ─┐
                   ├─→ stage0_discovery.py
                   │      │
                   │      ├─ call crypto_factor_lab swarm
                   │      ├─ parse ```json fence from swarm stdout
                   │      │   └─ retry once on parse fail
                   │      ├─ validate FactorCandidate schema
                   │      ├─ validate feature_key ∈ feature store cols
                   │      └─ write candidates_<sym>.json
                   │
                   └─ on full fail → write candidates_<sym>.failed.json
                                    + stage 1 falls back to legacy 3 factors
```

實測 swarm 對「吐 ```json fence」的 reliability ≈ 60–80%；BTC 本次 retry 才過、ETH 兩次都掛、SOL 之前同樣。retry 救率有限因根因是 LLM 非確定性 + prompt 工程瓶頸。

下游 stage 1 早已支援 `RESEARCH_LEGACY_FACTORS=1` fallback，但 legacy 是固定 3 因子（funding_rate / oi_change_24h / fng）跳過真實 evidence，對新因子（stablecoin_supply_z、funding_z 等衍生因子）無感 → 直接擋住 BTC s7/s9 archetype 的自動發現。

## Goals / Non-Goals

**Goals:**
- Stage 0 在 swarm 失敗（或不啟用 swarm）時仍能產出有意義的 `candidates_<sym>.json`，依 evidence IC 自動挑因子。
- 維持下游介面零變動（stage 1+ 無需任何 patch）。
- 保留 swarm 對 `economic_logic` 散文 enrichment 的價值（人類可讀解釋），但不讓 swarm 失敗阻塞 pipeline。
- 容許未來新增 archetype 規則時，只動 selector 模組不動 stage 0 orchestrator。

**Non-Goals:**
- 改寫 `agent/swarms/crypto_factor_lab` swarm prompt 或加 tool-use schema 強制 JSON（留 follow-up change）。
- 動 schema (`FactorCandidate` / `CandidatesManifest`) — 結構不變。
- 動 stage 1 以後任何階段 — `candidates_<sym>.json` 介面契約是邊界。
- 自動化 archetype 選擇（stage 2 多策略 fan-out 是另一個 change）。

## Decisions

### D1: Deterministic 主、swarm 副 — 翻轉預設

**選**: code 先依 evidence 跑 `select_candidates_from_evidence()` 產完整 `FactorCandidate` list（含合理 `economic_logic` placeholder），再選擇性呼叫 swarm「為這 K 個因子寫散文」、swarm 失敗就用 placeholder。

**為何不**：
- (A) 保留 swarm 主 + 加更多 retry / prompt 強化 → 救不到 100%，本問題本質。
- (B) 改 swarm 用 tool-use JSON schema 強制結構 → 需動 `agent/swarms/`，且 vibe-trading provider 透過 OpenRouter 中介，tool-use 支援度需驗（memory `vibe_trading_provider_constraint`）。範圍超出本 change。
- (C) 全砍 swarm → 失去 economic_logic 解讀，且未來若 tool-use 修好可無縫加回。

### D2: 因子挑選規則 — `select_candidates_from_evidence`

純函式 signature:

```python
def select_candidates_from_evidence(
    evidence: EvidenceManifest,
    feature_store_cols: set[str],
    *,
    min_abs_ic: float = 0.05,
    min_abs_ir: float = 0.10,
    max_candidates: int = 6,
    horizon_consistency_required: bool = False,
) -> list[FactorCandidate]:
    ...
```

挑選規則（依序）:
1. 對每個 evidence entry 求 `top_horizon = argmax_h |IC[h]|`、`top_abs_ic = |IC[top_h]|`。
2. 過 `top_abs_ic ≥ min_abs_ic` 且 `|IR| ≥ min_abs_ir` 兩道門檻。
3. 過 `feature_key ∈ feature_store_cols`（含 ic_eval_transform 後對應的 stored key）。
4. 依 `top_abs_ic` 降冪排序，取前 `max_candidates`。
5. 每個 candidate 產生：
   - `expected_ic_sign` = `'+'` if IC[top_h] > 0 else `'-'`
   - `horizons_h` = `[top_h]` 與其鄰近 horizon（依 IC 同向且 |IC| > min_abs_ic/2）
   - `economic_logic` = 預設 placeholder：`"(deterministic selection) {feature_key} top |IC|={ic:.4f}@{h}h, IR={ir:.3f}. Awaiting swarm enrichment."`
   - `name` = `feature_key`（swarm enrichment 可改）
   - `category` 取自 evidence entry

門檻 (`min_abs_ic`、`min_abs_ir`、`max_candidates`、`horizon_consistency_required`) **MUST** 可從 `research_config.yaml` 的 `stage0_selector` 區塊覆寫；無此區塊時用預設值。

### D3: Swarm 角色 — enrichment only

新增 `enrich_candidates_with_swarm(candidates, evidence) -> list[FactorCandidate]`:
- 構造一個簡化 prompt：「為以下因子寫 economic_logic 散文，回傳 JSON 陣列 `[{feature_key, economic_logic, ...}]`」。
- 若 swarm 成功且 JSON 解析通過：對每個 candidate 以 feature_key 匹配，更新 `economic_logic`（其他欄不動）。
- 若 swarm 失敗 / 解析失敗 / 部分 match 失敗：**保留原 candidate 不動，log warning，不 raise**。
- 整個 enrichment 流程包在 `try/except Exception`，pipeline 不因 swarm 中斷。

CLI flag:
- `--use-swarm` (預設 off)：啟用 enrichment。
- `--no-swarm` (預設 on)：純 deterministic。
- 等價於 env var `RESEARCH_STAGE0_USE_SWARM=1/0`。

### D4: 移除 legacy hardcoded fallback

stage 1 的 `RESEARCH_LEGACY_FACTORS` 環境變數行為改為 no-op（讀但不行動，避免 break 既有 shell scripts）。新行為：candidates 永遠存在（即使 0a evidence 弱），最差情況是 `candidates_<sym>.json` 中 `candidates` 陣列為空 + stdout 顯眼警告 `[stage0] {sym}: 0 factors passed IC/IR threshold; lower thresholds or expand feature pool`，但檔案存在、schema 合法、exit code 0。

### D5: `candidates_<sym>.failed.json` 行為

完全移除 `failed.json` 寫出（不再有概念上的「失敗」狀態）。Stage 1 偵測 `failed.json` 的程式碼路徑可保留（向後相容歷史檔案）但 stage 0 不再產此檔。

### D6: 快取行為

維持 `discovery_cache_days` 邏輯：若 `candidates_<sym>.json` 之 `generated_at` 在快取期內、且 evidence_<sym>.json 未更新 → 跳過。`--force` / `RESEARCH_FORCE_DISCOVERY=1` 強制重跑。

## Risks / Trade-offs

- **[Risk] 規則僵化**: deterministic 依 `|IC| ≥ 0.05` 可能漏掉 marginal 因子（如 basis_rel @ 0.019）。
  → **Mitigation**: 門檻可由 config 覆寫；用戶可降到 0.02。Memory `project_perp_derived_factors_run` 顯示 basis_rel 是有用 gate 但 IC 低，需手動加 config 救。

- **[Risk] 失去 LLM 對「組合因子」的創造力**: swarm 原本可呼叫 `append_feature_column` 自製複合因子。Deterministic 模式只能挑既有 evidence 中欄位。
  → **Mitigation**: enrichment 模式仍可（但目前 swarm 也很少這樣做）。長期解法是 stage 0a 預先生成更多衍生因子放進 feature store。

- **[Risk] 多重檢驗放大**: max_candidates=6 + stage 4 200-combo sweep = 1200 backtest，IC 灌水機率上升。
  → **Mitigation**: 預設 max_candidates=6 已是保守值；evidence 表本身有 multiple-testing caveat 提醒；stage 5 walk-forward OOS 是真檢驗。

- **[Trade-off] 失去 swarm 主導時的「對抗式審查」**: skeptic agent 原本可拒一些 marginal candidate；deterministic 沒有此把關。
  → **Acceptance**: Stage 1 verdict gate (single_use / ensemble_only / reject) 本就是真實的審查層；skeptic 在實務上常 PASS 全部，價值不高。

- **[Trade-off] 預設關 swarm → 用戶失去 economic_logic 散文**: 跑 `--use-swarm` 才有。
  → **Acceptance**: PIPELINE.md 標清；對新手 (memory `user_profile`) 預設關 swarm = 跑更穩，文字解讀可日後手動補。

## Migration Plan

1. 新增 `research/pipeline/lib/factor_selector.py`（純函式 + 單測），不接 stage 0。
2. 改寫 `research/pipeline/stage0_discovery.py`：deterministic 路徑 + 可選 enrichment + CLI flag + 警告訊息。
3. 跑 BTC / ETH / SOL 三個 symbol 驗證 deterministic 路徑產出與既有手寫 `candidates_<sym>.json` 大致一致。
4. 跑 `--use-swarm` 驗證 enrichment 失敗時不中斷。
5. 更新 PIPELINE.md。
6. （Rollback 策略）保留舊 swarm-primary 路徑於 git history；若需回退僅 revert `stage0_discovery.py` 一檔。
