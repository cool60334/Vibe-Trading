## 1. Schema update

- [x] 1.1 在 `dashboard/server/schemas.py::DiagnosisBlock` 加 `walk_forward_source: Optional[str] = None`
- [x] 1.2 加 `walk_forward_metrics: Optional[dict] = None`
- [x] 1.3 跑既有 schema 單測（若有）驗 archived diagnosis.json 仍能 validate

## 2. Walk-forward metrics loader

- [x] 2.1 在 `research/pipeline/stage3_diagnose.py` 加 `read_walk_forward_metrics(strategy_runs_entry, runs_root) -> dict | None`
- [x] 2.2 函式必須 pure（無 stdout side effects 除 warning）、tolerant of missing keys / missing file / unparseable csv
- [x] 2.3 取 `walk_forward_runs[0]`（list 第一筆；空 list → return None）
- [x] 2.4 串到既有 `read_metrics_csv` 讀 csv

## 3. Walk-forward loader tests

- [x] 3.1 在 `research/tests/test_stage3_diagnose.py` 新增 `TestReadWalkForwardMetrics` class
- [x] 3.2 scenario：walk_forward_runs 非空且檔存在 → 返回 dict（驗 sharpe/dd/trade_count keys）
- [x] 3.3 scenario：walk_forward_runs 空陣列 → 返回 None、無 raise
- [x] 3.4 scenario：walk_forward_runs 缺 key → 返回 None、無 raise
- [x] 3.5 scenario：referenced run dir 不存在 → 返回 None、無 raise
- [x] 3.6 scenario：referenced metrics.csv 不存在 → 返回 None、無 raise

## 4. Prompt builder updates

- [x] 4.1 改 `build_diagnosis_prompt` 簽名加 optional kwarg `walk_forward_metrics: dict | None = None`
- [x] 4.2 當 walk_forward_metrics 非 None：prompt 含 "Walk-Forward" + "OOS" + "authoritative" 字串、含 walk_forward JSON 區塊
- [x] 4.3 當 walk_forward_metrics None：prompt 結構與改前一致（不含 OOS section）
- [x] 4.4 Task 指示文字明示：以 OOS 為主、train 僅用於 overfit detection (gap)

## 5. Prompt builder tests

- [x] 5.1 加 `TestBuildPromptWithWalkForward` class
- [x] 5.2 scenario：non-None walk_forward → prompt contains "Walk-Forward" + "authoritative"
- [x] 5.3 scenario：None walk_forward → prompt 不含 "Walk-Forward" 與 "authoritative"
- [x] 5.4 scenario：walk_forward dict 的 sharpe / dd / trade_count 值出現在 prompt JSON 區塊

## 6. Rule-based fallback updates

- [x] 6.1 改 `rule_based_action` 簽名加 optional kwarg `walk_forward_metrics: dict | None = None`
- [x] 6.2 walk_forward 路徑：sharpe < 0 → back_to_stage_2；sharpe<1 OR |dd|>0.15 OR trades<30 → back_to_stage_4；else proceed
- [x] 6.3 OOS trade gate 設 30（不是 train 的 50）
- [x] 6.4 None 路徑保留現有 train-anchored 邏輯不變

## 7. Rule-based fallback tests

- [x] 7.1 加 `TestRuleBasedActionOOS` class
- [x] 7.2 scenario：wf sharpe 1.02 / dd -0.091 / trades 49 → PROCEED
- [x] 7.3 scenario：wf sharpe 0.25 / dd -0.26 / trades 89 → BACK_TO_STAGE_4
- [x] 7.4 scenario：wf sharpe -4.16 → BACK_TO_STAGE_2
- [x] 7.5 scenario：wf None → 走 train path（驗 backwards compat 同既有測試）
- [x] 7.6 scenario：wf sharpe 1.1 / trades 35 → PROCEED（驗 OOS trade gate ≥ 30）

## 8. main() wiring

- [x] 8.1 在 main loop 每個 strategy 處：呼叫 `read_walk_forward_metrics` 拿 wf metrics（or None）
- [x] 8.2 把 wf metrics 傳給 `build_diagnosis_prompt`
- [x] 8.3 把 wf metrics 傳給 `rule_based_action`（LLM 失敗 fallback path）
- [x] 8.4 在 `build_diagnosis_block` 寫出 `walk_forward_source` + `walk_forward_metrics` 兩個新欄位
- [x] 8.5 stdout 加印「[stage3d] {sid}: walk_forward sharpe=X dd=Y trades=Z」當 wf 存在

## 9. End-to-end smoke

- [x] 9.1 跑 `python -m research.pipeline.stage3_diagnose` 對現有 strategies
- [x] 9.2 驗 eth_s5_half_size diagnosis.json 之 `recommended_action == "proceed"`（OOS sharpe 1.02 > 1.0 + DD 9.1% < 15% + trades 49 > 30）
- [x] 9.3 驗 btc_s9 / eth_s3 / sol_s2 etc 既有 verdict 不退化（OOS metrics 與 train 同樣判定相符的情況不該翻轉）
- [x] 9.4 跑 `python -m research.pipeline.stage5_select`，驗 eth_s5_half_size.selected == True
- [x] 9.5 confirm stage 5 ranking：eth_s5 應為 ETH 系列冠軍、score 0.66+ 維持

## 10. Docs

- [x] 10.1 更新 `research/PIPELINE.md` 之 Stage 3-diag 章節：說明新讀 walk-forward、prompt 結構、OOS-anchored fallback
- [x] 10.2 在 PIPELINE.md「Walk-Forward train/OOS」段加註：stage3-diag 現在也接 walk_forward
- [x] 10.3 加 markdown 表「diag 決策入口」對比舊（只看 train）vs 新（OOS-first）

## 11. Memory / 收尾

- [x] 11.1 在 memory 新增 `project_stage3_diag_oos_aware_adr.md` 記錄此 change 動機與結果
- [x] 11.2 更新 `MEMORY.md` 索引
- [x] 11.3 跑 `openspec validate stage3-diag-oos-aware` 驗合法
