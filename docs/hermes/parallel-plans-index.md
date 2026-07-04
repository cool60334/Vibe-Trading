# 平行實作計畫索引（Part A 核准項）

> 產出 2026-07-04。7 份計畫**檔案足跡互斥**，可各開一個新 session（Sonnet）獨立實作、任意順序 merge。
> 每份計畫自帶完整 context，執行 session 建議開場："請用 superpowers:executing-plans 執行 docs/hermes/plans/<檔名>"。

## 計畫清單與檔案所有權

| 計畫 | Part A 項 | 檔案所有權（只准動這些） | 風險 |
|---|---|---|---|
| [2026-07-04-golden-run-contract-test.md](plans/2026-07-04-golden-run-contract-test.md) | C2 | `research/tests/test_golden_run.py`、`research/tests/fixtures/golden_run/`（皆新增） | 極低 |
| [2026-07-04-freshness-skip-stopped.md](plans/2026-07-04-freshness-skip-stopped.md) | B2 | `dashboard/server/freshness_scheduler.py`、`dashboard/server/test_freshness_scheduler.py` | 低 |
| [2026-07-04-atomic-write-chmod.md](plans/2026-07-04-atomic-write-chmod.md) | C3 | `research/lib/factor_io.py`、`research/tests/test_factor_io_chmod.py`（新） | 極低 |
| [2026-07-04-trader-expectancy-monitor.md](plans/2026-07-04-trader-expectancy-monitor.md) | D2-監控 | `dashboard/trader/monitor.py`（新）、`dashboard/trader/loop.py`、`dashboard/trader/test_monitor.py`（新）、`dashboard/trader/test_loop_status.py` | 低 |
| [2026-07-04-ops-health-and-pipeline-summary.md](plans/2026-07-04-ops-health-and-pipeline-summary.md) | D1+D3 | `research/pipeline/lib/pipeline_summary.py`（新）、`research/pipeline/stage5_select.py`、`research/tests/test_pipeline_summary.py`（新）、`dashboard/server/ops_health.py`（新）、`dashboard/server/main.py`、`dashboard/server/test_ops_health.py`（新） | 低 |
| [2026-07-04-runs-retention.md](plans/2026-07-04-runs-retention.md) | B3 | `dashboard/server/retention.py`（新）、`dashboard/server/test_retention.py`（新） | 中（刪檔工具 → 預設 dry-run 硬規定） |
| [2026-07-04-compiler-interval-awareness.md](plans/2026-07-04-compiler-interval-awareness.md) | A4 | `research/lib/signal_compiler.py`、`research/pipeline/stage2b_compile_signal.py`、`research/pipeline/stage3_backtest.py`（僅 lag-stress 編譯呼叫一行）、`research/pipeline/stage4_optimize.py`、`research/pipeline/stage4_cpcv.py`、`research/tests/test_signal_compiler_interval.py`（新） | 中（須驗 1H 輸出不變） |

## 互斥驗證

- `research/tests/`：三個計畫各建**不同**新測試檔，零交集。
- `research/pipeline/`：D1+D3 動 stage5；A4 動 stage2b/3/4/4cpcv —— 無同檔。
- `dashboard/server/`：B2 動 freshness_scheduler；D1+D3 動 main.py+新檔；B3 只建新檔 —— 無同檔。
- `dashboard/trader/`：只有 D2-監控 動。

## 刻意排除（勿在平行批次做）

| 項 | 原因 |
|---|---|
| A1 成本模型 + F2 | 會改全部歷史回測數字，需使用者確認 + C2 golden-run 先落地當安全網；動 `agent/`（上游）需格外小心 |
| A2 + A3（ledger / 窗口凍結） | 兩者都動 `stage4_optimize.py` → 互相衝突；等平行批次 merge 後單獨一個 session 一起做 |
| B1 增量快取 | 動實盤在讀的 feature store，風險中，需人盯，不適合丟給無監督 session |
| C1 stage3 拆檔 | 大範圍搬家，與一切動 stage3 的變更衝突，排最後 |
| D5 promote API | 動 `main.py` 與 D1 衝突 + 含前端；D1 merge 後再做 |

## 執行建議（給使用者）

1. 每個計畫開一個**獨立 session**；同時跑多個時，建議各自用 git worktree（session 裡說「用 worktree」即可，superpowers 有對應 skill），避免同一工作樹互踩。
2. 三個 pytest scope 不可混跑（各計畫內已寫明自己的 scope 與指令）。
3. Merge 順序任意；**每 merge 一份後**在主工作樹跑該計畫的 scope 全套測試再 merge 下一份。
4. 全部 merge 完，跑三 scope 全套：`python -m pytest research/tests/ -q`（repo 根）、`cd dashboard && python -m pytest trader -q`、`cd dashboard/server && python -m pytest -q`。
