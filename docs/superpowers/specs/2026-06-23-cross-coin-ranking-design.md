# 跨幣策略排名（Cross-Coin Ranking）— 設計

- **日期**：2026-06-23
- **分支**：quant-trading-dashboard
- **狀態**：設計已核准，待寫實作計畫
- **脈絡**：P1.5（擴幣的後續消費端）。趁 6 新幣逐幣跑 pipeline 的空檔平行做；落地後正好用來比「哪幣×策略最該推 testnet」。純 dashboard 工作，不碰跑中的 research pipeline。

---

## 1. 背景與動機

擴幣後會有 9 幣 × 多 archetype 的策略候選。要快速看出「哪個可部署」需要一張**跨幣、以 OOS 為主、排好序**的表。

現況（已查證）：`dashboard/web/src/pages/Compare.tsx` 是 `/` 首頁（`router.tsx:14` `{ index: true, element: <Compare/> }`，nav 標「Strategies」）。它已經是跨幣表：

- 讀 `api.strategies(interval)` → 全幣策略
- 有 coin filter（ALL / 各幣）
- 欄：策略 / 幣種 / 級別 / Sharpe / Max DD / Stage / GO-NO-GO / 紅旗

**兩個硬傷**：

1. **指標是 in-sample**：`main.py::_strategy_row` 的 `sharpe`/`max_drawdown` 取自 `m.backtest.in_sample`（`main.py:59-60`）。in-sample 永遠好看；本專案鐵律是 **OOS（walk-forward held-out）才算數**。用 IS 排會誤導。
2. **沒排序/排名**：純列表，看不出「誰最該部署」。

**結論**：強化 Compare（首頁），不開新頁（DRY）。後端 row builder 補 OOS 欄位，前端加 OOS 欄 + 排名。

### 資料現成（無 schema 改）

- `BacktestBlock`（`schemas.py:368`）：`in_sample: BacktestMetrics`（必） + `oos: Optional[BacktestMetrics]`（walk-forward held-out）。
- `BacktestMetrics`：`sharpe` / `max_drawdown` / `trades` / `profit_factor`。
- `StrategyManifest.selected: bool`（`schemas.py:500`）。

`_strategy_row` 目前只吐 in_sample 的 sharpe/dd，沒吐 oos、trades、selected。本案就是把這些既有資料吐出來 + 前端排序。

---

## 2. 目標 / 非目標

### 目標
- 首頁跨幣表以 **OOS 為主指標**（sharpe/DD/trades），IS sharpe 當次要欄（看 train→OOS overfit gap）。
- 預設**排名**：`selected` 優先 → OOS sharpe 降序（null 沉底）→ `fatal` 沉底。
- 欄頭可點改排序。
- `selected` 徽章。
- 保留現有 coin filter / interval / 紅旗 / GO-NO-GO / Stage。

### 非目標（YAGNI）
- 新頁面（強化現有 Compare）。
- 後端排序（client-side 排即可，資料量小、且欄頭排序本就需 client）。
- 圖表化排名 / 散點圖。
- 跨 interval 邏輯改動（沿用現有 `?interval=` 含 `all` merge）。
- 改 `StrategyManifest` schema。

---

## 3. 設計

### 3.1 後端 `dashboard/server/main.py::_strategy_row`

加欄位（保留現有 `sharpe`/`max_drawdown` = in_sample，向後相容）：

```python
def _strategy_row(m, interval: str) -> dict:
    oos = m.backtest.oos if (m.backtest and m.backtest.oos) else None
    return {
        # ── 既有（不動）──
        "strategy_id": m.strategy_id,
        "symbol": m.symbol,
        "pipeline_stage": m.pipeline_stage,
        "generated_at": m.generated_at.isoformat(),
        "gate_pass": m.gate.overall_pass if m.gate else None,
        "gate_fatal": m.gate.fatal_fail if m.gate else None,
        "sharpe": m.backtest.in_sample.sharpe if m.backtest else None,            # IS（次要）
        "max_drawdown": m.backtest.in_sample.max_drawdown if m.backtest else None, # IS（次要）
        "red_flags": [f.value for f in m.gate.red_flags] if m.gate else [],
        "interval": interval,
        # ── 新增（OOS 為主 + selected）──
        "sharpe_oos": oos.sharpe if oos else None,
        "dd_oos": oos.max_drawdown if oos else None,
        "trades_oos": oos.trades if oos else None,
        "pf_oos": oos.profit_factor if oos else None,
        "selected": m.selected,
    }
```

### 3.2 前端 type `dashboard/web/src/lib/api.ts::StrategyRow`

加：`sharpe_oos: number | null`、`dd_oos: number | null`、`trades_oos: number | null`、`pf_oos: number | null`、`selected: boolean`。

### 3.3 排序 comparator（pure util）

放 `dashboard/web/src/lib/ranking.ts`（新檔，純函式好測）：

```ts
export type SortKey = "rank" | "sharpe_oos" | "dd_oos" | "trades_oos" | "sharpe" | "pipeline_stage";

// 預設「可部署性」排名：selected 頂 → OOS sharpe 降序(null 沉底) → fatal 沉底。
export function defaultRank(a: StrategyRow, b: StrategyRow): number { ... }

// 欄頭點選排序（單欄；null 一律沉底；數值欄預設降序、stage 升序）。
export function compareBy(key: SortKey, dir: "asc" | "desc"): (a, b) => number { ... }
```

排序規則細節：
- `defaultRank`：① `selected` true 在前；② `gate_fatal` true 沉底；③ `sharpe_oos` 降序，`null` 視為 -∞（沉底）。
- `compareBy`：選定欄位排序，`null` 永遠沉底（不論 asc/desc）。

### 3.4 前端 `Compare.tsx`

- `StrategyRow` 多欄渲染。表格欄序：
  `策略 │ 幣種 │ 級別 │ OOS Sharpe │ OOS DD │ OOS Trades │ IS Sharpe(灰,次) │ Stage │ Selected │ GO/NO-GO │ 紅旗`
- state `sortKey`（預設 `"rank"`）、`sortDir`。`rows` 經 `useMemo` 排序後渲染。
- 欄頭（OOS Sharpe / OOS DD / OOS Trades / IS Sharpe / Stage）可點：切該欄排序、再點切方向；顯示 ▲▼ 指示。
- Selected 欄：`selected` → 綠徽章「已選 ✓」；否則「—」。
- OOS 欄用既有 `MetricCell` + `fmtSharpe`/`fmtPct`；`null`（無 walk-forward）顯示「—」。
- 標題「策略比較」→「策略排名」。
- coin filter / PipelineStrip / interval 空狀態 / row click 進詳情：全部保留不動。

---

## 4. 測試

- **後端 pytest**（`dashboard/server/test_main.py` 或 `test_*`）：
  - `_strategy_row`：manifest 帶 `backtest.oos` → row 有 `sharpe_oos/dd_oos/trades_oos/pf_oos` 正確值 + `selected`。
  - manifest 無 `oos`（`backtest.oos is None`）→ 上述 OOS 欄皆 `None`，不報錯。
  - 無 `backtest` → 全 None 安全。
- **前端 ranking util**：若 `dashboard/web` 有 vitest（計畫階段確認）→ 單測 `defaultRank`（selected 置頂、null OOS 沉底、fatal 沉底）+ `compareBy`（null 永遠沉底）。無 vitest 則靠 tsc 型別 + preview 驗。
- **前端整合**：`tsc -b` + `npm run build` 綠。
- **preview 驗證**：起 dev server → 開首頁 → 截圖確認 OOS 欄出現、排名正確（selected 頂、OOS sharpe 降序）、欄頭排序可點、selected 徽章。

> 註：dashboard/server 與 research/tests 的 pytest 必須分開跑（[[backlog-pipeline-minor-cleanups]]）。

---

## 5. 成功標準

開 `/` 首頁 →

1. 跨幣策略表以 OOS sharpe/DD/trades 為主欄、IS sharpe 為次要欄。
2. 預設排序：selected 在頂、其餘 OOS sharpe 降序、fatal 沉底。
3. 欄頭可點改排序。
4. 無 walk-forward 的策略 OOS 欄顯示「—」並沉底，不報錯。
5. 既有 coin filter / interval / 紅旗 / row click 不退化。
6. backend pytest 綠、`tsc -b` + build 綠、preview 截圖佐證。

→ 6 新幣 pipeline 一跑完，首頁即可一眼挑出最該推 testnet 的幣×策略。

---

## 6. 已定決策

- 排名依據：`selected` 優先 → OOS sharpe 降序 → fatal 沉底（非 stage5 composite score；透明直觀，欄頭仍可改排）。
- IS/OOS：OOS 為主、IS sharpe 為次要欄（看 overfit gap）。
- 排序在 client-side（view 概念、資料量小、欄頭排序本需 client）。
- 強化 Compare 首頁，不開新頁。
