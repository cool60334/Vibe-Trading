# Dashboard "View by Timeframe" + Cross-Interval Compare — Design

- **日期**: 2026-06-15
- **狀態**: 設計定案（已 brainstorm + Gemini review），待寫 plan
- **作者**: Eric（brainstorming with Claude）
- **關聯**: 接續 intraday Phase 1/2 + stage3 interval-correct backtest（`2026-06-15-stage3-interval-namespacing`）

## 1. 目標

Dashboard 能依時間級別（1H / 15m / 30m）檢視 strategies 與 factors，加上：
- **跨 interval 比較**：同頁把不同 interval 的策略並排比 sharpe / DD。
- **空狀態友善**：選的 interval 尚無資料時給提示，不空白。

## 2. 現實：full-stack（非純前端）

前端只能顯示 API 給的；API 給 `artifacts.py` 找到的；`artifacts.py` 目前**寫死 `research/manifests/` root**（`*/manifest.json` 一層、`factor_*.json`、`selection.json`、`regime_*.json`），interval-namespaced 的東西全看不到。所以需四層：write-side → read 層 → API → 前端。

## 3. Prerequisite P（write-side namespacing）— ✅ 完成

stage3 interval-namespacing 修復（commits 9a0d1f6→b0a0a7a）+ regime 補洞（3652784）已把**全部** write-side namespace 到 `manifests/<iv>/`：strategy manifest、selection.json、factor_<sym>.json、factor_values、features、evidence、**regime_<sym>.json**（全經 `active_manifests_dir()` + Phase 1 feature_store_path）。**P 全綠，前端案可直接假設 write-side 完整。** 本案只剩 read 層 + API + 前端三層。

## 4. 決策（brainstorm + Gemini review 採納）

1. **UX：全域 interval 選擇器**。nav header 放 1H/15m/30m 切換，整個 dashboard 範圍到該 interval。
2. **狀態：URL search param 為唯一真相**（Gemini）。`?interval=15m`；薄 `useInterval()` hook 讀它；NavLink 保留參數。**不用 localStorage / Context**（避免跨 tab stale）。預設 1H。
3. **API：`?interval=` query**（預設 1H）（Gemini 確認，非「回全部前端過濾」——manifest 可能很大）。加 **`/api/intervals`** 動態掃目錄回有資料的 interval（不寫死 list；未來 5m/4H 免改）。
4. **讀層 `artifacts.py`**：每 list/get 加 `interval: str = "1H"`，`base = manifests_root`（1H）或 `manifests_root/<iv>`（sub-hour）。不存在的 interval 目錄 → 回空、**不 crash**（Gemini）。

   > 註：`artifacts.py` 在 `dashboard/server`，與 `research/` 不同套件、目前不 import research 內部。解析規則（root vs `/<iv>`）是一行，**在 artifacts.py 內聯**而非跨套件 import `research.lib.timeframe`——保持 dashboard 解耦。規則靠約定共用、文件記明。
5. **跨 interval 比較**：Compare 頁對每個有資料 interval 各抓一次、合併，**React key 用複合鍵 `{interval}_{strategy_id}`**（Gemini：避免跨 interval 同 id 撞 key），每欄標 interval 徽章。
6. **空狀態**：選的 interval 回 [] → 「尚無 {interval} 策略（pipeline 可能還沒跑/進行中）」，非空白或無限轉圈。
7. **保留 hybrid root=1H**（駁 Gemini #7 全量搬遷）：1H 留 `manifests/` root，sub-hour 走子目錄。理由：tracked curated manifest、部署 runbook、多處碼綁 root，全搬風險大；解析規則一行隔離。

## 5. 架構層

```
write-side (P, 90% done; 剩 regime)            research/ 各 resolver → active_manifests_dir()
   │  manifests/<iv>/{<strat>/manifest.json, factor_<sym>.json, selection.json, regime_<sym>.json, ...}
   ▼
read 層  dashboard/server/artifacts.py          list_*/get_* 加 interval 參數，解析 base dir
   ▼
API      dashboard/server/main.py               ?interval= query + 新 /api/intervals
   ▼
前端     dashboard/web/src                       useInterval()(URL) + nav 選擇器 + Compare 複合鍵 + 空狀態
```

## 6. 受影響檔案

**後端 research**：無（P 已於 3652784 完成，regime 已 namespace）。
**後端 dashboard**：
- `dashboard/server/artifacts.py` — list_strategy_manifests / get_strategy_manifest / list_factor_manifests / get_factor_manifest / get_selection_manifest / get_regime_manifest 加 `interval`；新 `discover_intervals(repo_root)`。
- `dashboard/server/main.py` — `/api/strategies`、`/strategies/{id}`、`/factor-analysis`、`/selection`、`/regime`、`/pipeline` 加 `interval` query；新 `/api/intervals`。
**前端**：
- `dashboard/web/src/lib/api.ts` — 各 fetch 帶 `interval`；`api.intervals()`。
- `dashboard/web/src/hooks/useInterval.ts`（新）— 讀/寫 URL `?interval=`。
- `dashboard/web/src/components/layout/Layout.tsx` — nav 加 interval 選擇器（值來自 `/api/intervals`）。
- 各頁（Strategies/Factors/StrategyDetail/Pipeline）讀 `useInterval()` 串進 api 呼叫。
- `dashboard/web/src/pages/Compare.tsx` — 跨 interval 抓 + 複合鍵 + 徽章。
- 各頁空狀態。

## 7. 測試

- 後端 dashboard pytest：`artifacts` interval 解析（1H root / 15m 子目錄 / 不存在回空）、`discover_intervals`、API `?interval=` 預設+顯式、`/api/intervals`。
- 後端 research pytest：stage2_5_regime 走 active_manifests_dir。
- 前端 `dashboard/web` 無測試框架 → preview 工作流人工驗證（選擇器切換、Compare 跨 interval、空狀態）。
- 註：research / dashboard / agent pytest 分開跑。

## 8. 零 1H 回歸

artifacts `interval="1H"` 預設 → root（與現況逐字同）；前端預設 `?interval` 缺省當 1H；`/api/intervals` 至少含 1H。現有 1H dashboard 行為不變。

## 9. 風險

| 風險 | 緩解 |
|------|------|
| dashboard 與 research 各自解析 manifests 路徑、漂移 | 規則一行（root vs `/<iv>`）、兩邊文件註明同約定；artifacts 不跨套件 import |
| `/api/intervals` 掃到只有部分 stage 的 interval（半成品） | 空狀態處理；discover 以「有 manifest.json 或 evidence」為準 |
| URL param 在頁間遺失 | NavLink/連結一律帶 `?interval=`；`useInterval` 集中處理 |

## 10. 不在範圍

- pipeline job / dashboard 觸發 intraday 跑（需 job 帶 interval）——獨立小案，可後續。
- factor IC 熱圖、pipeline per-interval 狀態（brainstorm 時未選）。
- 純參數串穿版的 manifests 解析（沿用 env / 約定）。
