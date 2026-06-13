## 1. Schema 欄位

- [x] 1.1 `dashboard/server/schemas.py::StrategySpec` 加 `regime_filter: bool = False`
- [x] 1.2 加 `size_mult: float = Field(default=1.0, gt=0.0, le=1.0)`
- [x] 1.3 schema 測試：既有 yaml（無新欄位）驗證通過、預設值正確；size_mult 超界（1.5 / 0 / 負）被拒

## 2. Compiler — size_mult

- [x] 2.1 `research/lib/signal_compiler.py`：`size_mult != 1.0` 時輸出行改 `float(position) * <val>`；`== 1.0` 時輸出行與現狀字面相同
- [x] 2.2 測試：size_mult 0.45 產碼含 `* 0.45`、編譯後 engine 訊號為 ±0.45
- [x] 2.3 測試：預設 1.0 時對既有 fixture yaml 重編譯，輸出與改動前 byte-for-byte 相同（向後相容鐵證）

## 3. Compiler — regime mask

- [x] 3.1 codegen 生成 `_load_regime_series(symbol_short, target_index)` helper（以 eth_s4 手寫版為模板：讀 regime json breakdown、daily ffill 對齊、fail-soft 全 neutral）
- [x] 3.2 `symbol_short` 編譯期由 `spec.symbol` 推導（`ETH-USDT-SWAP` → `eth`）
- [x] 3.3 entry 訊號後注入 `entry_long &= (regime != "bear")`、`entry_short &= (regime != "bull")`
- [x] 3.4 `regime_filter: false` 產碼無任何 regime 邏輯（重編譯 diff 為空）
- [x] 3.5 測試：mask 擋 bear 期多單、不擋同期空單（合成 regime json fixture）
- [x] 3.6 測試：regime json 缺檔 → 不 raise、訊號等同無 mask 版
- [x] 3.7 測試：false 時產碼不含 `_load_regime_series`

## 4. eth_s5 DSL 等價驗證（acceptance 核心）

- [x] 4.1 建測試 fixture yaml：`regime_filter: true` + `size_mult: 0.45` + s3 sweep_092 參數（lookback 90 / entry 70/15 / hold 168 / sl 3 / tp 8.5 / persist 2/3 / signal_invalidation 40-60）
- [x] 4.2 編譯後與 `research/strategies/code/eth_s5_half_size/signal_engine.py` 手寫版邏輯逐項核對（percentile 窗、min_periods、smoothing、persist 實作）；記錄任何不可調和差異
- [x] 4.3 跑 OOS 視窗（2025-01-01→2026-06-02）回測，metrics 對比 `runs/eth_s5_half_size_oos/artifacts/metrics.csv`：sharpe≈1.016、DD≈9.13%、trades==49（容差內）
- [x] 4.4 跑 train 視窗同樣對比 `runs/eth_s5_half_size_train`
- [x] 4.5 若有差異：以手寫版為準修 codegen 或在 design.md 補記原因 + 放寬容差

## 5. Factory regime 變體 fan-out

- [x] 5.1 `_generate_for_symbol_multi()`：每個 ArchetypePlan 的 spec 額外 emit `regime_filter: true` 變體、id 後綴 `_regime`
- [x] 5.2 變體 `generation.json` 註明 regime overlay + runtime 依賴 `regime_<sym>.json`
- [x] 5.3 變體一併 `register_strategy()` 進 strategy_runs.json（thread `runs_path`，沿用 Defect-1 修正模式避免測試污染真檔）
- [x] 5.4 測試：2 archetype → 4 份 yaml/generation、id 正確、變體與 base 僅差 regime_filter
- [x] 5.5 測試：變體 yaml 過 stage2b 編譯（schema + AST）

## 6. Scaffold + stage 4 sweep size_mult

- [x] 6.1 `_default_spec_scaffold` 的 `parameter_search_ranges` 加 `"size_mult": [0.4, 1.0, 0.2]`
- [x] 6.2 `stage4_optimize.py::apply_overrides_to_spec` 支援 `size_mult` key（直設頂層欄位）
- [x] 6.3 測試：override 0.6 → spec.size_mult==0.6、重編譯 engine 訊號 ±0.6
- [x] 6.4 測試：override 缺 size_mult → 不動原值
- [x] 6.5 跑既有 stage4 測試套件確認無退化

## 7. E2E smoke

- [x] 7.1 對 btc（0a 產物齊備）跑 `python -m research.pipeline.stage2_strategies`：確認 `_regime` 變體 emit、全部過 2b 編譯
- [x] 7.2 抽一個 `_regime` 變體跑 stage 3 base 回測確認可執行（regime json 存在路徑）
- [x] 7.3 全測試套件（research/）綠

## 8. Docs + 收尾

- [x] 8.1 `research/PIPELINE.md`：DSL 速查表加 `regime_filter` / `size_mult` 兩欄位；stage 2 章節註明 regime 變體 fan-out；stage 4 註明 size_mult sweep
- [x] 8.2 trader 部署註記：regime json 為 compiled engine runtime 依賴（與 parquet stale 同議題）
- [x] 8.3 memory ADR + MEMORY.md 索引
- [x] 8.4 `openspec validate dsl-regime-and-size-mult`
