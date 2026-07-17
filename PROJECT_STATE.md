# PROJECT_STATE.md — 專案狀態總覽

> 最後更新：2026-07-17（branch: `quant-trading-dashboard`，HEAD `d51a47f`，**24 commits 未 push**）
> 維護規則：每次重大改動或新功能完成後更新本檔。AI 助手應主動更新。
> 本檔是「濃縮快照」；細節見 `docs/talos/`、`docs/superpowers/`、memory。

## 專案定位

一個 repo、兩套獨立系統：

| 系統 | 目錄 | branch | 性質 |
|---|---|---|---|
| Vibe-Trading agent | `agent/`, `frontend/` | `main` | 上游開源 LLM 交易研究代理人（ReAct、77 skills、452 alphas、MCP） |
| Quant 研究管線 | `research/`, `dashboard/` | `quant-trading-dashboard` | 私有 9 階段永續合約 alpha 發掘系統（BTC/ETH/SOL…），不推上游 |

核心目標：**系統化找出「真的、可部署」的 crypto perp 策略**——榨掉一切回測作弊（look-ahead、成本低估、overfit），寧可負結果也不要假 alpha。

## 已完成基建

- 9 階段 pipeline：`0a→0→1→2→2b→2.5→3→3-diag→4→3-diag→5`（特徵→發掘→驗證→組裝→編譯→regime→回測→診斷→優化→選拔）
- 資料層：OKX（K線/funding）、Binance archive（OI/多空比）、DefiLlama（穩定幣）、CoinGecko
- Dashboard（FastAPI+React）+ paper trader（虛擬成交+killswitch），雙 Docker stack 解耦（重佈 dashboard 不殺 trader）
- Pipeline UI：狀態頁、job runner、單幣單 stage 執行、壓測 UI
- **Talos**（原 Hermes，`research/hermes/`）：自主因子鍛造引擎，Phase 0～1E 全完成 + Foundry 真容器跑通 + LLM ideation + 因子轉正（induction）

## 誠實回測主線（關鍵突破時序）

1. IC 灌水修復（非平穩因子+ffill 假象）→ `apply_ic_eval_transform`
2. 回測納入 funding fee（永續回測沒 funding 全是幻覺）
3. ETH 4yr 真測全負 → 揭穿 bull-window overfit
4. A1 成本模型 v2 成研究側預設（真 taker 費+真 funding 序列）
5. **Regime look-ahead 修復（2026-07）**：標籤泄漏 ~1 天；重驗證實 eth_s5（1.16→0.827）、sol_s1（0.542, DD −40%）過閘全靠泄漏 → **兩策略停用**
6. Foundry OOS 鎖未接線修復（37% panel 曾含污染）
7. Intrabar stop 審計（stage3 `--intrabar-audit`）、entry-lag 審計、策略級 lag-stress
8. 窗口凍結 `window_end: 2026-04-01` + 終極 holdout（A2+A3）
9. **⚠️ 2026-07-17 反向發現**：以上全是「證偽能力」。positive control 首次量測「偵測能力」→ **最低偵測門檻 ≈ 年化 Sharpe 5**（見下）。證偽強不代表找得到；先前「零結果 → 池子沒魚」的推論是邏輯跳躍，**待校準修完才能重新解讀所有負面結果**。

## 知識資產

- **有效因子**：`funding_z`、`basis_rel`、`stablecoin_supply_z`、多空比系列（`global_ls_acct_z`/`toptrader_ls_z`/`ls_divergence`）、`depeg_z`（薄）
- **封盤死路**：盤中 <1H 因子全類（頻率錯配，1H 是地板）、Binance order-flow、ETH 舊因子集、`fiat_prem_z`
- **教訓**：IC>0.1 先懷疑資料；intraday 必跑 entry-lag 審計；護欄要有「真的被呼叫」的整合測試

## Talos 進度

**Phase 0～1 全數完成 merged**：1A gatekeeper → 1B hypothesis queue → 1C forge（LLM 寫碼+有界修復迴圈）→ 1D orchestrator → 1E evidence card + promote gate（`b06e410`/`cf3839f`/`e80073e`，模組 `evidence_card.py`/`evidence_store.py`）。

自主發掘鏈四個斷点全接完：

| 斷点 | 內容 | 狀態 |
|---|---|---|
| **B** — Foundry→pipeline 橋 | 嚴選因子重算全 span→per-run overlay→`load_factor_values(include_foundry)`→stage2/3/5 | ✅ `bc907d1` 起 |
| **A** — 自動排程 | Foundry 離開 critical path 當背景挖礦（`foundry_miner_scheduler`）+ pipeline 自排跑「庫內 ∪ overlay」（`discovery_pipeline_scheduler`），兩獨立 cadence | ✅ `43a9d86` 起（`f7bafea` 修 foundry step 沒真挖礦） |
| **畢業機制** — 因子轉正 induction | Foundry LLM 碼 → production 因子庫（`induct.py`），人工閘 + AST 重審 + PIT 重檢 + 決定性檢查 + bridge 對帳 + golden test；`stage0a` 每日重算 → trader 可用 | ✅ `b5d93f1`→`551e574`（07-16） |
| **LLM ideation** | panel field schema → `ideator.py` 生 crypto-native 假說 → 接進 `run_foundry`；死因分桶回饋、dedup 閘感知 | ✅ `8ebebcd`→`fd00758`（07-16/17） |

周邊：花費帳本 + 單實例鎖 + 跨 run 每日 LLM 上限；LLM client 泛化（OpenRouter/OpenAI seam）。

**Foundry 已真跑**（非空跑）：`research/manifests/research_ledger.jsonl` 51 筆、`foundry_evidence_eth.json` 落地、`research/runs/foundry{,_jobs}/` 有產出。真跑撞出並修掉 5 個 bug：sandbox mount 未解絕對路徑（`cbf2535`）、`factor_to_weights` 未轉 float64（`7a95243`）、job 失敗沒記 traceback（`29b912a`）、單一假說失敗炸掉整 run（`83b5cd1`）、沒告訴寫碼模型 df 真有哪些欄（`8a1e951`）。

## 目前狀態（2026-07-17）

**✅ Foundry gate calibration 修完，已 merge 進 `quant-trading-dashboard`（`c6f9951`）。網撈得到魚了。**

8-task plan（`docs/superpowers/plans/2026-07-17-foundry-gate-calibration.md`）用 subagent-driven-development 執行完，每 task 獨立 spec+quality review，終審 opus clean（0 Critical/Important）。三個統計錯誤都修了：`deflated_sharpe` 拆開 N（多重測試債）與變異數樣本；DSR 改吃 **gross** SR（虛無假設下期望 0），成本改由獨立 `net_ir>0` 閘管；`T` 改傳 `n_samples`（實際入樣本數）取代 `bars_per_year`。**門檻數值全未動**（`gross_ic_min=0.03`/`dsr_min=0.5`/`redundant_abs_spearman=0.7`/`max_turnover=0.5`）。新增 `research/hermes/calibration.py`（`plant_alpha`/`circular_shift`/`PRIME_SHIFT_DAYS`）當常設正/負控制迴歸測試。

**真跑 eth 真資料量到的數字**（非估計）：
- **最低偵測年化 Sharpe：5 → 2.75**（原本目標 ≤2.0 沒踩到，但這是 22k bar 樣本長度下的真實統計功效地板，不是 bug——已誠實記錄進測試斷言與註解，不硬調門檻湊）
- **負控制偽陽性率：0.64%**（1/156，真埋葬因子加 circular shift，遠低於 5% 上限）
- 過程中額外抓到 2 個 plan 本身的手滑 bug（`PRIME_SHIFT_DAYS` 誤植 101 通不過自己的 >15 清距檢查；Task 8 tripwire 測試合成變異數樣本比真實抽樣噪音緊太多）——都手算驗證公式本身無誤後才修，不是 code 端 regression。

**這件事的意涵**：pipeline 兩個月負面結果 + Foundry 零 candidate 的解讀污染，**現在可以解封**——校準已修完，之後的負面結果才是可信的「真沒魚」，不再混雜「網破」的可能。

**下一步／掛著待決：**
- 拿修好的閘重跑既有負面結果（ideation 兩輪 40 點子 0 candidate、eth/sol 既有因子）重新解讀，過濾掉舊校準問題污染
- **24+ commits 未 push**（induction + ideation + calibration 三整弧）
- ideation 現況：兩輪 40 點子 0 candidate，1H×31 欄疑似榨乾——但此結論受上述校準問題污染，須修完再判；擴資料源是備案
- 斷点 C server 部署需 root（fable_ro 唯讀進不去）
- eth_s5 / sol_s1 停用（regime 泄漏），**無現役可信策略運行中**
- Paper trader lookback 壞死（F1/F3 修復待核准）
- Foundry 舊 backlog：其餘 6 幣缺 features、runner relative-path mount 可攜性、calls≠USD、`agent/.env:14` 忘記的 key 自查
- Backlog：HTF-gate、stage2 archetype factory
- 詳見 memory [[project_talos_foundry_pipeline_bridge]]、[[project_talos_foundry_auto_scheduler]]、[[project_talos_factor_induction]]、[[project_talos_foundry_ideation]]；spec/plan 在 `docs/superpowers/{specs,plans}/2026-07-1{4,5,6,7}-*`

## 關鍵設定（research_config.yaml）

- interval `1H`、`oos_start: 2025-01-01`、`window_end: 2026-04-01`（凍結）
- fees：taker 0.055% + slippage 0.05%，A1 realistic 成本為預設
- 三個 pytest scope 分開跑：`pytest agent/tests/`、`python -m pytest research/tests/`（皆 repo root）、`cd dashboard/server && pytest`
