## 1. 依賴與 config

- [x] 1.1 加入 `pandas-ta` 依賴（requirements / pyproject），並驗證在無 TA-Lib C 函式庫環境可 import；踩 numpy 2.0 相容雷則 pin `numpy<2` 或退 `ta` 套件並記錄於 design Risks
- [x] 1.2 在 `research/pipeline/config.py` 新增：指標池清單（config 驅動，預設動量 RSI/MACD/ROC/Stoch、趨勢 EMA/SMA cross/ADX、波動 ATR/BB width/rolling std、量 OBV/MFI/volume z）、feature store 路徑、forward-return horizons、append 覆蓋率門檻

## 2. 指標池計算模組（research/lib/indicators.py）

- [x] 2.1 新增 `research/lib/indicators.py`，公開 `compute_indicator_pool(candles, config) -> dict[str, pd.Series]`，以純 python `pandas-ta` 計算 config 驅動之指標池，每指標輸出唯一具名 key 且與輸入 candle index 對齊
- [x] 2.2 確保所有指標為因果計算（僅 rolling/過去窗，無 look-ahead）
- [x] 2.3 單元測試：回傳序列與輸入 index 同 index 同長度、序列數等於 config 啟用指標數、無 TA-Lib 環境可執行

## 3. factor_io feature store / evidence helper（research/lib/factor_io.py）

- [x] 3.1 新增 `load_features(symbol)` / `load_features_meta(symbol)`，與既有 `load_factor_values` 共用路徑解析與 symbol 正規化；缺檔 raise `FileNotFoundError`（訊息指引先跑 stage0a）、schema_version 不符 raise `ValueError`
- [x] 3.2 新增 features store dump helper（寫 `features_<sym>.parquet` + `features_<sym>.meta.json`，pyarrow/snappy，meta 含 schema_version/symbol/generated_at/feature_names/index_start/index_end/n_rows）
- [x] 3.3 新增 `dump_evidence(symbol, evidence)` / `load_evidence(symbol)`（確定性 JSON，穩定排序、UTF-8、ensure_ascii=False）
- [x] 3.4 新增 `append_feature_column(symbol, key, series)`：對齊既有 index、拒全 NaN（ValueError）、拒覆蓋率低於門檻、key 已存在則覆寫、同步更新 meta `feature_names`/`n_rows`
- [x] 3.5 單元測試：features 讀回欄名/索引 tz、evidence round-trip pydantic 驗證、append 後可讀回、全 NaN 被拒且欄集合不變

## 4. Schema 改動（dashboard/server/schemas.py）

- [x] 4.1 `FactorCandidate` 新增 `feature_key: Optional[str] = None`（向後相容舊 manifest，須讓無 feature_key 之舊 candidates 驗證通過；存在性檢查移至 stage0 runtime，非 schema 層強制），`data_source`/`transform` 改 `Optional[str] = None`
- [x] 4.2 `CandidatesManifest` 已存在於 `dashboard/server/schemas.py`，確認欄位齊備（schema_version/symbol/generated_at/source_swarm_run/candidates）並與既有 `FactorManifest` 同檔
- [x] 4.3 新增 evidence 表 pydantic 模型（每筆含 feature_key/category/ic_by_horizon/ir/sample_size）
- [x] 4.4 更新 `dashboard/server/test_schemas_candidates.py`：`VALID_CANDIDATE` 加 `feature_key`、移除/改選填 data_source/transform；新增「舊缺 feature_key manifest 仍驗證通過」與「新 candidates JSON 通過 `CandidatesManifest.model_validate_json`」案例
- [x] 4.5 跑 `cd dashboard/server && pytest -q` 確認 schema 改動未破壞既有 dashboard 後端測試

## 5. Stage 0a runner（research/pipeline/stage0a_features.py）

- [x] 5.1 新增 `research/pipeline/stage0a_features.py`：thin orchestration shell + 可單元測試 pure-logic helpers，對每 symbol 執行 抓 OHLCV（現有 ccxt fetcher）+ 非價格 fetcher → 算指標池 + 非價格因子 → 寫 feature store → 算多 horizon IC 寫 evidence → verify outputs + exit code
- [x] 5.2 多 horizon IC/IR 計算沿用 `research/lib/factor_metrics`，寫 `evidence_<sym>.json`（依 max |IC| 排序、含 caveat：僅篩選用途、未做交易成本與 multiple-testing 校正）
- [x] 5.3 exit code：全 symbol 成功為 0；單一 symbol 失敗記錄並以非零結束，其餘 symbol 產物仍正常寫出
- [x] 5.4 單元測試：features parquet+meta 與 evidence 三檔存在且 feature 名集合一致、evidence 條目數等於 features 欄數且每筆 feature_key 對應存在欄、依 IC 排序、含價格與非價格特徵

## 6. Swarm preset crypto_factor_lab（2 agents）

- [x] 6.1 改寫 `agent/src/swarm/presets/crypto_factor_lab.yaml` 為恰兩 agent：`researcher` + `skeptic`，移除 `output_formatter`
- [x] 6.2 `researcher` system_prompt 改方法紀律護欄式：禁 look-ahead/資料洩漏、要求經濟邏輯、偏好低相關正交因子、對異常高 IC 保持懷疑、誠實回報 IC；MUST NOT 寫死任何特定因子 IC 方向/先驗（移除 funding 反向先驗）
- [x] 6.3 `researcher` 配 bash + 檔案讀寫工具：可讀 `features_<sym>.parquet`、自算複合因子並以 `append_feature_column` 寫回 store 後以欄名作 `feature_key` 提案
- [x] 6.4 `skeptic` 對候選做過擬合/正交性/經濟邏輯審查
- [x] 6.5 preset 宣告變數 `target_universe`、`horizons_h`、`evidence_path`、`features_path`
- [x] 6.6 驗證：齊備變數執行 `--swarm-run crypto_factor_lab` 不因缺變數 raise、stdout 至少含一個合法 ```json fenced block；agents 清單恰為兩者

## 7. Stage 0 discovery runner（research/pipeline/stage0_discovery.py）

- [x] 7.1 加入 0a 前置依賴檢查：`features_<sym>.parquet` 與 `evidence_<sym>.json` 缺失則不呼叫 swarm、視該 symbol 失敗、寫 `candidates_<sym>.failed.json`、exit code 1
- [x] 7.2 swarm 調用注入 evidence 表與 feature store 路徑變數
- [x] 7.3 新增 `validate_feature_keys` helper：candidate 之 `feature_key` 不在 features parquet 欄集合則丟棄該 candidate（不寫 manifest）並 print 警告
- [x] 7.4 維持 thin shell + pure helpers（`parse_candidates_json`、`verify_outputs`、`compute_exit_code`、`print_summary`），JSON 由程式 + pydantic 確定性處理，不依賴 LLM formatter
- [x] 7.5 更新 `test_stage0_discovery`：全成功 exit 0 + 每 symbol「candidates: N」摘要、0a 產物缺失 exit 1 + failed.json、feature_key 不存在被丟棄

## 8. Stage 1 動態讀取（research/factor_extended.py）

- [x] 8.1 `run_symbol` 不再硬編 `["funding_rate","oi_change_24h","fng"]`，改讀 `candidates_<sym>.json` 並依候選清單動態計算
- [x] 8.2 每候選透過 `feature_key` 自 feature store（`load_features`）取已算序列餵 `evaluate_factor`，MUST NOT 重呼 source fetcher / 重算 transform
- [x] 8.3 feature_key 在 store 缺欄則跳過該因子並 print 警告，不中斷其餘
- [x] 8.4 legacy fallback：`RESEARCH_LEGACY_FACTORS=1` 或 candidates 缺失（且未設 0）→ 退原硬編 3 因子模式並 print 警告
- [x] 8.5 完成後收斂寫 `factor_values_<sym>.parquet`（僅選定因子欄）供 stage2b 按名讀
- [x] 8.6 更新 `test_factor_extended_dynamic`：新模式從 feature store 取值（不呼 fetcher）、缺欄跳過、candidates 缺失走 legacy

## 9. End-to-end 驗證（BTC）

- [x] 9.1 跑 stage0a(BTC)：檢查 `features_btc.parquet` 欄數合理 + `evidence_btc.json` IC 排名合理
- [x] 9.2 跑 stage0 swarm：`candidates_btc.json` 含 `feature_key` 且全部存在於 feature store
- [x] 9.3 跑 stage1：`factor_btc.json` 由 feature store 取值產出、IC/verdict 正常、`factor_values_btc.parquet` 僅含選定因子
- [x] 9.4 跑 stage2b 回歸：`signal_engine` 仍能 `load_factor_values` 按名讀並編譯通過
- [x] 9.5 `pytest research/tests/` 全綠
