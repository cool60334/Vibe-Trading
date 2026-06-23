# Cross-Coin Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the `/` Compare page into an OOS-anchored deployability ranking: expose `backtest.oos` metrics in the strategy list and rank cross-coin strategies GO-first → OOS Sharpe desc, with sortable headers.

**Architecture:** Backend `_strategy_row` gains 4 OOS fields (no schema change). Frontend gets a pure `ranking.ts` comparator util + OOS columns + sortable headers in `Compare.tsx`. Ranking is client-side (small data, header-sort needs client anyway).

**Tech Stack:** FastAPI + pydantic (server), React 19 + TypeScript + Vite (web). Server tests = pytest; web has **no test runner** (verify via `tsc -b` + preview).

---

## Spec

`docs/superpowers/specs/2026-06-23-cross-coin-ranking-design.md`

## Conventions (read before starting)

- **`selected` is NOT on `StrategyManifest`** (it's on `SelectionEntry`/selection.json). Deployability = `gate.overall_pass` && !`gate.fatal_fail` (the promote-button gate). Rank on that, not `m.selected`.
- Run **server tests from `dashboard/server/`**: `cd dashboard/server && python -m pytest test_main.py -v` (the `client` fixture does `import main` — CWD must be on sys.path; run separately from research tests).
- Build **web from `dashboard/web/`**: `cd dashboard/web && npm run build` (= `tsc -b && vite build`).
- Commit trailer (every commit): `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

## File Structure

| File | Responsibility |
|---|---|
| `dashboard/server/main.py` (modify) | `_strategy_row` → add `sharpe_oos / dd_oos / trades_oos / pf_oos` |
| `dashboard/server/test_main.py` (modify) | test OOS fields present (with-oos) + null (no-oos) |
| `dashboard/web/src/lib/api.ts` (modify) | `StrategyRow` → add the 4 OOS fields |
| `dashboard/web/src/lib/ranking.ts` (create) | pure `SortKey`, `defaultRank`, `compareBy` |
| `dashboard/web/src/pages/Compare.tsx` (modify) | OOS columns, sortable headers, default rank, title |

---

### Task 1: Backend — expose OOS metrics in `_strategy_row`

**Files:**
- Modify: `dashboard/server/main.py:51-63`
- Test: `dashboard/server/test_main.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/server/test_main.py`:

```python
# ---------------------------------------------------------------------------
# GET /api/strategies — OOS columns (cross-coin ranking)
# ---------------------------------------------------------------------------

_PAYLOAD_WITH_OOS = {
    "schema_version": 1,
    "strategy_id": "strat_eth_oos",
    "symbol": "ETH",
    "generated_at": "2024-01-01T00:00:00Z",
    "pipeline_stage": 5,
    "spec": {
        "strategy_id": "strat_eth_oos",
        "symbol": "ETH",
        "spec_yaml": "runs/strat_eth_oos/config.yaml",
    },
    "backtest": {
        "in_sample": {"source_run": "strat_eth_oos_train", "sharpe": 1.4, "max_drawdown": 0.2},
        "oos": {
            "source_run": "strat_eth_oos_oos",
            "sharpe": 1.02, "max_drawdown": 0.09, "trades": 49, "profit_factor": 1.54,
        },
    },
}


def _client_for_payload(tmp_path, payload):
    d = tmp_path / "research" / "manifests" / payload["strategy_id"]
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    return TestClient(app)


def test_strategy_row_exposes_oos_metrics(tmp_path):
    with _client_for_payload(tmp_path, _PAYLOAD_WITH_OOS) as c:
        row = c.get("/api/strategies").json()[0]
    assert row["sharpe_oos"] == 1.02
    assert row["dd_oos"] == 0.09
    assert row["trades_oos"] == 49
    assert row["pf_oos"] == 1.54
    # in-sample sharpe stays as the secondary column
    assert row["sharpe"] == 1.4


def test_strategy_row_oos_null_when_no_walk_forward(tmp_path):
    payload = json.loads(json.dumps(_PAYLOAD_WITH_OOS))
    del payload["backtest"]["oos"]  # in_sample only, no held-out OOS yet
    with _client_for_payload(tmp_path, payload) as c:
        row = c.get("/api/strategies").json()[0]
    assert row["sharpe_oos"] is None
    assert row["dd_oos"] is None
    assert row["trades_oos"] is None
    assert row["pf_oos"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard/server && python -m pytest test_main.py -k oos -v`
Expected: FAIL — `KeyError: 'sharpe_oos'`

- [ ] **Step 3: Implement — add OOS fields**

In `dashboard/server/main.py`, replace `_strategy_row` (lines 51-63) with:

```python
def _strategy_row(m, interval: str) -> dict:
    oos = m.backtest.oos if (m.backtest and m.backtest.oos) else None
    return {
        "strategy_id": m.strategy_id,
        "symbol": m.symbol,
        "pipeline_stage": m.pipeline_stage,
        "generated_at": m.generated_at.isoformat(),
        "gate_pass": m.gate.overall_pass if m.gate else None,
        "gate_fatal": m.gate.fatal_fail if m.gate else None,
        "sharpe": m.backtest.in_sample.sharpe if m.backtest else None,
        "max_drawdown": m.backtest.in_sample.max_drawdown if m.backtest else None,
        "red_flags": [f.value for f in m.gate.red_flags] if m.gate else [],
        "interval": interval,
        # OOS (walk-forward held-out) — authoritative ranking metrics; null until an OOS run exists.
        "sharpe_oos": oos.sharpe if oos else None,
        "dd_oos": oos.max_drawdown if oos else None,
        "trades_oos": oos.trades if oos else None,
        "pf_oos": oos.profit_factor if oos else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard/server && python -m pytest test_main.py -k "oos or list_strategies" -v`
Expected: PASS (new OOS tests + existing list tests still green)

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/main.py dashboard/server/test_main.py
git commit -m "feat(dashboard): expose backtest.oos metrics in strategy list rows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Frontend — `StrategyRow` OOS fields + pure ranking util

**Files:**
- Modify: `dashboard/web/src/lib/api.ts:283-294`
- Create: `dashboard/web/src/lib/ranking.ts`

- [ ] **Step 1: Add the OOS fields to `StrategyRow`**

In `dashboard/web/src/lib/api.ts`, replace the `StrategyRow` interface (lines 283-294) with:

```ts
export interface StrategyRow {
  strategy_id: string;
  symbol: string;
  pipeline_stage: number;
  generated_at: string;
  gate_pass: boolean | null;
  gate_fatal: boolean | null;
  sharpe: number | null;        // in-sample (secondary)
  max_drawdown: number | null;  // in-sample (secondary)
  sharpe_oos: number | null;    // OOS / walk-forward (primary ranking metric)
  dd_oos: number | null;
  trades_oos: number | null;
  pf_oos: number | null;
  red_flags: RedFlagCode[];
  interval: string;
}
```

- [ ] **Step 2: Create the ranking util**

Create `dashboard/web/src/lib/ranking.ts`:

```ts
import type { StrategyRow } from "./api";

export type SortKey = "rank" | "sharpe_oos" | "dd_oos" | "trades_oos" | "sharpe" | "pipeline_stage";
export type SortDir = "asc" | "desc";

// Deployability tier: GO (gate pass & not fatal) = 0; non-fatal non-GO = 1; fatal = 2.
function deployTier(r: StrategyRow): number {
  if (r.gate_fatal === true) return 2;
  if (r.gate_pass === true) return 0;
  return 1;
}

// Compare two nullable numbers; null ALWAYS sinks regardless of direction.
function cmpNum(a: number | null, b: number | null, dir: SortDir): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return dir === "asc" ? a - b : b - a;
}

// Default "deployability" rank: GO first -> OOS Sharpe desc (null last) -> fatal bottom.
export function defaultRank(a: StrategyRow, b: StrategyRow): number {
  const tier = deployTier(a) - deployTier(b);
  if (tier !== 0) return tier;
  return cmpNum(a.sharpe_oos, b.sharpe_oos, "desc");
}

// Column-header sort. null always sinks. Returns a comparator.
export function compareBy(key: SortKey, dir: SortDir): (a: StrategyRow, b: StrategyRow) => number {
  if (key === "rank") return defaultRank;
  if (key === "pipeline_stage") {
    return (a, b) => (dir === "asc" ? a.pipeline_stage - b.pipeline_stage : b.pipeline_stage - a.pipeline_stage);
  }
  const get = (r: StrategyRow): number | null =>
    key === "sharpe_oos" ? r.sharpe_oos
    : key === "dd_oos" ? r.dd_oos
    : key === "trades_oos" ? r.trades_oos
    : r.sharpe;
  return (a, b) => cmpNum(get(a), get(b), dir);
}
```

- [ ] **Step 3: Type-check**

Run: `cd dashboard/web && npx tsc -b`
Expected: no errors (ranking.ts compiles against StrategyRow; unused import of compareBy is fine — it's exported).

- [ ] **Step 4: Commit**

```bash
git add dashboard/web/src/lib/api.ts dashboard/web/src/lib/ranking.ts
git commit -m "feat(web): StrategyRow OOS fields + pure ranking comparator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Frontend — OOS columns + sortable headers in `Compare.tsx`

**Files:**
- Modify: `dashboard/web/src/pages/Compare.tsx`

- [ ] **Step 1: Update imports + add sort state + sorted rows**

In `Compare.tsx`, change the React import and add the ranking import at the top:

```ts
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type StrategyRow, type RedFlagCode } from "../lib/api";
import { compareBy, type SortKey, type SortDir } from "../lib/ranking";
import { useInterval } from "@/hooks/useInterval";
import { cn } from "../lib/utils";
```

Inside `export default function Compare()`, after the existing `const [activeCoin, setActiveCoin] = useState<string>("ALL");` line, add:

```ts
  const [sortKey, setSortKey] = useState<SortKey>("rank");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "pipeline_stage" ? "asc" : "desc");
    }
  }
```

Then replace the `filtered` line:

```ts
  const filtered = activeCoin === "ALL" ? rows : rows.filter((r) => r.symbol === activeCoin);
```

with a filtered-then-sorted memo:

```ts
  const filtered = useMemo(() => {
    const base = activeCoin === "ALL" ? rows : rows.filter((r) => r.symbol === activeCoin);
    return [...base].sort(compareBy(sortKey, sortDir));
  }, [rows, activeCoin, sortKey, sortDir]);
```

- [ ] **Step 2: Add a sortable-header helper**

In `Compare.tsx`, above `export default function Compare()`, add:

```tsx
function SortableTh({
  label, sortKey: key, active, dir, onSort, align = "right",
}: {
  label: string;
  sortKey: SortKey;
  active: boolean;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: "right" | "center";
}) {
  return (
    <th
      className={cn("px-4 py-3 cursor-pointer select-none hover:text-foreground", align === "right" ? "text-right" : "text-center")}
      onClick={() => onSort(key)}
    >
      {label}
      <span className="ml-1 text-[10px]">{active ? (dir === "asc" ? "▲" : "▼") : "↕"}</span>
    </th>
  );
}
```

- [ ] **Step 3: Replace the table header row**

In `Compare.tsx`, replace the entire `<thead>...</thead>` block with:

```tsx
            <thead>
              <tr className="border-b bg-muted/50 text-xs text-muted-foreground uppercase tracking-wide">
                <th className="px-4 py-3 text-left">策略</th>
                <th className="px-4 py-3 text-left">幣種</th>
                <th className="px-4 py-3 text-left">級別</th>
                <SortableTh label="OOS Sharpe" sortKey="sharpe_oos" active={sortKey === "sharpe_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="OOS DD" sortKey="dd_oos" active={sortKey === "dd_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="OOS Trades" sortKey="trades_oos" active={sortKey === "trades_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="IS Sharpe" sortKey="sharpe" active={sortKey === "sharpe"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="Stage" sortKey="pipeline_stage" active={sortKey === "pipeline_stage"} dir={sortDir} onSort={toggleSort} align="center" />
                <th className="px-4 py-3 text-center">GO/NO-GO</th>
                <th className="px-4 py-3 text-left">紅旗</th>
              </tr>
            </thead>
```

- [ ] **Step 4: Replace the table body cells**

In `Compare.tsx`, replace the `<tbody>...</tbody>` block with (OOS primary, IS secondary/grey):

```tsx
            <tbody className="divide-y">
              {filtered.map((row) => (
                <tr
                  key={`${row.interval}_${row.strategy_id}`}
                  onClick={() => navigate(`/strategies/${row.strategy_id}?interval=${row.interval}`)}
                  className={cn(
                    "cursor-pointer transition-colors hover:bg-muted/40",
                    row.gate_fatal && "bg-red-50/50 dark:bg-red-950/20",
                    !row.gate_fatal && row.gate_pass === false && "bg-orange-50/40 dark:bg-orange-950/10",
                  )}
                >
                  <td className="px-4 py-3 font-mono font-medium text-foreground">{row.strategy_id}</td>
                  <td className="px-4 py-3 text-muted-foreground">{row.symbol}</td>
                  <td className="px-4 py-3">
                    <span className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">
                      {row.interval}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums font-medium">
                    <MetricCell value={row.sharpe_oos} formatter={fmtSharpe} />
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    <MetricCell value={row.dd_oos} formatter={fmtPct} />
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    <MetricCell value={row.trades_oos} formatter={(v) => String(Math.round(v))} />
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-muted-foreground">
                    <MetricCell value={row.sharpe} formatter={fmtSharpe} />
                  </td>
                  <td className="px-4 py-3 text-center text-muted-foreground">{row.pipeline_stage}</td>
                  <td className="px-4 py-3 text-center">
                    <GateBadge pass={row.gate_pass} fatal={row.gate_fatal} />
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {row.red_flags.map((f) => (
                        <RedFlagChip key={f} code={f} />
                      ))}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
```

- [ ] **Step 5: Update the page title**

In `Compare.tsx`, change the heading:

```tsx
        <h1 className="text-2xl font-semibold">策略排名</h1>
```

- [ ] **Step 6: Type-check + build**

Run: `cd dashboard/web && npm run build`
Expected: `tsc -b` clean + `vite build` succeeds (no unused-var / type errors).

- [ ] **Step 7: Commit**

```bash
git add dashboard/web/src/pages/Compare.tsx
git commit -m "feat(web): OOS-anchored ranking + sortable headers on strategy page

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Verify in the browser + wrap

**Files:** none (verification only)

- [ ] **Step 1: Start backend + web dev server**

The web dev server proxies `/api` to the FastAPI backend (see `dashboard/web/vite.config.ts`). Start the FastAPI server first (`cd dashboard/server && uvicorn main:app --port 8000`), then `preview_start` the vite dev server. (If the proxy target differs, set it / start accordingly.)

- [ ] **Step 2: Load `/` and confirm content**

`preview_eval: window.location.reload()` then `preview_snapshot`.
Expected: table shows OOS Sharpe / OOS DD / OOS Trades / IS Sharpe columns; rows present for local manifests (eth_s5 has OOS, others may show "—").

- [ ] **Step 3: Confirm ranking + sorting**

`preview_snapshot` — verify GO rows sit above NO-GO/fatal, and within GO, OOS Sharpe is descending. `preview_click` an OOS-column header, then `preview_snapshot` — order changes; arrow indicator flips on second click.

- [ ] **Step 4: Screenshot proof**

`preview_screenshot` of `/` showing the ranked table. Share with the user.

- [ ] **Step 5: Console check**

`preview_console_logs` — no React errors/warnings from the new code.

---

## Self-Review

**Spec coverage:**
- Backend OOS fields (sharpe_oos/dd_oos/trades_oos/pf_oos), no schema change → Task 1 ✓
- StrategyRow type → Task 2 ✓
- Ranking comparator (default GO-first → OOS sharpe desc → fatal sink; null sink) → Task 2 (ranking.ts) ✓
- OOS columns + IS secondary + sortable headers + title → Task 3 ✓
- GO/NO-GO badge conveys deployability, no separate Selected column → Task 3 (kept existing GateBadge) ✓
- Verify: backend pytest, tsc/build, preview → Tasks 1, 2, 3, 4 ✓
- No new page, no schema change, no vitest added → honored ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; run steps show command + expected. ✓

**Type consistency:** `StrategyRow` fields (`sharpe_oos/dd_oos/trades_oos/pf_oos`) identical across Task 1 (backend keys), Task 2 (TS type + ranking.ts getters), Task 3 (cell usage). `SortKey`/`SortDir`/`defaultRank`/`compareBy` names consistent between ranking.ts (Task 2) and Compare.tsx (Task 3). `compareBy(sortKey, sortDir)` signature matches call site. `SortableTh` props match usage in the header block. ✓
