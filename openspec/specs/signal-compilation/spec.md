# signal-compilation Specification

## Purpose

Stage 2b / signal_compiler 的 YAML→signal_engine 編譯行為契約：StrategySpec compiler 欄位（regime_filter / size_mult）的 schema 約束與其編譯產碼語義。實作於 `dashboard/server/schemas.py::StrategySpec`、`research/lib/signal_compiler.py` 與 `research/strategies/code/_templates/signal_engine.py.j2`。既有編譯行為（指標、進出場 DSL）由本 capability 後續 change 漸進補規範。

## Requirements

### Requirement: StrategySpec regime_filter 與 size_mult 欄位

`dashboard/server/schemas.py::StrategySpec` SHALL 新增兩個 compiler-relevant 欄位：

- `regime_filter: bool`，預設 `False`
- `size_mult: float`，預設 `1.0`，約束 `0 < size_mult <= 1`

兩欄位 MUST 有預設值，使既有（未含新欄位的）strategy YAML 通過 schema 驗證且語義不變。`size_mult` 超出 `(0, 1]` 範圍 MUST 觸發 Pydantic validation error。

#### Scenario: 既有 YAML 無新欄位仍驗證通過

- **GIVEN** 一份不含 `regime_filter` / `size_mult` 的既有策略 YAML
- **WHEN** 以 `StrategySpec.model_validate(...)` 驗證
- **THEN** 驗證 MUST 通過
- **AND** `spec.regime_filter` MUST 為 `False`、`spec.size_mult` MUST 為 `1.0`

#### Scenario: size_mult 超界被拒

- **GIVEN** YAML 含 `size_mult: 1.5`（或 `0`、負值）
- **WHEN** 驗證
- **THEN** MUST raise validation error

### Requirement: size_mult 編譯為訊號縮放

`research/lib/signal_compiler.py::compile_strategy` SHALL 在 `spec.size_mult != 1.0` 時，把訊號輸出行編譯為 `signal.iloc[bar_i] = float(position) * <size_mult>`。當 `size_mult == 1.0` 時輸出行 MUST 與改動前完全相同（既有 YAML 重編譯結果 byte-for-byte 不變）。

#### Scenario: size_mult 0.45 編譯輸出縮放

- **GIVEN** spec 含 `size_mult: 0.45`
- **WHEN** `compile_strategy(spec)` 產碼
- **THEN** 產出碼 MUST 含 `float(position) * 0.45`
- **AND** 編譯後 engine 對同一進場訊號輸出的 signal 值 MUST 為 `±0.45`

#### Scenario: 預設 size_mult 輸出不變

- **GIVEN** spec 之 `size_mult == 1.0`
- **WHEN** 產碼
- **THEN** 訊號輸出行 MUST 為 `signal.iloc[bar_i] = float(position)`（與改動前字面相同）

### Requirement: regime_filter 編譯為進場 mask

`compile_strategy` SHALL 在 `spec.regime_filter` 為 true 時：

1. 生成讀取 `research/manifests/regime_<symbol_short>.json` 的 helper（`symbol_short` 由 `spec.symbol` 第一段小寫推導，如 `ETH-USDT-SWAP` → `eth`），將 daily `breakdown` 標籤以 forward-fill 對齊到回測 bar index。
2. 在 entry 訊號計算後注入 mask：`entry_long &= (regime != "bear")`、`entry_short &= (regime != "bull")`。
3. Fail-soft：regime json 不存在、無法解析、或 `breakdown` 空 → 全部視為 `"neutral"`（mask 無作用），MUST NOT raise。

`spec.regime_filter` 為 false 時產出碼 MUST 不含任何 regime 相關程式碼（既有 YAML 編譯輸出不變）。

#### Scenario: regime mask 擋掉 bear 期多單

- **GIVEN** `regime_filter: true` 的 spec 編譯出的 engine
- **AND** regime json 標某時段為 `bear`
- **AND** 該時段內因子條件本應觸發多單進場
- **WHEN** engine 跑該時段
- **THEN** 該時段 MUST NOT 開多單
- **AND** 同 regime 下空單進場 MUST 不受影響

#### Scenario: regime json 缺檔 fail-soft

- **GIVEN** `regime_filter: true` 的 engine 在 `regime_<sym>.json` 不存在的環境執行
- **WHEN** `generate(...)` 被呼叫
- **THEN** MUST NOT raise
- **AND** 輸出訊號 MUST 等同 `regime_filter: false` 版本（mask 退化為無作用）

#### Scenario: regime_filter false 產碼無 regime 痕跡

- **GIVEN** spec 之 `regime_filter == False`
- **WHEN** 產碼
- **THEN** 產出碼 MUST 不含 `_load_regime_series` 或 regime 字樣的邏輯（與改動前輸出一致）

### Requirement: eth_s5 DSL 等價重建

系統 MUST 能以純 DSL（`regime_filter: true` + `size_mult: 0.45` + eth_s3 sweep_092 最佳參數）重建與手寫 `eth_s5_half_size` signal_engine 行為等價的 engine。等價以回測 metrics 驗證：同視窗（train 2022-06→2025-01、OOS 2025-01→2026-06）重跑，sharpe / max_drawdown / trade_count 與既有 `runs/eth_s5_half_size_{train,oos}/artifacts/metrics.csv` 在容差內一致。此測試作為「手寫優化已無損固化進 pipeline」的 acceptance 證明。

#### Scenario: DSL 重建 eth_s5 OOS metrics 一致

- **GIVEN** 以 DSL 欄位重建的 eth_s5 等價 spec 編譯出的 engine
- **WHEN** 在 OOS 視窗（2025-01-01 → 2026-06-02）跑回測
- **THEN** sharpe MUST ≈ 1.016、max_drawdown MUST ≈ -9.13%、trade_count MUST == 49（浮點容差內）
