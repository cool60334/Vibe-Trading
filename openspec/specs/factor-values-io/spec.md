# factor-values-io Specification

## Purpose

IO helpers in `research/lib/factor_io.py` for the feature store and evidence table: reading features and meta, deterministic evidence read/write, and the agent composite-factor append convention.

## Requirements

### Requirement: feature store 讀取與 meta helper

系統 SHALL 擴充 `research/lib/factor_io.py`，公開 feature store 讀取函式：

- `load_features(symbol: str) -> pd.DataFrame`：讀 `research/manifests/features_<symbol_short>.parquet`，回傳 DataFrame（索引為 UTC tz-aware hourly DatetimeIndex，每欄為一 feature key 之 `float64` 序列）；檔不存在 SHALL raise `FileNotFoundError`，訊息 MUST 指引使用者先跑 `stage0a_features`。
- `load_features_meta(symbol: str) -> dict`：讀 sidecar `features_<sym>.meta.json`；`schema_version` 不符當前版本 SHALL raise `ValueError`。

此 helper SHALL 與既有 `load_factor_values` 共用同一套路徑解析與 symbol 正規化邏輯（小寫短名），以維持單一 IO 來源。Stage 1（`factor_extended`）MUST 透過此 helper 自 feature store 取得因子序列，MUST NOT 在該路徑重新呼叫 source fetcher。

#### Scenario: helper 讀回 0a 寫出的 features parquet

- **WHEN** Stage 0a 已產出 `features_btc.parquet`
- **AND** 呼叫 `load_features("btc")`
- **THEN** 回傳 MUST 為 `pd.DataFrame`，columns MUST 等於 meta 中 `feature_names`
- **AND** index MUST 為 `pd.DatetimeIndex` 且 tz 為 UTC

#### Scenario: features parquet 缺失時 raise 明確錯誤

- **WHEN** `features_btc.parquet` 不存在
- **AND** 呼叫 `load_features("btc")`
- **THEN** MUST raise `FileNotFoundError`
- **AND** 錯誤訊息 MUST 包含指引先跑 `stage0a_features` 的字樣

### Requirement: evidence 表讀寫 helper

系統 SHALL 在 `research/lib/factor_io.py` 公開 evidence 排名表之確定性讀寫 helper：

- `dump_evidence(symbol: str, evidence) -> Path`：將 evidence 條目（可為 pydantic 模型或其 dict 序列）以確定性 JSON（穩定排序、UTF-8、`ensure_ascii=False`）寫出至 `research/manifests/evidence_<sym>.json`，回傳寫出路徑。
- `load_evidence(symbol: str) -> dict`：讀回 `evidence_<sym>.json`；檔不存在 SHALL raise `FileNotFoundError`。

寫出之 JSON MUST 為可被對應 pydantic 模型 `model_validate` 驗證之結構（與 `feature-evidence-build` 之證據表契約一致）。

#### Scenario: evidence 寫出後可讀回並驗證

- **WHEN** 以一組 evidence 條目呼叫 `dump_evidence("btc", entries)`
- **THEN** `research/manifests/evidence_btc.json` MUST 存在
- **AND** 以 `load_evidence("btc")` 讀回之結構 MUST 能通過對應 pydantic 模型驗證

### Requirement: agent 複合因子 append 約定

系統 SHALL 在 `research/lib/factor_io.py` 公開 `append_feature_column(symbol: str, key: str, series: pd.Series) -> None`，供 researcher agent 將自算之複合因子序列以具名欄寫回 feature store，使下游可按 `feature_key` 重現取值。

該函式 MUST：(1) 將輸入 series 重新對齊至既有 features parquet 之 index；(2) 拒絕全為 NaN 之序列（raise `ValueError`）；(3) 拒絕非有限值覆蓋率低於設定門檻之序列（預設覆蓋率門檻可由 config 調整）；(4) 若 `key` 已存在則覆寫該欄；(5) 同步更新 `features_<sym>.meta.json` 之 `feature_names` 與 `n_rows`。寫入 MUST 採與既有 dump 相同之 pyarrow / snappy 設定。

stage 0 接受任何 candidate 之 `feature_key` 前，MUST 確認該 key 已存在於更新後之 feature store 且非全 NaN；不符則丟棄該 candidate（對應 `factor-discovery` 之驗證契約）。

#### Scenario: append 複合因子後欄位可被讀回

- **WHEN** 對既有 `features_btc.parquet` 呼叫 `append_feature_column("btc", "mom_funding_combo", series)`
- **THEN** `load_features("btc")` 之 columns MUST 包含 `mom_funding_combo`
- **AND** `features_btc.meta.json` 之 `feature_names` MUST 包含 `mom_funding_combo`

#### Scenario: 全 NaN 序列被拒

- **WHEN** 以一條全為 NaN 之 series 呼叫 `append_feature_column`
- **THEN** MUST raise `ValueError`
- **AND** feature store 之欄位集合 MUST NOT 改變
