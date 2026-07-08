# Talos Phase 1 — Factor Foundry 全景概覽計畫

> 層級：**roadmap 概覽**（子系統切分 / 依賴序 / 介面契約 / 埋雷清單）。逐子系統的 TDD 細節各有獨立計畫檔。
> 前置：Phase 0 五地基模組已完成並驗證全綠（`research/hermes/` pit/split/candidate_store/sandbox_ast/sandbox）。
> 設計來源：`docs/talos/talos-design.md` §3.6 Phase 1 + 附錄 C（agy 二審 11 點）+ agy Phase 1 概覽二審（5 子系統 + 序 + 5 雷）。

**Goal:** 建自主因子鑄造廠——從假設佇列到統計守門到證據卡，讓 LLM 在 Phase 0 沙盒/PIT 護欄內安全發明因子，過關寫隔離候選庫、失敗進墓地，**production 由人工閘 promote**。

---

## 子系統地圖（5 個，agy 概覽二審定案）

| 子系統 | 責任 | 性質 | 建議模組 |
|--------|------|------|----------|
| **1E 證據卡 + 晉升閘** | 定義合格因子的產出 schema（EvidenceCard）+ 人工 promote 閘（候選→production，唯一碰 production features 的路徑） | 確定性 + human-in-loop | `research/hermes/evidence_card.py`、`research/hermes/promote.py` |
| **1A 統計守門** | 全部嚴格統計檢驗：Net-return IC / 非重疊 IC / regime+分年 IC / **abs(Spearman) 數值去重（含墓地）** / DSR+PBO（ledger 全域 trial） | 純確定性 | `research/hermes/gatekeeper.py`（擴充既有 `factor_metrics.py`） |
| **1B 假設佇列 + 靜態預過濾** | 4 來源收集（zoo 452 遷移 / LLM 發想 / 高IC衍生 / 學術）+ 字串/AST 層墓地去重（省 token，數值去重歸 1A） | 確定性 | `research/hermes/hypothesis_queue.py` |
| **1C LLM 產碼 + 修復引擎** | 有界修復迴圈：LLM 產碼→PIT 樣板注入→沙盒跑→錯誤回饋重試 ≤N | LLM 創意點 | `research/hermes/foundry_engine.py` |
| **1D 任務編排** | 生命週期 + 按需/nightly cron + compute/token 預算 + early stopping + write-file→reconcile job | 確定性 | `research/hermes/orchestrator.py` |

**關鍵調整（vs 初版切分）：**
- **新增 1E**：AI 無最終簽署權，真錢系統必須 human-in-loop 作最後防線；證據卡是 write-file→reconcile 的標準 payload。
- **相關性/墓地數值去重從 1B 移 1A**：`abs(Spearman)>0.7` 需先在沙盒跑出**數值序列**才能比；1B 階段只有字串/公式，只能做字串/AST 層預過濾。

---

## 實作順序：1E → 1A → 1B → 1C → 1D

**嚴格相依反向序**（先定「成功長怎樣」，再寫「產出它」的邏輯）：

1. **1E 證據卡 schema** — 先定合格因子的資料結構，決定 1A 吐什麼、`candidate_store` 存什麼。晉升閘比照 `final_holdout.py` 模式（唯一碰 production 的手動路徑）。
2. **1A 統計守門** — 純確定性數學，餵假因子序列 TDD 驗 Net-return/非重疊/DSR 正確。防 LLM 垃圾的核心防線。（依賴 1E schema）
3. **1B 佇列 + 靜態過濾** — 建靜態防護網（字串去重），省 1C token。（可與 1A 平行）
4. **1C LLM 產碼 + 修復** — 最複雜、不確定。有 1A 守門 + Phase 0 沙盒才能安全讓 LLM 亂撞。（依賴 1A/1B/Phase 0）
5. **1D 編排** — 串成 cron + CLI，加預算 + early stopping。（依賴全部）

---

## 介面契約（子系統接點）

```
1B Hypothesis(id, description, source, logic_ast_hash)
      │  已剔字串/AST 層墓地重複
      ▼
1C   run(Hypothesis, max_retries=N)
      │  LLM 產碼 → pit.py 樣板注入 → sandbox.py 執行
      ▼  RepairResult(success, final_code, sandbox_output: pd.Series, trial_steps)
1A   evaluate(factor_series, price_volume_df, trial_count, dead_and_live_matrix)
      │  1H signal 先 lag；Net-return IC/非重疊/regime/DSR/abs-Spearman
      ▼  GatekeeperResult(passed, metrics: dict, rejection_reason)
1E   build_card(GatekeeperResult, final_code, Hypothesis) → EvidenceCard
      │  passed → candidate_store.write_candidate + write_evidence_card
      │  failed → research_ledger 記墓地 + 死因
      ▼
1E   promote (人工 CLI，唯一碰 production features 的路徑)
```

- **1A 讀** production + 墓地因子序列矩陣算相關；**讀** `research_ledger.jsonl` 撈全域 trial count。
- **1C 下接** Phase 0 `sandbox_ast`/`sandbox`/`pit`。
- **1D 寫** `runs/foundry_jobs/<id>/job.json`（新 foundry runner reconcile）；**不 inline 跑**。
- **1E 寫** `manifests/foundry_evidence/<sym>.json`（複用 `factor_io.dump_evidence` 慣例）+ candidate parquet。

---

## 埋雷清單（agy 揭，各子計畫 TDD 必覆蓋）

| # | 雷 | 歸屬 | 解法 |
|---|----|------|------|
| P1 | **Net-return IC turnover 謬誤** — turnover ≠ `abs(factor.diff())`；截面標準化前後誤差巨大 | 1A | 先映射因子→target weights，再 `abs(weights.diff())×cost_bps`。TDD 用已知 turnover 的假因子驗數值 |
| P2 | **DSR trial count 逃逸** — 只算當晚 → nightly 跑數月=數萬次 snooping 卻以為 50 次 | 1A + 1D | 從 `research_ledger.jsonl` `groupby(asset_family)` 撈**歷史全域累計**傳給 DSR |
| P3 | **abs(Spearman) 墓地效能爆** — 數千死因子逐一算會拖垮 nightly | 1A | 矩陣批次運算（非 for-loop）；墓地序列降維索引或只留近期/高波動核心 |
| P4 | **AST 擋不住 OOM/無窮迴圈** — LLM 寫合法語法卻笛卡爾積 OOM 或 `while True` 掛住容器 | 1C + Phase 0 | Docker 硬 `timeout` + `--memory`；1C 捕 TimeoutError/非零退出 = 修復失敗 |
| P5 | **Nightly 破產迴圈** — LLM 在「差一點」因子微調燒光 token | 1C + 1D | 嚴格 `max_retries=3`，失敗直丟墓地不無限追問；1D budget token tracker + early stopping |

> **P4 附註（Phase 0 已知限制）**：`DockerSandbox` docstring 記載 `subprocess.run(timeout=)` 只殺 CLI client 不殺容器（moby 不從 CLI 傳 SIGKILL 到 daemon）。1C 實作前須先補「容器生命週期安全的 timeout」（`docker run` 背景 + `docker stop --time` + 逾時 `docker kill`），否則 P4 防線漏。列為 1C 前置。

---

## 刻意不在 Phase 1（留 Phase 2+）

選幣自動化、pipeline 編排自動化、優化自動化、paper 晉級推薦、實盤推薦、stage3 診斷 LLM 查詢工具、alpha decay 監控、hermes-agent 虛擬研究員實驗。

---

## 交付順序（每子計畫之間停等核准）

1. **本概覽**（核准後）
2. **1E** → `docs/talos/plans/2026-07-08-talos-phase1e-evidence-card.md`（詳 TDD，**停**）
3. 1A → 統計守門詳 TDD（**停**）
4. 1B → 佇列詳 TDD（**停**）
5. 1C → LLM 引擎詳 TDD（含 P4 容器 timeout 前置）（**停**）
6. 1D → 編排詳 TDD（**停**）
