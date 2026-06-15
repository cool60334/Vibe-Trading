# Stage 3 Interval-Correct Backtest (factor-data namespacing) — Design

- **日期**: 2026-06-15
- **狀態**: 設計定案（已 Gemini review），待寫 plan
- **作者**: Eric（brainstorming with Claude）
- **關聯**: 完成 intraday Phase 1/2 後的修復；同時關掉前端「依 interval 檢視」案的 write-side Prerequisite P

## 1. 問題

在 15m/30m 跑 stage3 回測時，結果其實是 **1H 回測**（ETH@30m 報 Sharpe 1.71 是假象）。

根因不在資料抓取——`agent/backtest/loaders/okx.py` 完整支援 `interval`（驗證 15m/30m、`bar=interval`、分頁也調過），`runner.py` 把 `config["interval"]` 傳給 loader，`stage3_backtest.build_run_config` 寫 `cfg.interval`。candle 在「stage3 有帶 `RESEARCH_INTERVAL`」時是對的。

真因是 **factor 資料寫死 `research/manifests/` root**：
- **讀**：每個 signal_engine 呼叫 `load_factor_values(symbol)`（無 manifests_dir）→ `factor_io.py` 預設 `_DEFAULT_MANIFESTS_DIR = research/manifests`（root，1H），不理 interval。
- **寫**：`factor_extended.resolve_manifests_dir()`、`factor_regime._resolve_manifests_dir()`、`stage1_factors.main()`、`emit_manifest.py:854` 全寫死 root。

所以即使抓了 30m K 線，signal_engine 讀的是 **root 的 1H factor_values** → 訊號是 1H → 等同 1H 回測。Phase 1 只把 stage0a 的 features/evidence 命名空間到 `manifests/<iv>/`；factor_values 與下游 manifest 沒跟上。

## 2. 性質：長期修復，非短期 hack

修「資料佈局」本身（factor 資料依 interval 命名空間）——一勞永逸、未來 5m/4H 免改。唯一務實處是傳遞管道用 env（見 §3 決策 2），但以 per-run 權威來源消除洩漏，安全。**最純版**（interval 當顯式參數一路串穿、regen 所有 signal_engine）成本高效益邊際，**故意不做**，列為未來 hardening。

## 3. 決策（含 Gemini review 採納）

1. **單一 `active_manifests_dir()` helper**（放 `research/lib/timeframe.py`，Phase 1 已建）：讀 `RESEARCH_INTERVAL` → `research/manifests`（1H/未設）或 `research/manifests/<iv>`（sub-hour，驗證 ∈ SUPPORTED_INTERVALS）。**五個** root-resolver 全 delegate 給它（DRY、單一真相源）。
2. **runner 從 config.json 設 interval**（消除 env 洩漏）：`runner.py` 跑 signal_engine 前 **無條件** `os.environ["RESEARCH_INTERVAL"] = config.get("interval", "1H")`。per-run 的 config.json 是權威來源，覆蓋任何 shell 殘留 → Gemini 的「spooky env 洩漏」解掉。對既有/手寫/生成的 signal_engine 全部即時生效，**免 regen**。
3. **稽核全部 factor/feature 讀取點**（Gemini）：stage2/2b/2.5 等若以無參數呼叫 `load_features`/`load_evidence`/`load_factor_values`，吃 §決策1 的預設即自動正確；若有顯式傳 root 的，改掉。避免「30m top picks = 1H 贏家」。
4. **stage3 `_run_backtest` 顯式帶 env**（Gemini，保險）：子行程 env 明確含 `RESEARCH_INTERVAL`，不單靠繼承。
5. **index 頻率 guard**（Gemini）：`load_factor_values` 讀到的 parquet 索引間距與 interval 不符時印警告，防 silent stale。
6. **emit_manifest 也 namespace**：strategy manifest + selection.json 落 `manifests/<iv>/`，否則 30m 策略 manifest 撞 root、stage5 selection 讀錯。順帶關掉前端案的 write-side Prerequisite P。

## 4. 受影響檔案

| 檔 | 改動 |
|----|------|
| `research/lib/timeframe.py` | 新增 `active_manifests_dir()` |
| `research/lib/factor_io.py` | 各 `load_*`/meta 預設 dir 走 helper；加 index guard |
| `research/factor_extended.py` | `resolve_manifests_dir()` delegate helper |
| `research/factor_regime.py` | `_resolve_manifests_dir()` delegate helper |
| `research/pipeline/stage1_factors.py` | `main()` 的 manifests_dir 走 helper |
| `research/emit_manifest.py` | `main()` 的 manifests_dir 走 helper |
| `agent/backtest/runner.py` | 跑 engine 前設 `RESEARCH_INTERVAL = config["interval"]` |
| `research/pipeline/stage3_backtest.py` | `_run_backtest` 顯式帶 env（保險） |

## 5. 零 1H 回歸

`active_manifests_dir()` 在 `RESEARCH_INTERVAL` 未設或 `1H` 時回 `research/manifests`（root）——與現況逐字相同。所有現有 1H 跑、tracked curated manifest、部署路徑不動。新行為只在 sub-hour 觸發。

## 6. 測試

- research pytest：`active_manifests_dir`（1H/未設→root、15m/30m→子目錄、非法→raise）；factor_io 預設解析 + index guard；各 resolver delegate。
- agent pytest：runner 設 env from config（1H 與 30m）；零回歸。
- **eth@30m smoke**（操作）：跑 stage1→3，驗 factor_values 落 `manifests/30m/`、run card interval=30m、factor parquet 索引間距=30m、bar 數符合 30m。
- 註：research 與 agent pytest 必須分開跑。

## 7. 風險

| 風險 | 緩解 |
|------|------|
| env 洩漏到無關 1H 跑 | runner 從 config 無條件設、stage 主程序由 load_config 的 RESEARCH_INTERVAL 驅動；測試用 monkeypatch 隔離 |
| 兩套命名空間機制（cfg.feature_store_path vs active_manifests_dir）不一致 | 兩者都讀同一 RESEARCH_INTERVAL、解析到同路徑；stage0a 維持 feature_store_path 不動（同結果） |
| root 殘留舊檔被誤讀 | smoke 確認 30m 讀到 `manifests/30m/`；必要時清 root 殘檔 |
| dashboard 觸發的 intraday 仍 1H | 已知限制：pipeline job 還沒帶 interval（屬前端案）；現走 CLI `RESEARCH_INTERVAL=` |

## 8. 不在範圍

- 前端「依 interval 檢視」（dashboard 讀取層 + API + selector）——獨立案，本案只關它的 write-side 前置。
- 最純的 interval 顯式串穿 + signal_engine 模板 baking（未來 hardening）。
- pipeline job / dashboard 帶 interval 的觸發（前端案）。
