# PROJECT_STATE.md — 專案狀態總覽

> 最後更新：2026-07-13（branch: `quant-trading-dashboard`）
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

## 目前狀態（2026-07-13）

**Talos Foundry 整弧完成 + 首次真跑 against 真 manifests**
- OHLCV refresh 已實作+push（`research/pipeline/refresh_ohlcv.py`、`scripts/refresh_foundry_ohlcv.sh`）；btc/eth/sol 全 span 2022–2026 `ohlcv_<sym>.parquet` 本機產好（gitignored）
- 全生產路徑真付費跑通（真 OpenAI gpt-4o-mini→真 Docker 沙盒→gatekeeper→graveyard）
- **2026-07-13 首次對真 eth manifests 真跑，揪修 2 個 regime gate 真 bug**（TDD）：
  - `_load_daily_regime` 回 tz-naive index → regime_ic 對 tz-aware factor 崩（`utc=True` 修）
  - `regime_ic` 用 factor 短 index 的 mask 去切 ohlcv 寬 index 的 fwd_ret → `Unalignable`（`fwd_ret.reindex(factor.index)` 修）
  - 觸發條件＝某幣有 `regime_<sym>.json` + ohlcv 比 features 寬（OHLCV refresh 拉寬 span 造出）；舊測試 naive fixtures + 等長 factor/fwd + monkeypatch 全躲過
  - 真跑 done：4 hypotheses 全 rejected、budget cap 乾淨停、OOS 鎖完好；research suite 1619 pass

**掛著待決：**
- eth_s5 / sol_s1 停用（regime 泄漏），**無現役可信策略運行中**（`runs/testnet/` 僅 `eth_s5_live_smoke`）
- Paper trader lookback 壞死（live 200 根 < 需 1080 → 訊號恆 0，從未成交）；F1/F3 修復**待使用者核准**
- Foundry backlog（非阻塞）：①其餘 6 幣（bnb/xrp/doge/ada/ltc/bch）缺 features 要先跑 stage0a；②runner 該 `resolve()` mount 路徑（relative `--manifests-dir` 在 Windows docker mount 會被當 volume name）；③calls≠USD 成本換算；④server cron 需 root（fable_ro 唯讀進不去）；⑤`agent/.env:14` 忘記的 OpenAI key 待自查/rotate
- 未 commit：~19 個 plans/specs 文件 + `eth_s5_half_size/real_funding_recompute.json`
- Backlog：HTF-gate（pipeline hardening ⑤）、stage2 archetype factory

## 關鍵設定（research_config.yaml）

- interval `1H`、`oos_start: 2025-01-01`、`window_end: 2026-04-01`（凍結）
- fees：taker 0.055% + slippage 0.05%，A1 realistic 成本為預設
- 三個 pytest scope 分開跑：`pytest agent/tests/`、`python -m pytest research/tests/`（皆 repo root）、`cd dashboard/server && pytest`
