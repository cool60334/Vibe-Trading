## Context

ETH s4/s5 的兩個手寫優化（regime overlay、size scaling）已被真實 OOS + 壓測驗證（OOS sharpe 1.02 / DD 9.1% / 3x-fee worst sharpe 0.675 / gate 全綠），但只存在於 `research/strategies/code/eth_s5_half_size/signal_engine.py` 手寫檔。本 change 把它們固化進 DSL → compiler → archetype factory → sweep 四層，讓新幣種全自動跑出同等級策略。

相關現況：

- **Compiler**：`research/lib/signal_compiler.py::compile_strategy(spec, yaml_hash) -> str` 為純字串 codegen（非 Jinja 檔案模板）。進場訊號於 `entry_long.iloc[bar_i]` / `entry_short.iloc[bar_i]`（line ~344-348）判斷；輸出於 line ~372 `signal.iloc[bar_i] = float(position)`。
- **Schema**：`StrategySpec`（`dashboard/server/schemas.py` ~line 729）只列 compiler-relevant 欄位（name/archetype/symbol/timeframe_signal/indicators/entry_long/entry_short/exit_rules），`extra="ignore"` 把 position_sizing 等 metadata 全忽略。
- **參考實作**：`research/strategies/code/eth_s4_regime_filtered/signal_engine.py` 的 `_load_regime_series()`（daily breakdown → ffill 對齊 hourly、fail-soft 全 neutral）與 eth_s5 的 `SIZE_MULT` 已是經 OOS 驗證的程式碼模式，compiler codegen 直接以它為模板。
- **Factory**：`stage2_strategies.py::_generate_for_symbol_multi()` 依 `pick_archetypes()` 回傳的 `ArchetypePlan` list 逐一 build + emit + `register_strategy()`。
- **Sweep**：`stage4_optimize.py::apply_overrides_to_spec(base_spec, overrides)` 改 spec dict → 重編譯 → 跑回測。新增 sweep 維度 = 在此函式加一個 key 處理 + scaffold 的 `parameter_search_ranges` 加 range。

## Goals / Non-Goals

**Goals:**
- YAML 寫 `regime_filter: true` + `size_mult: 0.45` 即可編譯出與 eth_s5 手寫版行為等價的 engine。
- Archetype factory 自動 fan-out regime 變體；size_mult 進 stage 4 sweep 自動調。
- 既有 YAML（無新欄位）編譯輸出 byte-for-byte 不變。
- 手寫策略（manual marker）完全不受影響。

**Non-Goals:**
- 不改 backtest engine（已支援 |signal|<1 部位縮放）。
- 不改 regime 標籤演算法（stage 2.5 既有 regime_<sym>.json）。
- 不做 regime-conditional sizing（bull 加倉等進階變體——記 backlog）。
- 不回頭把 eth_s5 手寫檔改成 DSL 版（等價驗證任務除外，見 D6）。

## Decisions

### D1: DSL 欄位放 StrategySpec 頂層

```python
class StrategySpec(_Manifest):
    ...
    regime_filter: bool = False
    size_mult: float = Field(default=1.0, gt=0.0, le=1.0)
```

不放 `position_sizing` block 內——該 block 是 compiler 忽略的 metadata（`extra="ignore"`），把 compiler-relevant 欄位混進去會破壞「strictly typed compiler fields」的既有設計線。頂層欄位 + 預設值 = 向後相容。

### D2: regime mask codegen — 複製 eth_s4 已驗證模式

`compile_strategy` 當 `spec.regime_filter` 為 true 時：

1. 生成 module-level `_load_regime_series(symbol_short, target_index)` helper（內容同 eth_s4 手寫版：讀 `research/manifests/regime_{symbol_short}.json` 的 `breakdown`、daily→hourly ffill、任何失敗回全 `"neutral"`）。
2. `symbol_short` 編譯期從 `spec.symbol` 推導（`"ETH-USDT-SWAP"` → `"eth"`，取第一段 lower）。
3. 在 entry 訊號計算後注入：

```python
regime = _load_regime_series("eth", ohlcv.index)
entry_long = entry_long & (regime != "bear")
entry_short = entry_short & (regime != "bull")
```

Fail-soft 語義：regime json 缺/壞 → 全 neutral → mask 無作用 → 行為退化為 base 版（不 crash）。這正是 trader docker 等缺檔環境的安全預設。

### D3: size_mult codegen — 一行

輸出行 `signal.iloc[bar_i] = float(position)` 變為：

```python
signal.iloc[bar_i] = float(position) * {spec.size_mult}
```

`size_mult == 1.0` 時生成 `* 1.0` 或省略——**省略**（條件判斷，==1.0 走原字串），保證既有 YAML 編譯輸出 byte-for-byte 不變（D1 goal 的驗證手段：對既有 yaml 重編譯 diff 為空）。

### D4: factory regime 變體 fan-out

`pick_archetypes()` 維持不動（純因子→archetype 邏輯）。變體展開放在 `_generate_for_symbol_multi()` 層：對每個 `ArchetypePlan` 產出的 spec，再複製一份設 `regime_filter: true`、strategy id 加後綴 `_regime`、hypothesis 註明 overlay。理由：

- router 保持純函式（因子 → 結構），變體是 emission 政策不是 archetype 結構。
- fan-out ×2 的計算成本由 stage 3 `archetype_misfit` fail-fast + diag 路由自然消化。
- 兩版並存讓 stage 4/5 用 walk-forward 數據決定誰活——不假設 regime 對每幣種都有效（目前僅 ETH 單幣證據）。

### D5: size_mult 進 sweep

- `_default_spec_scaffold` 的 `parameter_search_ranges` 加 `"size_mult": [0.4, 1.0, 0.2]`（4 值：0.4/0.6/0.8/1.0）。
- `apply_overrides_to_spec` 加：`overrides["size_mult"]` 存在時直接設頂層欄位（最簡單的 override——不像 lookback 要改寫 DSL 字串）。
- Sweep 目標函數不變（sharpe 排名）。注意：size_mult 縮放對 sharpe 近乎中性（scale-invariant），對 DD 線性——sweep 選 best 仍以 sharpe 排，所以 size_mult 的實際價值在 stage 5 gate 的 DD 門檻（OOS DD ≤10%）。**Sweep 排名 tie-breaking 已足夠**：sharpe 同級時任一 size_mult 都可能勝出，而 walk-forward holdout 用 best 參數跑 → gate 用 OOS DD 判。若未來發現 sweep 總選 1.0（sharpe 完全相等時的順序偏差），再加「DD 過 gate 優先」的 tie-break——不在本 change 範圍。

### D6: 等價驗證（acceptance 核心）

用 DSL 重建 eth_s5：寫一份 `regime_filter: true` + `size_mult: 0.45` + s3 sweep_092 參數的 YAML（測試 fixture，不入 registry），編譯後跑 train+OOS 視窗回測，metrics 與 `runs/eth_s5_half_size_{train,oos}` 既有 artifacts 對比，sharpe/DD/trades 容差內一致（浮點容差 1e-6；理論上應全等，因為手寫版邏輯即是 codegen 模板）。此測試證明「手寫優化已無損固化進 pipeline」。

差異風險點（需在實作時核對 eth_s4 手寫版 vs compiler 既有 codegen 的細節差）：persist 條件的 rolling 窗實作、percentile min_periods、smoothing 順序。若有不可調和差異，等價測試容差放寬並在 design 補記原因。

### D7: PIPELINE.md 與 generation 紀錄

- YAML DSL 速查表加兩欄位說明。
- regime 變體的 `generation.json` 註明 `regime_filter: true` 與 runtime 依賴 `regime_<sym>.json`。

## Risks / Trade-offs

- **[Risk] compiler codegen 與 eth_s4 手寫版細節不一致** → D6 等價測試直接抓；不一致時以手寫版（已 OOS 驗證）為準修 codegen。
- **[Risk] regime json runtime 依賴在 live trader 缺檔** → fail-soft 全 neutral；部署清單須含 regime json 同步（trader stale-parquet backlog 同議題，註記之）。
- **[Trade-off] fan-out ×2 計算量** → fail-fast 消化；可接受。
- **[Trade-off] sweep 網格 ×4** → `--max` 上限既有，粗步長 0.2。
- **[Risk] size_mult 對 sharpe 中性導致 sweep 不偏好它** → 已知並接受（D5）；gate 的 DD 門檻是真正消費者。

## Migration Plan

1. Schema 兩欄位 + 向後相容測試（既有 yaml 重編譯 diff 為空）。
2. Compiler：size_mult（簡單）→ regime mask（codegen helper）+ 單測。
3. D6 等價驗證測試（eth_s5 DSL 重建 vs 既有 runs artifacts）。
4. Factory 變體 fan-out + scaffold search range + 單測。
5. stage4 `apply_overrides_to_spec` size_mult + 單測。
6. E2E：對 btc（已有 0a 產物）跑 stage 2 multi-emit 確認 `_regime` 變體出現、stage 2b 編譯全過。
7. PIPELINE.md。

Rollback：revert diff；預設值保證未用新欄位的一切照舊。
