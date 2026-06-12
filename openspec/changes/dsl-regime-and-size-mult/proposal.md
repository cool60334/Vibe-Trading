## Why

ETH 策略鏈 s1→s5 的四輪迭代中，把 OOS sharpe 從 0.25 推到 1.02 並通過 stage 5 gate 的兩個關鍵改動，至今只存在於 `eth_s5_half_size` 的**手寫** signal_engine（`# manual: do-not-overwrite`）裡：

1. **Regime overlay**（s4：bear 不做多、bull 不做空）— OOS sharpe 0.25→1.00，且 train/OOS gap 從 -1.16 縮到 -0.19（整鏈最大跳躍、最強的去 overfit 機制）。
2. **Signal magnitude sizing**（s5：`SIZE_MULT=0.45`，輸出 ±0.45 而非 ±1.0）— OOS DD 19.3%→9.1%，是通過 gate `max_drawdown ≤ 10%` 的關鍵；live 交易所最小 leverage 1x，magnitude scaling 是唯一可部署的降倉手段。

目前對新幣種跑全自動 pipeline（stage 0a→5），天花板停在 eth_s3 等級（trend_with_gate + signal_invalidation 動態出場，OOS sharpe ~0.25）。新幣種要重現 s4/s5 等級必須再靠 claude/人工手寫一次 signal_engine——違反「其他幣種都可以用這套流程找出 alpha 策略」的目標。

## What Changes

- **StrategySpec DSL 加兩個 compiler 欄位**（`dashboard/server/schemas.py`）：
  - `regime_filter: bool = False` — true 時編譯出的 engine 在進場前套 regime mask（bear 擋多、bull 擋空、neutral 雙向）。
  - `size_mult: float = 1.0`（0 < x ≤ 1）— engine 輸出 `±size_mult` 而非 `±1.0`。
  - 兩者皆有預設值 → 既有 YAML 全部向後相容、行為不變。
- **signal_compiler 編譯支援**（`research/lib/signal_compiler.py`）：
  - `regime_filter: true` → 生成讀 `research/manifests/regime_<sym>.json` 的 mask helper（daily 標籤 ffill 對齊 hourly bar；檔缺/壞 → 全 neutral = 無 mask，fail-soft，同 eth_s4 手寫版已驗證的模式）。
  - `size_mult` → 訊號輸出行乘上該值。
- **Stage 2 archetype factory fan-out regime 變體**（`research/pipeline/stage2_strategies.py` + `archetype_router.py`）：每個 archetype 額外 emit 一個 `regime_filter: true` 變體（id 後綴 `_regime`），讓 stage 3 fail-fast / stage 4 walk-forward 用數據決定 base 與 regime 版誰活。
- **Scaffold + sweep 接 size_mult**：`_default_spec_scaffold` 的 `parameter_search_ranges` 加 `size_mult`（如 `[0.4, 1.0, 0.2]`）；`stage4_optimize.py::apply_overrides_to_spec` 支援 `size_mult` override key → sweep 自動找「DD 過 gate 的最大倉位」，取代 eth_s5 的手調 0.45。

## Capabilities

### New Capabilities

- `signal-compilation`: stage 2b / signal_compiler 的 YAML→engine 編譯行為契約（現無 spec 覆蓋此層）。本 change 定義其中 regime mask 與 size_mult 兩項編譯需求；既有編譯行為不在本 change 規範範圍。

### Modified Capabilities

- `strategy-archetype-synthesis`: factory fan-out 加 regime 變體；shared scaffold 加 size_mult 預設與 search range。

## Impact

**改動檔案**：
- `dashboard/server/schemas.py` — StrategySpec 加 2 欄位（有預設、向後相容）
- `research/lib/signal_compiler.py` — regime mask 注入 + size_mult 輸出
- `research/pipeline/stage2_strategies.py` + `research/pipeline/lib/archetype_router.py` — regime 變體 fan-out + scaffold search range
- `research/pipeline/stage4_optimize.py` — `apply_overrides_to_spec` 加 size_mult
- 對應測試（compiler / factory / stage4）+ `research/PIPELINE.md`

**不變**：
- backtest engine（site-packages）— engine 本就吃 [-1,1] 訊號，`target_notional = |signal| × equity × leverage`，零改動
- stage 3 / 3-diag / 5、gate、manifest、dashboard
- 既有手寫策略（`# manual: do-not-overwrite` 跳過編譯，不受影響）
- `regime_<sym>.json` 產生方式（stage 2.5 既有產物）

**Breaking changes**：無。預設值使所有既有 YAML 編譯結果 byte-等價。

**風險**：
- Regime 變體 fan-out 使策略數 ×2 → stage 3/4 計算量 ×2。緩解：stage 3 archetype_misfit fail-fast 會先殺爛的；stage 4 只對 diag 放行者掃。
- regime mask 引入 runtime 檔案依賴（regime_<sym>.json）→ 編譯出的 engine 在缺檔環境（如 trader docker）行為退化為無 mask。緩解：fail-soft 設計 + caveat 寫進 generation 說明；trader 部署時需確認 regime json 同步（已知 parquet stale 議題同類，見 trader backlog）。
- size_mult 進 sweep 增加網格維度（×4 組合數）。緩解：factory 可設粗步長（0.2）；sweep `--max` 上限既有。
