# A1 — 回測成本模型三重失真修正（Design Spec）

- **日期**：2026-07-07
- **項目**：Part A / A1（P0，唯一會重寫歷史回測數字的項）
- **前置安全網**：C2 golden-run 合約測試（已凍結 GOLDEN，byte-for-byte）
- **二審**：agy（Gemini）2026-07-07，裁定 Option B（研究側預設 realistic）> A > C；本 spec 採納並補兩層 gating
- **對抗式驗證**：agy-verify 2026-07-07，7 findings 採納 6（#1 legacy 擋 fees、#2 init cache、#3 缺值 fail-loud、#4 guard 下放 loader、#5 補測、#6 PoC 前置）、駁回 1（#7 多資產：build_run_config 為 crypto-only）

---

## 1. 問題（三重失真，皆實碼確認）

| # | 失真 | 現況（實碼位置） | 真實 |
|---|------|------------------|------|
| 1 | **funding 用固定率** | `crypto.py:39` `funding_rate=0.0001`；`_market_hooks.py:211` `notional * funding_rate * direction` 用固定純量，方向永遠「多付空收」 | funding 是歷史序列（會變號），且與訊號相關。對 funding_z contrarian 類方向性偏誤最大 |
| 2 | **平倉收 maker** | `crypto.py:58` `rate = taker if is_open else maker`（開 taker、平 maker 0.0002） | live/paper trader 雙邊市價單 = taker 0.00055；回測每來回便宜 ~4bps |
| 3 | **config fees 未接** | `stage3_backtest.py:214` 僅 `fee_multiplier is not None`（stress run）才塞 fee keys；base run 不帶 → 引擎吃硬編碼 default（taker **0.0005**） | `research_config.yaml` `fees:` 有 taker 0.00055，base run 該用它 |

**合併影響**：既有 Sharpe/return 絕對值系統性偏樂觀；策略間相對排序大致仍可信（同偏誤套全體），但 gate 門檻（OOS Sharpe ≥ 1.0）是用打折成本過關。

---

## 2. 決策：兩層 gating（agy Option B + upstream 相容）

agy 裁定：真錢系統 default 必須最嚴苛；opt-in（原 Option A）會讓 default 停在「已知虛假樂觀」的不保守狀態，違反「模糊選保守側」法則。

但 `crypto.py` 是 upstream open-source（tracked on `main`），不可改其 default 行為。解法 = **兩層**：

| 層 | 檔案 | default | 行為 |
|----|------|---------|------|
| **引擎** | `agent/backtest/engines/crypto.py`、`_market_hooks.py` | **不變（legacy）** | 新 funding-series / both-legs-taker 只在 config 帶對應 key 時啟動。無 key = 現行為，golden-run 綠、upstream 相容 |
| **研究 pipeline** | `research/pipeline/stage3_backtest.py` `build_run_config` | **realistic（B）** | base run 預設塞真實成本 config（config fees + funding 序列路徑 + both-legs-taker）。`research_config.yaml` `legacy_costs: true` 才回舊 |

`legacy_costs` = 除錯後門（跑 golden 對比 / 舊策略驗屍），非日常路徑。

### 三修正的引擎 config 介面

1. **both-legs-taker**：新 config key `taker_both_legs: true`。`calc_commission` 兩邊都用 `taker_rate`。無 key = legacy（開 taker 平 maker）。
2. **config fees 接線**：realistic 路徑 `build_run_config` 塞 `maker_rate/taker_rate/slippage`（讀 `cfg.fees`）。**（agy #1）`legacy_costs: true` 時必須「不塞」fees keys**，讓引擎退回硬編碼 default（taker 0.0005 / maker 0.0002），才能 byte-for-byte 重現舊回測。絕不可在 legacy 路徑注入 cfg.fees 的新版 0.00055。
3. **funding 序列**：新 config key `funding_series_path`（parquet 路徑，指向 feature store `features_<sym>.parquet` 的 `funding_rate_raw` 欄）。fallback 語意**必須分兩種 case（agy #3，最關鍵）**：
   - **未提供路徑**（legacy 或 funding 序列出包時手動抽掉）→ fallback 固定 `funding_rate`。天然解耦、安全（回應 agy coupling 顧慮，免第二旗標）。
   - **已提供路徑、但某結算時點值為 NaN / 遺漏** → **絕不 fallback，立即 fail-loud 拋 Exception 中斷回測**，強迫修補特徵檔。否則同一回測半段真半段假 = 靜默污染。
   - **載入時機（agy #2）**：引擎 `__init__` 一次性把該資產 parquet 讀入記憶體，轉 timestamp-keyed dict（O(1) 查）；禁止逐 bar 反覆 I/O 或對大 DataFrame query。

---

## 3. PIT / look-ahead 防護（agy 標記最大風險）

funding 注入是唯一引入時間序列的修正。結算值**只在結算 bar 之後可見**，回測不可在結算 bar 開盤就「預知」該筆 funding。

- 結算點 00:00 / 08:00 / 16:00 UTC 的 funding，在**該 bar 收盤結算**、對持倉侵蝕 capital——與現有 `calc_crypto_funding_fee` 的結算時點語意一致（現況就是在 FUNDING_HOURS 的 bar 結算），只是把固定率換成該時點的歷史值。
- **專屬單測**：構造已知 funding 序列，斷言 (a) 結算 bar 用的是「該結算時點對齊的歷史值」非未來值；(b) 非結算 bar 不結算；(c) **未提供路徑**時 fallback 固定率；(d) **已提供路徑但該結算點缺值**時**拋 Exception**（非 fallback）。

---

## 4. cost_model_version 標籤 + 跨策略同池 guard（agy 補強）

遷移期舊 run artifact（v1 假成本）與新 run（v2 真成本）並存於 run 目錄。跨策略比較若混池 → 資金/排行榜偏袒 Sharpe 被高估的 legacy 策略（劣幣驅逐良幣）。

- **標籤**：回測 run 輸出 metrics / manifest 夾帶 `cost_model_version`（`"v1_legacy"` / `"v2_realistic"`）。由 `build_run_config` 依 gating 決定值並寫入 run config，回測結果沿用。
- **Guard（agy #4，下放到共用讀取層）**：不只在 stage5 select / emit_manifest 設點——那會漏 stage4 分析、ensemble、param-opt 等其他載入多 run 的地方。Guard 放**最底層的共用 run/manifest 載入函式**：只要一次載入的 run 集合出現不同 `cost_model_version`，讀取當下立即 fail-loud 報錯停。（實作前先定位該共用 loader；若不存在則在各比較點統一補。）

---

## 5. 測試策略（TDD）

三 pytest scope 分開跑。A1 動 `agent/` 引擎 + `research/` pipeline，主要落 agent 與 research scope。

1. **引擎單測**（`agent/tests/`）：
   - `calc_commission` both-legs-taker key on/off。
   - funding 序列查值 / PIT 對齊 / fallback / **缺值 fail-loud**（§3 四斷言 a–d，其中 (d) 缺值拋 Exception 是 agy #3/#5 重點）。
   - `__init__` 載入 funding parquet → O(1) dict（agy #2/#5）。
2. **golden-run 對比**（C2）：legacy 路徑 byte-identical 綠；realistic 路徑產生**可預期的** Sharpe 下降（記錄 btc/eth/sol delta）。
3. **pipeline 單測**（`research/tests/`）：`build_run_config` 預設塞 realistic config keys；`legacy_costs: true` 回舊；`cost_model_version` 標籤正確。
4. **guard 單測**：跨策略混版 → assert 觸發。

---

## 6. 檔案足跡

| 檔案 | 動作 |
|------|------|
| `agent/backtest/engines/crypto.py` | `calc_commission` both-legs-taker；`__init__` 載入 funding parquet → O(1) dict（agy #2）；on_bar 傳序列查值 |
| `agent/backtest/engines/_market_hooks.py` | `calc_crypto_funding_fee` 加序列查值 + PIT + fallback/fail-loud 分流（agy #3） |
| `research/pipeline/stage3_backtest.py` | `build_run_config` 預設 realistic + `cost_model_version`；legacy 路徑擋 fees 注入（agy #1）。**註：本函式 crypto-only（`source` 恆 okx），無需 asset-class guard（agy #7 駁回）** |
| `research/pipeline/config.py` | 讀 `legacy_costs` 欄位 |
| `research/research_config.yaml` | 註解說明 `legacy_costs`（default 不設 = realistic） |
| 共用 run/manifest loader（實作前定位）+ `stage5_select.py` / `emit_manifest.py` | cost_model_version 同池 guard（agy #4 下放到 loader） |
| `agent/tests/`、`research/tests/` | 上述單測（含缺值 fail-loud、init cache） |

---

## 7. 施工順序（TDD，檢查點制每步停）

0. **業務衝擊 PoC（agy #6，前置）**：拋棄式 script 對一支代表性策略套 realistic 成本（config fees + 真 funding 序列），估 Sharpe delta。**確認斷崖在預期/可接受範圍**再啟動下列工程 TDD；若全軍覆沒需先回報重評 alpha，避免前 4 步白工。
1. 引擎層：both-legs-taker key + 測（最單純、無爭議）
2. 引擎層：funding 序列查值 + `__init__` cache + PIT/fallback/fail-loud 測（最高風險，重點審）
3. pipeline 層：build_run_config realistic default + config fees 接線 + legacy 擋注入 + cost_model_version
4. guard：cost_model_version 同池 fail-loud（下放 loader）
5. golden-run 重跑，記錄 btc/eth/sol Sharpe delta，人工複核與 step 0 PoC 是否一致
6. agy 二審 diff（真錢引擎變更）

**鐵律**：三 pytest scope 分開跑；伺服器唯讀（本項純本地，暫不部署）；每步停等核准。
