# SOL 永續合約 — 完整 Alpha 發掘 Pipeline 執行報告

**標的**：SOL-USDT-SWAP（OKX 永續合約）
**資料期間**：2022-06 ~ 2026-06（約 4 年，1H K 線，35,040 根）
**Walk-forward 切分**：train = 2022-06 ~ 2025-01；held-out OOS = 2025-01 ~ 2026-06
**交易成本**：maker 0.02% / taker 0.055% / 滑點 0.05%
**執行日**：2026-06-02

> 名詞速查（新手用）：
> - **IC（資訊係數）**：因子值與「未來 N 小時報酬」的相關係數。|IC|>0.05 算有料；正號=同向、負號=反向。
> - **IR（資訊比率）**：IC 的穩定度（平均/標準差），越高代表訊號越一致。
> - **train / OOS**：train 是用來挑參數的歷史段；OOS 是挑參時「完全沒看過」的後段，唯一可信的真實檢驗。
> - **sharpe**：每承受 1 單位波動賺多少，>1 不錯、<0 是賠。
> - **verdict（裁決）**：single_use=可單獨用、ensemble_only=太弱只能組合用、reject=淘汰。

---

## 一、執行摘要（結論先講）

1. **SOL 最強因子是「穩定幣供給 z-score」（stablecoin_supply_z）**：日級 IC +0.080、IR 3.44，是全池 24 個因子中唯一明確過 |IC|>0.05 門檻的。其餘因子（波動率、資金費率、基差）都很弱（|IC| 約 0.02~0.04）。OI 系列因子全廢（Bybit 只給 7 天歷史，樣本不足）。

2. **自動產生的策略（sol_s1，3 因子 OR 共識）是死路**：過度交易（3,833 筆），手續費吃光弱訊號。調參 200 組全負，最佳 train sharpe 仍 −0.53，OOS −92.7%。

3. **手動擬定的改良策略（sol_s2，2 因子 AND 閘控）大幅改善但仍不過 OOS**：把交易數從 3,833 砍到 266，train sharpe 由 −1.72 翻正到 +0.48；調參後最佳 train sharpe 衝到 **1.319**。但 held-out OOS 為 **−42.9%（sharpe −0.857）**。

4. **最終判定：SOL 在此因子集下無「穩健、可實盤」的多空 alpha。** 穩定幣趨勢策略只在 train（牛市段）有效，換到 OOS（2025-26 SOL 大跌段）就崩 —— 屬 **regime-dependent（看市況吃飯）**，與先前 ETH 4 年負結果同類。唯一價值是**防禦性**：OOS SOL 買入持有 −58%，sol_s2 只賠 −42.9%，少賠約 15 個百分點，但絕對值仍是虧損。

5. **沒有任何策略通過 Stage 5 篩選進 testnet**（兩支皆 `back_to_stage_2`），這是正確結果。

---

## 二、各階段執行結果

| 階段 | 動作 | 結果 |
|------|------|------|
| 0a 特徵/證據 | 抓 OKX K 線+資金費率、Bybit OI、DefiLlama 穩定幣；算 24 因子 + IC | OK，evidence_sol.json，top=stablecoin_supply_z |
| 0 因子探索 | LLM swarm | **失敗**（未吐 json fence，老問題）→ 改用「依 evidence IC 確定性建候選」的繞道 |
| 1 因子評估 | 5 候選算 verdict | 3 存活（皆 ensemble_only）、2 淘汰 |
| 2 / 2b / 2.5 | 合成策略 YAML → 編譯 signal_engine → 機制標記 | OK（sol_s1 自動產生） |
| 3 回測 | base（train 窗） | sol_s1 sharpe −1.72；後續手建 sol_s2 sharpe +0.48 |
| 3-diag 診斷 | LLM 路由 | 兩支皆 `back_to_stage_2` |
| 4 調參 | 200 組 grid sweep + 自動 OOS holdout | 見下 |
| 5 篩選 | 加權算分 | 0 支入選（皆被 back_to_stage_2 擋下） |

### Stage 0a — 因子 IC 排名（節錄，best |IC| 由大到小）

| 因子 | 類別 | best IC | horizon | IR | 樣本 |
|------|------|--------:|:-------:|---:|-----:|
| **stablecoin_supply_z** | stablecoin | **+0.080** | 8h | 3.44 | 1,458（日級） |
| atr_14 | volatility | −0.058 | 168h | −0.29 | 35,032 |
| rolling_std_20 | volatility | −0.053 | 168h | −0.15 | 35,013 |
| bb_width_20 | volatility | +0.036 | 72h | 0.15 | 35,013 |
| funding_rate_raw | funding | +0.032 | 168h | −0.20 | 3,587 |
| adx_14 | trend | +0.029 | 24h | 0.18 | 35,032 |
| basis_rel | basis | +0.026 | 168h | −0.47 | 35,032 |
| funding_z | funding | +0.024 | 168h | −0.07 | 4,334 |
| oi_*（4 支）| oi | — | — | — | 0~168（**廢，Bybit 7 天限制**）|

> 重點：只有 stablecoin_supply_z 真正有料。注意 SOL 的 **funding IC 是正的**（+0.032），與 BTC 的負（contrarian）相反 —— 不能照搬 BTC 的資金費率玩法。

### Stage 1 — verdict

| 因子（候選名） | feature_key | verdict |
|------|------|------|
| stablecoin_supply_zscore | stablecoin_supply_z | ensemble_only |
| bollinger_band_width | bb_width_20 | ensemble_only |
| atr_volatility | atr_14 | ensemble_only |
| funding_rate_raw | funding_rate_raw | reject |
| basis_relative | basis_rel | reject |

> 連最強的 stablecoin_supply_z 也只拿到 ensemble_only（沒到 single_use）—— 因為在「小時級」量測時 IC 被前向填值稀釋到 +0.041、且 IR 為負，未達單獨使用門檻。它的料其實在「日級」。

---

## 三、策略與回測（重點）

### 策略 A：sol_s1_multi_factor_consensus（pipeline 自動產生）

- 結構：3 因子（stablecoin + bb_width + atr），**logic = any（OR）**，小時級百分位進場。
- 問題：OR + 小時級 → 訊號太密、過度交易。
- 結果：

| | train 最佳（調參後） | held-out OOS |
|---|---|---|
| sharpe | −0.533 | **−2.963** |
| 報酬 | 負 | **−92.7%** |
| 交易數 | ~3,275 | 1,724 |

→ 200 組參數**全負**，概念失敗。

### 策略 B：sol_s2_stablecoin_trend_gated（手動擬定，仿 BTC 最佳 s9）

- 結構：只取兩個**正 IC 的趨勢因子**（stablecoin_supply_z + bb_width），改 **logic = all（AND 閘控）** —— 兩者同時極端才進場。
- 經濟邏輯：場外資金進場（高 stablecoin z）**且** 波動擴張確認（高 bb_width）才做多；反向做空。AND 把交易數壓低，讓弱訊號撐得過成本。
- 立即見效：交易數 3,833 → **266**，base sharpe −1.72 → **+0.48**。

**Stage 4 最佳參數（sweep_178）**：

| 參數 | 值 |
|------|----|
| lookback_days（百分位回看窗） | 150 |
| entry_high_pct（做多高門檻） | 70 |
| entry_low_pct（做空低門檻） | 25 |
| hold_max_hours（最長持有） | 120 |
| take_profit_pct | 9.0 |
| stop_loss_pct | 2.5 |
| leverage | 1.0 |

**績效**：

| | **train（in-sample）** | **held-out OOS（真檢驗）** |
|---|---|---|
| sharpe | **1.319** | **−0.857** |
| 總報酬 | +452% | **−42.9%** |
| max drawdown | −61% | −55.8% |
| 交易數 | 298 | 88 |
| 勝率 | 35.9% | 25% |
| profit factor | 1.44 | 0.77 |
| 同期 SOL 買入持有 | — | **−58%** |

> 解讀：train 的 sharpe 1.32 / +452% 幾乎全是「騎到 SOL 牛市」的結果。一換到 OOS（2025-26 SOL 由高點崩跌段），策略跟著賠 −42.9%。雖然**少賠**買入持有 15 個百分點（防禦性、低相關），但**絕對值仍虧**，sharpe 為負 → **不是真 alpha，是過擬合 + 看市況**。

---

## 四、為什麼 SOL 不像 BTC 成功？

對照 BTC：同樣的 stablecoin 趨勢 + 閘控結構，BTC s9 的 OOS 是 **+5.2% / sharpe 0.27**（在 BTC −24% 的市況下還賺），SOL 卻是 **−42.9%**。差異：

1. **SOL 的 beta 太大**：OOS 段 SOL −58% vs BTC −24%。SOL 是高波動山寨，穩定幣這種「慢變數」因子追不上它的暴漲暴跌。
2. **stablecoin 因子對 SOL 的 IC（日級 0.080）雖高，但 IR 在小時級為負、跨機制不穩**，本質是牛市才靈。
3. **缺乏有效的「閘」**：BTC 用 funding_z 當反向擁擠度閘控有效；SOL 的 funding 是正相關且極弱，沒有等效的好閘，只能用 bb_width 勉強代替。

---

## 五、建議下一步

1. **不要實盤這兩支策略。** sol_s2 僅具防禦性參考價值。
2. **降頻 + 純多單測試**：stablecoin 因子料在「日級」，可試「日線、只做多、sign(stablecoin_z) 為主」的低頻策略（對照 BTC s6 經驗），避開小時級過度交易與做空在山寨幣的高成本。
3. **換因子家族**：對 SOL 這種高 beta 標的，鏈上活動（DEX 量、TVL、活躍地址）、生態代幣解鎖排程，可能比通用穩定幣供給更貼題 —— 但需新資料源接入。
4. **修一個小 bug**：Stage 3 的 bear regime 切片在 oos_start 之後產生了 `start > end` 的非法窗（sol_s1_bear 因此失敗）。regime 切片本只供描述用，不影響 walk-forward 結論，但值得修。

---

## 六、產物清單

- `research/manifests/evidence_sol.json` — 24 因子 IC 排名
- `research/manifests/candidates_sol.json` — 5 候選（確定性繞道產出）
- `research/manifests/factor_sol.json` / `.md` — verdict
- `research/strategies/strategy_sol_s1_multi_factor_consensus.yaml`（自動）
- `research/strategies/strategy_sol_s2_stablecoin_trend_gated.yaml`（手建，**最佳**）
- `research/manifests/sol_s2_stablecoin_trend_gated/optimization.json` — best_params + top-5
- `runs/sol_s2_stablecoin_trend_gated_sweep_178/` — 最佳 train run
- `runs/sol_s2_stablecoin_trend_gated_oos_holdout/` — 真 OOS run
- `research/manifests/selection.json` — 0 入選

> 註：本次為隔離 SOL 而把 `research_config.yaml`、`strategy_runs.json` 暫縮成 SOL only，跑完已還原 btc/eth（備份檔 `*.bak_sol_run`）。
