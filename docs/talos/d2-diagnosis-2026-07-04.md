# D2 即刻診斷 — eth_s5 / sol_s1 paper trader 零交易之謎（2026-07-04）

> 性質：唯讀診斷（本地 code 精讀 + 伺服器唯讀 SSH）。**未動任何線上或本地程式**。修復方案列於文末，等核准。

## 判定：壞掉（結構性），不是正常低頻

兩個運行中的 paper trader（eth_s5_half_size 上線 ~06-07、sol_s1_single_factor_regime 上線 ~06-27）**自部署以來數學上不可能產生任何一筆交易**。回測與實盤跑同一份 signal_engine 程式碼，但餵的資料窗不同 —— 這是 backtest-live parity 缺口，不是策略沉默。

## 因果鏈（每環都已驗證）

1. **策略條件需要 90 天 rolling percentile**
   兩策略進場條件皆為 `*_percentile_90d` 型：rolling 窗 = 90×24 = **2160 根** 1H K 線，`min_periods = 1080 根`（45 天）。
   - eth_s5（手寫 engine）：[signal_engine.py:124-127](research/strategies/code/eth_s5_half_size/signal_engine.py) — `win = 90*24`、`min_periods = win//2`。
   - sol_s1（編譯 engine）：`signal_engine.py:95` 同型。
   - **伺服器上部署的 run_dir 版本已逐行比對，同一份碼。**

2. **engine 先把因子 reindex 到 ohlcv 的索引，再算 rolling**
   `_factors[key].reindex(ohlcv.index, ffill)` → 之後才 `.rolling(2160, min_periods=1080)`。所以 rolling 可用的樣本數 = **ohlcv 給幾根就只有幾根**（因子 parquet 明明有 4 年史，但被切到 ohlcv 窗口）。

3. **live trader 只餵 200 根**
   `trader/loop.py` 預設 `--lookback 200`；`trader/manager.py` 的 `build_loop_cmd()` **不傳 lookback**（已 grep 確認無此參數）；`control.json` 也沒有此欄。→ 實盤 ohlcv = 200 根 ≈ 8.3 天。

4. **200 < 1080 → percentile 全 NaN → 條件全 False → signal 恆為 0**
   NaN 與任何數比較皆 False；persist 2/3 疊加後仍 False；exit 邏輯根本輪不到。`compute_signal()` 回傳 0 → 迴圈判「訊號沒變」→ 永不下單。

5. **旁證（伺服器實測）**
   - 兩個 testnet 目錄**連 trades.csv 都不存在**（一筆 fill 都沒發生過）。
   - equity.csv 每小時心跳正常寫入（迴圈活著、broker 正常）→ 排除「進程死掉」。
   - `factor_values_*.parquet` 新鮮（<1 天）→ 排除 factor stale pause（status 也顯示 running 無 alerts）。
   - `regime_eth.json` / `regime_sol.json` 每日 02:1x 更新、覆蓋到今天 → 排除 regime 檔 stale。（目前兩幣 regime = bear：即使 percentile 正常，eth_s5/sol_s1 的多單會被 mask，僅空單可觸發 —— 這是設計行為，非故障。）

6. **為什麼回測沒事**：回測 ohlcv = 完整 train/OOS 窗（數年）→ rolling 有足夠樣本。同一份碼、不同資料窗 → 無聲分歧。（附帶發現：回測窗的**前 45 天**同樣因 min_periods 落在 NaN 段而無訊號 —— 每個 run 的頭 45 天是死區，輕微低估交易數。）

## 為什麼一個月沒人發現

- trader 狀態顯示 `running`、equity 有心跳、無 alert —— 所有現有監控都只看「活著沒」與「虧多少」，沒有「**該交易而未交易**」維度（= Part A 報告 D2-監控段提案的實證）。
- 就算用期望頻率推算：eth_s5 預期 ~35 筆/年 → 27 天 0 筆的機率 ≈ 7%，單看統計還真不夠顯著 —— 需要的是結構性檢查（「訊號值是不是 NaN/恆 0」），不是只有頻率檢查。

## 修復方案（等核准，未實作）

| 方案 | 內容 | 優點 | 代價 |
|---|---|---|---|
| **F1（建議）：live 端補足資料窗** | `trader/signal.py::_fetch_ohlcv` 加分頁抓取（Bybit 單次上限 1000 根 → 3 次分頁），lookback 預設改 ≥ 2160+persist+buffer（如 2200），`control.json`/`supervisor` 可覆寫 | **engine 一字不動** → 回測與歷史結論零重驗；風險最小 | 每 tick 多 2 次 API 呼叫（微不足道） |
| F2：engine 改在全量 parquet 上先算 percentile 再 reindex | 因子 percentile 與 ohlcv 窗脫鉤，live 只需少量 K 線 | 更根本；順帶消除回測頭 45 天死區 | **會改變回測數字**（等同 A1 級重驗）；手寫+編譯模板都要動 |
| F3（must-have，與 F1/F2 疊加）：fail-loud guard | trader 端：若 engine 輸出序列尾端全為 0 且進場條件序列尾端全 NaN → 發 `signal undefined: insufficient history` critical alert 並 pause | 讓這類結構性靜默永遠不再無聲 | 無 |

建議組合：**F1 + F3 立即做（小改動、不動研究結論）**；F2 併入 Part A 的 A1 baseline 重跑一起做（反正都要重驗）。
修好後注意：目前 regime = bear，eth_s5/sol_s1 只有空單側可能觸發，多單被 mask 是正常設計。

## 實作狀態（2026-07-04，F1+F3 已完成於本地，TDD）

| 變更 | 檔案 | 內容 |
|---|---|---|
| F1 分頁抓取 | `dashboard/trader/signal.py` | `_fetch_ohlcv` 超過單頁上限（1000）自動以 `since` 向後翻頁、去重、裁到最新 N 根；≤1000 維持舊單次呼叫（相容既有 broker/fake） |
| F1 需求推導 | `dashboard/trader/lookback.py`（新） | 從 `strategy_runs.json` → 策略 YAML 掃 `_percentile_<n>d` / `_zscore_<n>d`，required = max_n×24+16；任何缺檔 fail-open 回 None |
| F1 生效 | `dashboard/trader/loop.py` | 預設 lookback 200→**2200**（`DEFAULT_LOOKBACK`）；`resolve_lookback` 取 max(CLI, YAML 需求) |
| F1 覆寫通道 | `dashboard/trader/manager.py` | `control.json` 可帶 `lookback` → `--lookback` 傳給 loop |
| F3 fail-loud | `signal.py` + `loop.py` | `compute_signal(min_bars=)`：K 線根數 < 需求 → 不跑 engine、回 `insufficient_history` → loop **pause + critical alert**（一次），資料補足自動 resume |
| 回歸釘死 | `test_signal_pagination.py` | 端到端測試：同一顆 rolling(2160, min_periods=1080) engine，200 根 → signal 0（原 bug）、2200 根 → signal 1 |

測試：trader scope **89 passed**（新增 25）、dashboard/server scope 242 passed。eth_s5/sol_s1 需求推導值 = 90×24+16 = **2176** ≤ 預設 2200 ✓。

### 部署（需使用者執行，我不碰伺服器）
1. commit + push 本分支 → 伺服器 `git pull`。
2. `cd dashboard && docker compose -f docker-compose.trader.yml up -d --build`（trader image 重建 + manager 會自動 respawn loops；dashboard stack 不用動）。
3. 驗證：下一個整點 tick 後看 `runs/testnet/*/testnet_status.json` —— 正常情況不再需要任何 lookback 手動設定；若出現 `signal history insufficient` critical alert = F3 守門在叫，檢查交易所 K 線深度。
4. 提醒：修活後 eth_s5/sol_s1 在當前 bear regime 只有空單側可觸發；一段時間 0 筆仍可能是正常的 —— 「預期 vs 實際頻率」常設監控是 Part A 的 D2-監控項（P1，另行核准）。

## 連帶修訂

- Part A 報告 D2 診斷段：結論由「待驗生死」改為「**已確診：結構性壞死**」；D2-監控段的優先序理由增強（本案為實證）。
- agy 二審的「trader 已癱瘓」直覺：**方向對了**（結論正確），但其推理（API 斷線/regime 未更新）不對 —— 真因是 lookback 資料窗。記錄以示公允。
