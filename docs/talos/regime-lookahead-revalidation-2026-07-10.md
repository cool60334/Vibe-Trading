# Regime Look-Ahead 重驗：eth_s5 / sol_s1 的 promote 建立在洩漏上

> 日期：2026-07-10 · 分支 `quant-trading-dashboard`
> 觸發：Talos Phase 1A 實作期間發現 `regime` 日級 label 的 ~1 天 look-ahead（`3e6e8a8`），該洩漏同時存在於 **live `signal_engine.py.j2` 模板與三個手刻策略**（`027344e` 修復）。agy 建議：在用「可能有破洞的量測工具」生產新因子前，先回頭重驗已知策略。
> 結論：**eth_s5 與 sol_s1 的 OOS 通過 promote gate，是因為量測工具漏水。修好後兩者都不過閘。維持停用。**

---

## 1. 洩漏機制

`research/lib/regime.py` 的 `daily_close_from_hourly()` 用 `resample("1D").last()`。在預設 `label="left"` 下，**D 日的收盤價被存在 `D 00:00` 這個時間戳**。`compute_regime()` 從該收盤推導 `regime.loc[D]`——所以 D 日的 label 實際上要到 **D 23:00 / D+1 00:00** 才知道。

任何消費端做 `regime_df["regime"].reindex(hourly_index, method="ffill")`，就會把 D 日的 label 指派給 D 日**當天前 23 小時**的 bar —— 一個系統性的 **~1 天 look-ahead**，且**在 regime 轉換日咬得最兇**。

修復：`research/lib/regime.py::ffill_regime_to()` —— 先把 daily index 前移 1 天再 ffill，label 只從真正可知的那刻起生效。這是消費 `compute_regime` 輸出的**唯一安全方式**。

**受影響且直接 gate 真實（paper）進場的**：`signal_engine.py.j2` 模板 + `eth_s4_regime_filtered` / `eth_s5_half_size`（當時部署中的 paper trader）/ `sol_s1_single_factor_regime`。

---

## 2. 實驗設計（變因隔離）

過去教訓：一次 fresh 重跑混了「OOS 窗延長 + factor parquet 重生 + 成本模型變更」三個變因，導致無法歸因。本次嚴格隔離。

- **窗**：`2025-01-01 → 2026-03-31`。**刻意 cap 在 `final_holdout_start = 2026-04-01` 之前**（A2/A3 憲法：終極 holdout 任何 pipeline 窗都不得碰）。
- **資料**：現有 `factor_values_*.parquet` / `features_*.parquet` / `regime_*.json`，**不重生**。
- **唯一變因**：`_load_regime_series()` 內的那一行 ffill。
  - leaky：`daily.reindex(target_index.normalize(), method="ffill")`
  - fixed：`ffill_regime_to(daily, target_index.normalize())`
- **成本**：A/B 兩組同用引擎預設（legacy）以純隔離 regime；再單獨跑一組 `fixed + realistic`（A1 研究側 default：config fees + `taker_both_legs` + 真 funding 序列）取誠實數字。
- 執行：`agent/backtest/runner.py::main(run_dir)`，本地（伺服器唯讀鐵律）。

**sol_s1 注意**：其 OOS run 目錄裡的 code 快照是**較舊的自動編譯版本**，與現行凍結引擎差異遠不只 regime（變數結構 / `FACTOR_LAG_HOURS` / `SIZE_MULT`）。若拿它當 leaky 組會混淆變因。故 **leaky 組改由現行 fixed 碼「只回退那一行」構造**。

### 反假象驗證（宣稱前必做）

`_load_regime_series()` 對「檔缺 / JSON 壞 / breakdown 空」有**靜默 fallback 到全 `neutral`**（等於關掉 regime 遮罩）。若 fixed 組其實走了 fallback，delta 就不是 look-ahead 而是「沒有遮罩」。逐項排除：

| 檢查 | ETH | SOL |
|---|---|---|
| fixed 組是否全 `neutral` fallback | 否（bull/bear/neutral 皆在） | 否 |
| leaky vs fixed 的 label 差異比例 | **384 / 10897 = 3.52%** | **528 / 10897 = 4.85%** |
| trades / equity 是否真的不同 | 是 | 是 |

差異比例正好對應「label 後移 1 天」在轉換日附近造成的 bar 數，符合機制預期。

---

## 3. 結果

### eth_s5_half_size

| 組 | sharpe | max DD | trades | win rate | profit factor |
|---|---|---|---|---|---|
| leaky + legacy | **1.084** | −9.13% | 41 | 53.7% | 1.544 |
| fixed + legacy | 0.899 | −9.13% | 41 | 51.2% | 1.437 |
| **fixed + realistic** | **0.827** | −9.13% | 41 | 51.2% | 1.438 |

- look-ahead 的純貢獻：**+0.185 sharpe**
- 成本模型的純貢獻：**−0.072 sharpe**（與先前「純成本約 −0.09」的診斷一致）
- **誠實 OOS sharpe = 0.827，低於 promote gate 1.0。**

### sol_s1_single_factor_regime

| 組 | sharpe | max DD | trades | win rate | profit factor |
|---|---|---|---|---|---|
| leaky + legacy | **1.709** | **−16.6%** | 72 | 54.2% | 1.776 |
| fixed + legacy | 0.675 | −38.1% | 72 | 47.2% | 1.223 |
| **fixed + realistic** | **0.542** | **−40.0%** | 72 | 47.2% | 1.213 |

- look-ahead 的純貢獻：**+1.03 sharpe**
- 且 look-ahead **把真實回撤藏了一半**：−16.6%（假）vs −40.0%（真）
- **誠實 OOS sharpe = 0.542，DD −40%。**

---

## 4. 為什麼 3~5% 的 bar 造成這麼大的差異

被改動的 bar **不是隨機分布**，而是集中在 **regime 轉換日**。策略的 regime gate 正是靠轉折點決定「能不能做多 / 能不能做空」。提前 1 天知道轉折，等於在賺賠分野上作弊。

`sol_s1` 受創遠大於 `eth_s5`，一個合理解釋是它是**單因子 + regime overlay**（`ls_divergence` contrarian），alpha 高度依賴 regime gate 過濾；`eth_s5` 是多因子 consensus，regime 只是其中一層。此點未進一步驗證，列為觀察。

---

## 5. 風險意涵（真錢側）

`sol_s1` 最危險：leaky 回測顯示 DD −16.6%，看似安全。真實 −40.0%。killswitch 的 `KILL_TERMINATE_DD=0.07` 是**相對於預期 DD**設定的 —— 用一個被低估一半的預期 DD 去設終止線，實盤會一路虧到遠超預期才觸發終止。

**兩個 paper trader 目前皆 `desired_state=stopped`。本重驗確認：維持停用是正確的，且在因子重建之前不得復活。**

---

## 6. 已知限制 / 未做

- 只重驗了 OOS 窗，未重跑 train 窗（train sharpe 是否也被灌水未測）。
- 未檢查 `eth_s4_regime_filtered`（同樣受影響，但未部署）。
- 未檢查其他 ffill 消費點：`signal_engine` 內對 `funding_z` / `stablecoin_supply_z` / `ls_divergence` 也做 `reindex(..., method="ffill")`。這些是**因子序列**不是 regime label，是否有同類 bin-label 洩漏**尚未稽核**——建議列為後續。
- 兩個存檔 OOS run 的窗（`2026-06-02` / `2026-06-18`）都超過 `final_holdout_start`，早於 A2 憲法落地。本次重驗已 cap。

---

## 7. 教訓

1. **「OOS 衰退是真 alpha 衰退」的診斷，必須先證明量測工具沒漏水。** 先前判 eth_s5 的 OOS 崩壞是策略衰退（非成本），該診斷是用漏水的 regime 做的。
2. **resample + label 語意是 look-ahead 的經典藏身處。** `.last()` 取 bin 末值卻掛 bin 首戳，任何 ffill 消費端都會偷看。
3. **少量 bar 的洩漏不等於少量影響。** 3.5% 的 bar 就足以把 sharpe 從閘下推到閘上；4.85% 足以隱藏一半的回撤。
4. **A/B 必須從同一份現行碼構造**（只回退目標那行），不可拿歷史快照當對照組——快照挾帶其他世代差異。
