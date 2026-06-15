# Dashboard View-by-Timeframe + Cross-Interval Compare — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the dashboard view strategies/factors by interval (1H/15m/30m) via a global selector, with an "All" mode that compares strategies across intervals, plus graceful empty states.

**Architecture:** Backend `artifacts.py` resolves a per-interval manifests dir (`research/manifests` for 1H, `research/manifests/<iv>/` for sub-hour); API endpoints gain `?interval=` (plus an "all" merge that tags each row with its interval) and a new `/api/intervals` that discovers which intervals have data. The frontend keeps the active interval in the URL (`?interval=`) via a `useInterval()` hook, a nav selector drives it, and pages thread it into API calls. Cross-interval compare = the strategies page in `interval=all` mode (interval column + composite React keys). Write-side namespacing (Prerequisite P) is already complete.

**Tech Stack:** Backend: FastAPI + pydantic, pytest (run from `dashboard/server/`). Frontend: React + TypeScript + Vite + react-router + Tailwind (no unit-test harness → verify via the preview workflow).

**Scope:** Two phases. Phase A (backend, TDD) is shippable on its own (API serves per-interval data). Phase B (frontend) consumes it. Out of scope: `/api/pipeline/status` page interval-awareness, cross-interval *factor* merge (factors use a single interval), pipeline-job interval triggering. See spec: `docs/superpowers/specs/2026-06-15-dashboard-interval-view-design.md`.

**Design refinement vs spec:** the global selector includes an **"All"** option which *is* the cross-interval compare mode (no separate toggle). Every strategy row gains an `interval` field so the frontend can badge/column it and build composite keys.

---

## File Structure

**Phase A — backend (`dashboard/server/`)**
- `artifacts.py` — `_manifests_base(repo_root, interval)`, `discover_intervals(repo_root)`; all `list_*/get_*` gain `interval="1H"`.
- `main.py` — `?interval=` on strategies/strategy/factor-analysis/selection/regime; new `/api/intervals`; strategy rows tagged with `interval`; `interval=all` merge.
- `test_interval.py` (new) — artifacts resolution, discovery, API wiring.

**Phase B — frontend (`dashboard/web/src/`)**
- `hooks/useInterval.ts` (new) — read/write `?interval=` (URL = single source of truth).
- `components/layout/IntervalSelector.tsx` (new) — pill selector fed by `/api/intervals`.
- `components/layout/Layout.tsx` — mount the selector in the header.
- `lib/api.ts` — `interval` param on `strategies/strategy/factorAnalysis/selection/regime`; `intervals()`; `StrategyRow.interval`.
- `pages/Compare.tsx` — thread interval, interval column, composite keys, row→detail passes interval, interval-aware empty state.
- `pages/StrategyDetail.tsx` — read own `?interval=`, pass to `api.strategy`.
- `pages/FactorReport.tsx` — thread interval (single; treat "all" as 1H).

**Zero 1H regression:** every `interval` defaults to `"1H"` → bare root, identical to today. The URL defaults to 1H when absent.

---

# Phase A — Backend (TDD, pytest from `dashboard/server/`)

## Task 1: artifacts — per-interval dir resolution

**Files:**
- Modify: `dashboard/server/artifacts.py`
- Test: `dashboard/server/test_interval.py`

- [ ] **Step 1: Write the failing test**

```python
# dashboard/server/test_interval.py
"""Interval-aware artifacts + API — run from dashboard/server/ (pytest test_interval.py)."""
import json
from pathlib import Path

import artifacts


def _write_manifest(base: Path, strategy_id: str) -> None:
    d = base / strategy_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "strategy_id": strategy_id, "symbol": "ETH-USDT-SWAP",
        "generated_at": "2026-06-15T00:00:00+00:00", "pipeline_stage": 5,
        "spec": {"source_run": None, "strategy_id": strategy_id, "symbol": "ETH-USDT-SWAP",
                 "spec_yaml": "x.yaml", "description": None},
        "generation": None, "reproducibility": None, "backtest": None,
        "optimization": None, "diagnosis": None, "gate": None,
    }), encoding="utf-8")


def test_manifests_base_1H_is_root():
    root = Path("/tmp/repo")
    assert artifacts._manifests_base(root, "1H") == root / "research" / "manifests"


def test_manifests_base_subhour_is_namespaced():
    root = Path("/tmp/repo")
    assert artifacts._manifests_base(root, "30m") == root / "research" / "manifests" / "30m"


def test_list_strategy_manifests_reads_namespaced(tmp_path):
    _write_manifest(tmp_path / "research" / "manifests", "eth_1h")
    _write_manifest(tmp_path / "research" / "manifests" / "30m", "eth_30m")
    root_list = artifacts.list_strategy_manifests(tmp_path, "1H")
    sub_list = artifacts.list_strategy_manifests(tmp_path, "30m")
    assert [m.strategy_id for m in root_list] == ["eth_1h"]
    assert [m.strategy_id for m in sub_list] == ["eth_30m"]


def test_list_strategy_manifests_missing_interval_returns_empty(tmp_path):
    (tmp_path / "research" / "manifests").mkdir(parents=True)
    assert artifacts.list_strategy_manifests(tmp_path, "15m") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `dashboard/server/`): `python -m pytest test_interval.py -v`
Expected: FAIL — `AttributeError: module 'artifacts' has no attribute '_manifests_base'` / `list_strategy_manifests()` takes 1 positional arg.

- [ ] **Step 3: Write minimal implementation**

In `dashboard/server/artifacts.py`, add the resolver after the imports:

```python
def _manifests_base(repo_root: Path, interval: str = "1H") -> Path:
    """Manifests dir for an interval: research/manifests for 1H, .../manifests/<iv> for sub-hour."""
    base = repo_root / "research" / "manifests"
    return base if interval == "1H" else base / interval
```

Change `list_strategy_manifests` and `get_strategy_manifest` to take `interval` and use the resolver:

```python
def list_strategy_manifests(repo_root: Path, interval: str = "1H") -> list[StrategyManifest]:
    base = _manifests_base(repo_root, interval)
    manifests: list[StrategyManifest] = []
    if not base.is_dir():
        return manifests
    for path in sorted(base.glob("*/manifest.json")):
        raw = _load_json(path)
        if raw is not None:
            try:
                manifests.append(StrategyManifest.model_validate(raw))
            except Exception:
                pass
    return manifests


def get_strategy_manifest(repo_root: Path, strategy_id: str, interval: str = "1H") -> Optional[StrategyManifest]:
    path = _manifests_base(repo_root, interval) / strategy_id / "manifest.json"
    raw = _load_json(path)
    if raw is None:
        return None
    try:
        return StrategyManifest.model_validate(raw)
    except Exception:
        return None
```

Apply the same `interval="1H"` + `_manifests_base(repo_root, interval)` change to `list_factor_manifests`, `get_factor_manifest`, `get_selection_manifest`, `get_regime_manifest` (replace each `base = repo_root / "research" / "manifests"` / hardcoded path prefix with `_manifests_base(repo_root, interval)`).

- [ ] **Step 4: Run test to verify it passes**

Run (from `dashboard/server/`): `python -m pytest test_interval.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/artifacts.py dashboard/server/test_interval.py
git commit -m "feat(dashboard): artifacts resolve per-interval manifests dir"
```

---

## Task 2: artifacts — `discover_intervals`

**Files:**
- Modify: `dashboard/server/artifacts.py`
- Test: `dashboard/server/test_interval.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `dashboard/server/test_interval.py`:

```python
def test_discover_intervals_root_only(tmp_path):
    _write_manifest(tmp_path / "research" / "manifests", "eth_1h")
    assert artifacts.discover_intervals(tmp_path) == ["1H"]


def test_discover_intervals_includes_subhour(tmp_path):
    _write_manifest(tmp_path / "research" / "manifests", "eth_1h")
    _write_manifest(tmp_path / "research" / "manifests" / "30m", "eth_30m")
    _write_manifest(tmp_path / "research" / "manifests" / "15m", "eth_15m")
    assert artifacts.discover_intervals(tmp_path) == ["1H", "15m", "30m"]


def test_discover_intervals_empty_defaults_1H(tmp_path):
    assert artifacts.discover_intervals(tmp_path) == ["1H"]
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `dashboard/server/`): `python -m pytest test_interval.py -k discover -v`
Expected: FAIL — `module 'artifacts' has no attribute 'discover_intervals'`.

- [ ] **Step 3: Write minimal implementation**

Add to `dashboard/server/artifacts.py`:

```python
import re

_INTERVAL_DIR_RE = re.compile(r"^\d+[mH]$")  # 15m, 30m, 4H, ...


def _dir_has_manifests(d: Path) -> bool:
    return any(d.glob("*/manifest.json")) or any(d.glob("factor_*.json"))


def discover_intervals(repo_root: Path) -> list[str]:
    """Intervals with data: '1H' if the manifests root has manifests, plus any
    sub-hour subdir (name like 15m/30m) that has manifests. Sorted, 1H first.
    Always returns at least ['1H'] so the selector is never empty.
    """
    base = repo_root / "research" / "manifests"
    out: list[str] = []
    if base.is_dir():
        if _dir_has_manifests(base):
            out.append("1H")
        subs = sorted(
            d.name for d in base.iterdir()
            if d.is_dir() and _INTERVAL_DIR_RE.match(d.name) and _dir_has_manifests(d)
        )
        out.extend(subs)
    return out or ["1H"]
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `dashboard/server/`): `python -m pytest test_interval.py -k discover -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/artifacts.py dashboard/server/test_interval.py
git commit -m "feat(dashboard): discover_intervals scans manifests subdirs"
```

---

## Task 3: API — `?interval=`, interval-tagged rows, `interval=all` merge, `/api/intervals`

**Files:**
- Modify: `dashboard/server/main.py`
- Test: `dashboard/server/test_interval.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `dashboard/server/test_interval.py`:

```python
from fastapi.testclient import TestClient

import main


def test_api_strategies_passes_interval(monkeypatch):
    seen = {}

    def fake_list(repo_root, interval="1H"):
        seen["interval"] = interval
        return []

    monkeypatch.setattr(main.artifacts, "list_strategy_manifests", fake_list)
    client = TestClient(main.app)
    client.get("/api/strategies?interval=15m")
    assert seen["interval"] == "15m"


def test_api_strategies_all_merges_and_tags(monkeypatch):
    monkeypatch.setattr(main.artifacts, "discover_intervals", lambda r: ["1H", "30m"])

    class M:
        def __init__(self, sid):
            self.strategy_id = sid
            self.symbol = "ETH-USDT-SWAP"
            self.pipeline_stage = 5
            from datetime import datetime, timezone
            self.generated_at = datetime(2026, 6, 15, tzinfo=timezone.utc)
            self.gate = None
            self.backtest = None

    monkeypatch.setattr(main.artifacts, "list_strategy_manifests",
                        lambda r, interval="1H": [M(f"s_{interval}")])
    client = TestClient(main.app)
    rows = client.get("/api/strategies?interval=all").json()
    by_iv = {r["interval"]: r["strategy_id"] for r in rows}
    assert by_iv == {"1H": "s_1H", "30m": "s_30m"}


def test_api_intervals_endpoint(monkeypatch):
    monkeypatch.setattr(main.artifacts, "discover_intervals", lambda r: ["1H", "15m"])
    client = TestClient(main.app)
    assert client.get("/api/intervals").json() == ["1H", "15m"]
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `dashboard/server/`): `python -m pytest test_interval.py -k api -v`
Expected: FAIL — endpoints ignore `interval` / no `/api/intervals` / rows lack `interval`.

- [ ] **Step 3: Write minimal implementation**

In `dashboard/server/main.py`, replace `list_strategies` (51-67) with an interval-aware, tagging version + add a row builder:

```python
def _strategy_row(m, interval: str) -> dict:
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
    }


@app.get("/api/strategies")
def list_strategies(interval: str = Query("1H")) -> list[dict]:
    if interval == "all":
        rows: list[dict] = []
        for iv in artifacts.discover_intervals(REPO_ROOT):
            rows.extend(_strategy_row(m, iv) for m in artifacts.list_strategy_manifests(REPO_ROOT, iv))
        return rows
    return [_strategy_row(m, interval) for m in artifacts.list_strategy_manifests(REPO_ROOT, interval)]


@app.get("/api/intervals")
def list_intervals() -> list[str]:
    return artifacts.discover_intervals(REPO_ROOT)
```

Add `interval` query to the other endpoints:

```python
@app.get("/api/strategies/{strategy_id}")
def get_strategy(strategy_id: str, interval: str = Query("1H")) -> StrategyManifest:
    manifest = artifacts.get_strategy_manifest(REPO_ROOT, strategy_id, interval)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
    return manifest


@app.get("/api/factor-analysis")
def get_factor_analysis(interval: str = Query("1H")) -> list[FactorManifest]:
    iv = "1H" if interval == "all" else interval
    return artifacts.list_factor_manifests(REPO_ROOT, iv)


@app.get("/api/regime")
def get_regime(symbol: str = Query(...), interval: str = Query("1H")) -> dict[str, Any]:
    data = artifacts.get_regime_manifest(REPO_ROOT, symbol, "1H" if interval == "all" else interval)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Regime manifest for '{symbol}' not found")
    return data


@app.get("/api/selection")
def get_selection(interval: str = Query("1H")) -> SelectionManifest:
    manifest = artifacts.get_selection_manifest(REPO_ROOT, "1H" if interval == "all" else interval)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Selection manifest not found")
    return manifest
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `dashboard/server/`): `python -m pytest test_interval.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Run existing dashboard suite for regression**

Run (from `dashboard/server/`): `python -m pytest . -q`
Expected: PASS (default `interval="1H"` keeps every existing endpoint identical).

- [ ] **Step 6: Commit**

```bash
git add dashboard/server/main.py dashboard/server/test_interval.py
git commit -m "feat(dashboard): API interval query + /api/intervals + all-merge"
```

---

# Phase B — Frontend (exact code + preview verification; no unit-test harness)

> Verify each task with the preview workflow: `preview_start`, then `preview_snapshot` / `preview_click` / `preview_screenshot`. Reload after edits if HMR doesn't pick up.

## Task 4: `useInterval` hook + api.ts interval params

**Files:**
- Create: `dashboard/web/src/hooks/useInterval.ts`
- Modify: `dashboard/web/src/lib/api.ts`

- [ ] **Step 1: Create the hook**

```typescript
// dashboard/web/src/hooks/useInterval.ts
import { useSearchParams } from "react-router-dom";

// URL is the single source of truth for the active interval (no localStorage/Context).
const VALID = ["all", "1H", "15m", "30m"];

export function useInterval(): [string, (iv: string) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("interval");
  const interval = raw && VALID.includes(raw) ? raw : "1H";
  const setIv = (iv: string) => {
    const next = new URLSearchParams(params);
    next.set("interval", iv);
    setParams(next); // pushes ?interval= into the URL
  };
  return [interval, setIv];
}
```

- [ ] **Step 2: Add interval to the api client**

In `dashboard/web/src/lib/api.ts`, add `interval` to `StrategyRow`:

```typescript
export interface StrategyRow {
  strategy_id: string;
  symbol: string;
  pipeline_stage: number;
  generated_at: string;
  gate_pass: boolean | null;
  gate_fatal: boolean | null;
  sharpe: number | null;
  max_drawdown: number | null;
  red_flags: RedFlagCode[];
  interval: string;
}
```

Replace the affected client methods in the `api` object:

```typescript
  strategies: (interval = "1H"): Promise<StrategyRow[]> => get(`/strategies?interval=${interval}`),
  strategy: (id: string, interval = "1H"): Promise<StrategyManifest> =>
    get(`/strategies/${id}?interval=${interval}`),
  factorAnalysis: (interval = "1H"): Promise<FactorManifest[]> =>
    get(`/factor-analysis?interval=${interval}`),
  selection: (interval = "1H"): Promise<SelectionManifest> => get(`/selection?interval=${interval}`),
  regime: (symbol: string, interval = "1H"): Promise<Record<string, unknown>> =>
    get(`/regime?symbol=${symbol}&interval=${interval}`),
  intervals: (): Promise<string[]> => get("/intervals"),
```

- [ ] **Step 3: Verify it builds**

Run (from `dashboard/web/`): `npx tsc --noEmit`
Expected: no type errors.

- [ ] **Step 4: Commit**

```bash
git add dashboard/web/src/hooks/useInterval.ts dashboard/web/src/lib/api.ts
git commit -m "feat(web): useInterval hook (URL source of truth) + api interval params"
```

---

## Task 5: Interval selector in the nav

**Files:**
- Create: `dashboard/web/src/components/layout/IntervalSelector.tsx`
- Modify: `dashboard/web/src/components/layout/Layout.tsx`

- [ ] **Step 1: Create the selector**

```tsx
// dashboard/web/src/components/layout/IntervalSelector.tsx
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useInterval } from "@/hooks/useInterval";
import { cn } from "@/lib/utils";

const LABELS: Record<string, string> = { all: "全部", "1H": "1H", "15m": "15m", "30m": "30m" };

export function IntervalSelector() {
  const [interval, setIv] = useInterval();
  const [available, setAvailable] = useState<string[]>(["1H"]);

  useEffect(() => {
    api.intervals().then(setAvailable).catch(() => setAvailable(["1H"]));
  }, []);

  // "All" is offered only when more than one interval has data.
  const options = available.length > 1 ? ["all", ...available] : available;

  return (
    <div className="flex items-center gap-1 ml-2">
      <span className="text-xs text-muted-foreground mr-1">時間級別</span>
      {options.map((iv) => (
        <button
          key={iv}
          onClick={() => setIv(iv)}
          className={cn(
            "rounded-md px-2 py-1 text-xs font-medium transition-colors",
            interval === iv
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:bg-muted/80",
          )}
        >
          {LABELS[iv] ?? iv}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Mount it in the header**

In `dashboard/web/src/components/layout/Layout.tsx`, import and place it after the `<nav>` block (before the dark-mode button). Add the import:

```tsx
import { IntervalSelector } from "./IntervalSelector";
```

Insert immediately after the closing `</nav>` (line 35):

```tsx
        <IntervalSelector />
```

- [ ] **Step 3: Verify in preview**

`preview_start`; `preview_snapshot` — confirm the 時間級別 pills render in the header. If 15m/30m data exists, confirm an "全部" pill appears; clicking a pill updates the URL to `?interval=...`.

- [ ] **Step 4: Commit**

```bash
git add dashboard/web/src/components/layout/IntervalSelector.tsx dashboard/web/src/components/layout/Layout.tsx
git commit -m "feat(web): global interval selector in nav header"
```

---

## Task 6: Compare page — interval column, cross-interval, empty state

**Files:**
- Modify: `dashboard/web/src/pages/Compare.tsx`

- [ ] **Step 1: Thread interval into the fetch**

Replace the imports + the data-loading effect. Add at the top:

```tsx
import { useInterval } from "@/hooks/useInterval";
```

Replace the `useEffect` (130-141) so it refetches when the interval changes:

```tsx
  const [interval] = useInterval();

  useEffect(() => {
    setLoading(true);
    api
      .strategies(interval)
      .then((data) => {
        setRows(data);
        setLoading(false);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }, [interval]);
```

- [ ] **Step 2: Add the interval column + composite key + interval-aware navigation**

In the table header (`<thead>`), add a column after 幣種:

```tsx
                <th className="px-4 py-3 text-left">級別</th>
```

In the row `<tr>`, change the React `key` to a composite key and the navigation to carry the row's interval:

```tsx
                <tr
                  key={`${row.interval}_${row.strategy_id}`}
                  onClick={() => navigate(`/strategies/${row.strategy_id}?interval=${row.interval}`)}
```

Add the interval-badge cell right after the 幣種 cell (`<td ...>{row.symbol}</td>`):

```tsx
                  <td className="px-4 py-3">
                    <span className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">
                      {row.interval}
                    </span>
                  </td>
```

Also update the `PipelineStrip` row key (line 19) to the composite key to avoid collisions in "All" mode:

```tsx
          <div key={`${r.interval}_${r.strategy_id}`} className="flex items-center gap-2 text-xs">
```

- [ ] **Step 3: Interval-aware empty state**

Replace the empty-state block (191-194) so it names the interval:

```tsx
      {filtered.length === 0 ? (
        <div className="rounded-lg border p-8 text-center text-sm text-muted-foreground">
          {interval === "all" ? "尚無任何策略" : `尚無 ${interval} 策略 — 該時間級別的 pipeline 可能還沒跑或進行中`}
        </div>
      ) : (
```

- [ ] **Step 4: Verify in preview**

`preview_start`; with the selector on `1H`, `preview_snapshot` — table shows a 級別 column = 1H, existing behaviour intact. Switch to `15m` (or `全部`): `preview_screenshot` — rows reflect the chosen interval; "All" shows mixed-interval rows each badged. Select an interval with no data → the friendly empty state appears. Click a row → URL is `/strategies/<id>?interval=<iv>`.

- [ ] **Step 5: Commit**

```bash
git add dashboard/web/src/pages/Compare.tsx
git commit -m "feat(web): strategies page interval column + cross-interval (all) + empty state"
```

---

## Task 7: StrategyDetail + FactorReport thread interval

**Files:**
- Modify: `dashboard/web/src/pages/StrategyDetail.tsx`
- Modify: `dashboard/web/src/pages/FactorReport.tsx`

- [ ] **Step 1: StrategyDetail reads its own `?interval=`**

In `dashboard/web/src/pages/StrategyDetail.tsx`, add the import and read the param, then pass it to `api.strategy`. Add:

```tsx
import { useSearchParams } from "react-router-dom";
```

Where the component reads the route id and fetches, derive interval from the query and pass it:

```tsx
  const [params] = useSearchParams();
  const interval = params.get("interval") || "1H";
  // ... in the effect that loads the manifest:
  //   api.strategy(id, interval).then(...)
```

(Find the existing `api.strategy(id)` call and change it to `api.strategy(id, interval)`; add `interval` to that effect's dependency array.)

- [ ] **Step 2: FactorReport threads the selected interval**

In `dashboard/web/src/pages/FactorReport.tsx`, add the import and use the interval (factors use a single interval; "all" falls back to 1H):

```tsx
import { useInterval } from "@/hooks/useInterval";
```

```tsx
  const [interval] = useInterval();
  // in the load effect:
  //   api.factorAnalysis(interval === "all" ? "1H" : interval).then(...)
  // add `interval` to the effect's dependency array.
```

Add an interval label to the page header so the user knows which timeframe's factors are shown (place near the existing page title):

```tsx
      <span className="text-sm text-muted-foreground">時間級別：{interval === "all" ? "1H" : interval}</span>
```

- [ ] **Step 3: Verify build + preview**

Run (from `dashboard/web/`): `npx tsc --noEmit` → no errors.
`preview_start`; navigate Factors with the selector on `15m` → `preview_snapshot` shows the 15m factor tables (or empty); from the strategies page click a 15m row → StrategyDetail URL carries `?interval=15m` and loads that interval's manifest.

- [ ] **Step 4: Commit**

```bash
git add dashboard/web/src/pages/StrategyDetail.tsx dashboard/web/src/pages/FactorReport.tsx
git commit -m "feat(web): StrategyDetail + Factors respect selected interval"
```

---

## Task 8: End-to-end preview verification

**Files:** none (verification).

- [ ] **Step 1: Backend regression**

Run (from `dashboard/server/`): `python -m pytest . -q` → all green (Phase A + existing).

- [ ] **Step 2: Frontend build**

Run (from `dashboard/web/`): `npx tsc --noEmit` → no errors.

- [ ] **Step 3: Full preview walkthrough**

`preview_start`. With 1H selected (default): strategies table = current behaviour + 級別 column. Switch to 15m/30m/全部: rows update; "全部" mixes intervals each badged; row click carries interval to detail; Factors reflects interval; an empty interval shows the friendly message. `preview_screenshot` the 全部 view as proof.

- [ ] **Step 4: Record outcome** in the PR description (screenshots + which intervals had data at test time).

---

## Self-Review Notes

- **Spec coverage:** §4.1 global selector → Task 5; §4.2 URL source of truth + useInterval → Task 4; §4.3 `?interval=` + `/api/intervals` → Task 3; §4.4 artifacts interval + missing→empty → Tasks 1-2; §4.5 cross-interval compare composite keys → Task 6 (via "All"); §4.6 empty states → Task 6; §4.7 hybrid root=1H → Task 1 (`_manifests_base`). Regime endpoint interval → Task 3.
- **Refinement noted:** "All" added to the selector as the cross-interval mode; `StrategyRow.interval` added end-to-end (artifacts dir → API tag → row → badge/key → detail link).
- **Type consistency:** `interval: str = "1H"` across artifacts + API; `useInterval()` returns `[string, (iv:string)=>void]`; `StrategyRow.interval` consumed in Compare key/column/nav; `api.strategy(id, interval)` matches StrategyDetail call.
- **Zero 1H regression:** every interval defaults to "1H" → root; URL absent → 1H; verified by `test_list_strategy_manifests_*` (1H = root) and the existing dashboard suite (Task 3 step 5).
- **Out of scope (unchanged):** `/api/pipeline` + `/api/pipeline/status` (Pipeline page) stay 1H-root; cross-interval factor merge; pipeline-job interval triggering.
