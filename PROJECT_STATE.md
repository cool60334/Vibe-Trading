# PROJECT_STATE.md — 專案狀態總覽

> 最後更新：2026-07-15（branch: `quant-trading-dashboard`）
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
- **Talos**（原 Hermes，`research/hermes/`）：自主因子鍛造引擎，Phase 0～1D 完成 + Foundry 首次真容器跑通

## 誠實回測主線（關鍵突破時序）

1. IC 灌水修復（非平穩因子+ffill 假象）→ `apply_ic_eval_transform`
2. 回測納入 funding fee（永續回測沒 funding 全是幻覺）
3. ETH 4yr 真測全負 → 揭穿 bull-window overfit
4. A1 成本模型 v2 成研究側預設（真 taker 費+真 funding 序列）
5. **Regime look-ahead 修復（2026-07）**：標籤泄漏 ~1 天；重驗證實 eth_s5（1.16→0.827）、sol_s1（0.542, DD −40%）過閘全靠泄漏 → **兩策略停用**
6. Foundry OOS 鎖未接線修復（37% panel 曾含污染）
7. Intrabar stop 審計（stage3 `--intrabar-audit`）、entry-lag 審計、策略級 lag-stress
8. 窗口凍結 `window_end: 2026-04-01` + 終極 holdout（A2+A3）

## 知識資產

- **有效因子**：`funding_z`、`basis_rel`、`stablecoin_supply_z`、多空比系列（`global_ls_acct_z`/`toptrader_ls_z`/`ls_divergence`）、`depeg_z`（薄）
- **封盤死路**：盤中 <1H 因子全類（頻率錯配，1H 是地板）、Binance order-flow、ETH 舊因子集、`fiat_prem_z`
- **教訓**：IC>0.1 先懷疑資料；intraday 必跑 entry-lag 審計；護欄要有「真的被呼叫」的整合測試

## Talos 進度

- Phase 1A gatekeeper → 1B hypothesis queue → 1C forge（LLM 寫碼+有界修復迴圈）→ 1D orchestrator：**全完成，merged**
- Foundry 首真跑（真 Docker 容器全鏈）merged（88dc898）
- 花費帳本 + 單實例鎖 + 跨 run 每日 LLM 呼叫上限
- LLM client 泛化：OpenRouter + OpenAI provider seam
- 待做：Phase 1E evidence card（plan 已寫）

## 目前狀態（2026-07-15）

**Foundry 自主發掘鏈 B+A 全弧完成並 push（HEAD 01ef6bf）**

一個 session 走完「Foundry 因子→自動策略」缺的兩個斷点，各自 brainstorm→spec→agy 多輪硬審→plan→subagent 實作→驗證→push：

- **斷点 B — Foundry→pipeline 橋**（commit bc907d1 起）：Foundry 嚴選因子經 bridge 重算全 span→per-run overlay→`load_factor_values(include_foundry)`→stage2/3/5。決策：信任 foundry 閘直達 stage2、發掘自動部署人工閘、production `features_<sym>.parquet` 不碰。前置：foundry 持久化 forge 碼（原本丟掉）。修 2 個讀碼驗出的阻斷點（值只到 pre-oos 要重算、碼沒存）。
- **斷点 A — 自動排程**（commit 43a9d86 起）：**解耦**——Foundry 離開 pipeline critical path 當背景挖礦（`foundry_miner_scheduler`），pipeline 自排跑「庫內 ∪ overlay」（`discovery_pipeline_scheduler`），兩獨立 cadence。both 走現有 serial `pipeline_manager`（command-step model + `EXIT_PAUSED=201` + 非機密 config 快照）。agy 否決「用 Foundry 取代 stage0」（會弄丟 funding_z 等庫內 alpha，reject≥0.7 天生正交）→ 定調並存+畢業。
- **驗證**：research 1675 + dashboard 284 全綠；空跑兩 tick 正確 enqueue；job.json 無機密（key 由 subprocess 從 .env 讀）；production 未碰。
- 詳見 memory [[project_talos_foundry_pipeline_bridge]]、[[project_talos_foundry_auto_scheduler]]；spec/plan 在 `docs/superpowers/{specs,plans}/2026-07-1{4,5}-foundry-*`。

**下一步／掛著待決：**
- **真跑一個 discovery job 過 manager 尚未做**（需 docker+LLM key+花錢+授權；建議先本機證，勿直接上 server）
- **畢業機制**（Foundry 碼→production 因子庫）＝上線真價值的關鍵 follow-on；沒它，選出的 foundry 策略被 B 的 promote guard 擋住無法上 live
- 斷点 C server 部署需 root（fable_ro 唯讀進不去）
- eth_s5 / sol_s1 停用（regime 泄漏），**無現役可信策略運行中**
- Paper trader lookback 壞死（F1/F3 修復待核准）
- Foundry 舊 backlog：其餘 6 幣缺 features、runner relative-path mount 可攜性、calls≠USD、`agent/.env:14` 忘記的 key 自查
- Backlog：HTF-gate、stage2 archetype factory

## 關鍵設定（research_config.yaml）

- interval `1H`、`oos_start: 2025-01-01`、`window_end: 2026-04-01`（凍結）
- fees：taker 0.055% + slippage 0.05%，A1 realistic 成本為預設
- 三個 pytest scope 分開跑：`pytest agent/tests/`、`python -m pytest research/tests/`（皆 repo root）、`cd dashboard/server && pytest`
