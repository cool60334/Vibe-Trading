## 1. OOS source fallback

- [x] 1.1 在 `research/emit_manifest.py::build_backtest_block` 改 OOS 讀取：`oos_runs` 非空用 `oos_runs[0]`，否則 fallback `walk_forward_runs[0]`，皆空則 `oos=None`
- [x] 1.2 確認 `BacktestMetrics.source_run` 正確帶出實際 OOS run 名

## 2. OOS source tests

- [x] 2.1 在 `research/tests/test_emit_manifest.py` 加 `TestOosSourceFallback` class
- [x] 2.2 scenario：oos_runs 空 + walk_forward_runs 有 → oos 非 None、source_run == wf run
- [x] 2.3 scenario：oos_runs 有值 → 優先用 oos_runs（precedence）
- [x] 2.4 scenario：兩者皆空 → oos == None

## 3. OOS-aware gate evaluation

- [x] 3.1 在 `research/emit_manifest.py` 加模組常數 `GATE_OOS_MIN_TRADES = 30`（附 stage3-diag 交叉註解）
- [x] 3.2 `compute_gate` 加 `eval_metrics = oos_m if oos_m is not None else is_m` + `oos_mode` flag
- [x] 3.3 `min_sharpe`：OOS 模式用 `GATE_MIN_WALK_FORWARD_SHARPE`(1.0)、legacy 用 `GATE_MIN_SHARPE`(1.5)；actual 取 eval_metrics
- [x] 3.4 `max_drawdown`：actual 取 eval_metrics、threshold 不變(0.10)
- [x] 3.5 `min_trades`：OOS 模式 threshold=30、legacy=100；actual 取 eval_metrics
- [x] 3.6 `min_profit_factor`：actual 取 eval_metrics、threshold 不變(1.5)
- [x] 3.7 emitted `GateThreshold.threshold` 反映實際採用的門檻值（dashboard 顯示正確 bar）
- [x] 3.8 legacy 路徑（oos None）行為與改前 byte-for-byte 一致

## 4. alpha_not_fee_illusion 降級

- [x] 4.1 `compute_gate`：`has_stress = worst_stress_sharpe is not None`
- [x] 4.2 `alpha_not_fee_illusion` 的 `fatal = has_stress`（無資料時 non-fatal）
- [x] 4.3 `passed = has_stress and worst_stress_sharpe > 0`
- [x] 4.4 確認 `fatal_fail = any(t.fatal and not t.passed)` 在無壓測時不被此 check 設 true

## 5. Gate tests

- [x] 5.1 在 `research/tests/test_emit_manifest.py` 加 `TestOosAwareGate` class
- [x] 5.2 scenario：OOS sharpe 1.016 → min_sharpe actual=1.016 threshold=1.0 passed
- [x] 5.3 scenario：OOS trades 49 → min_trades threshold=30 passed
- [x] 5.4 scenario：OOS dd 0.091 → max_drawdown passed（≤0.10）
- [x] 5.5 scenario：legacy（oos None）sharpe 1.6 trades 120 → threshold 1.5/100 不變
- [x] 5.6 scenario：no stress → alpha_not_fee_illusion fatal=false、fatal_fail 不被設
- [x] 5.7 scenario：stress worst -0.5 → alpha fatal=true、fatal_fail=true
- [x] 5.8 scenario：stress worst 0.4 → alpha passed=true
- [x] 5.9 scenario：eth_s5-shaped（OOS 1.016/0.091/49/1.539、no stress）→ fatal_fail=false
- [x] 5.10 跑 pytest 全綠、既有 test 不退化

## 6. End-to-end smoke

- [x] 6.1 跑 `python -m research.emit_manifest`（或 `python -m research.pipeline.stage5_select`）重生 manifest
- [x] 6.2 驗 `eth_s5_half_size` manifest `gate.fatal_fail == false`
- [x] 6.3 驗 `btc_s9_stablecoin_funding_gated_tuned` 與 `eth_s3_dynamic_exit` 也 `fatal_fail == false`
- [x] 6.4 驗有真負 OOS 的策略（若有，如 sol_s2 / eth_s1）仍正確 fatal_fail==true

## 7. Docs + 收尾

- [x] 7.1 更新 `research/PIPELINE.md` gate / Stage 5 段：說明 gate 改 OOS-aware、walk_forward fallback、alpha gate 無資料時 non-fatal
- [x] 7.2 在 memory 新增 `project_emit_manifest_oos_aware_gate_adr.md`
- [x] 7.3 更新 `MEMORY.md` 索引
- [x] 7.4 跑 `openspec validate emit-manifest-oos-aware-gate` 驗合法
