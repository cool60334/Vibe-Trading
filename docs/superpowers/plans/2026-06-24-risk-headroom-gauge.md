# Risk Headroom Gauge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a per-trader "distance-to-kill" gauge to the Testnet page: plot the current drawdown against the kill-switch pause/terminate thresholds, consuming `live.max_drawdown` directly (the authoritative current DD the kill-switch triggers on).

**Architecture:** A pure `risk.ts` util + a `RiskGauge` component rendered inside the existing `KillSwitchPanel`. Frontend-only; no schema/backend change; no card refactor — all data is already on `status.live` + `status.killswitch`.

**Tech Stack:** React 19 + TypeScript + Vite + Tailwind. **No test runner** in `dashboard/web` (scripts = dev/build/preview) → verify via `tsc -b` + preview screenshots (do NOT add vitest).

## Spec
`docs/superpowers/specs/2026-06-24-risk-headroom-gauge-design.md`

## Conventions
- **`live.max_drawdown` IS the current DD** (positive fraction, from the persisted peak; misnomer) — verified at `dashboard/trader/killswitch.py:87` (`current_drawdown`) + `dashboard/trader/loop.py:277`. It equals exactly what the kill-switch `check()` evaluates. Use it directly; never recompute DD from the (truncated) equity tail.
- Build web from `dashboard/web/`: `npx tsc -b` (type-check), `npm run build` (full).
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

## File Structure
| File | Responsibility |
|---|---|
| `dashboard/web/src/lib/risk.ts` (create) | pure `RiskZone`, `RiskHeadroom`, `riskHeadroom()` |
| `dashboard/web/src/pages/Testnet.tsx` (modify) | `RiskGauge` component + wire into `KillSwitchPanel` + pass `currentDd` at the call site |

---

### Task 1: pure `risk.ts` util

**Files:**
- Create: `dashboard/web/src/lib/risk.ts`

- [ ] **Step 1: Create the util**

Create `dashboard/web/src/lib/risk.ts`:

```ts
export type RiskZone = "safe" | "caution" | "danger";

export interface RiskHeadroom {
  zone: RiskZone;
  currentPct: number;     // current DD as % (positive)
  pausePct: number;
  terminatePct: number;
  toPause: number;        // pause - current (fraction; negative once past)
  toTerminate: number;    // terminate - current
  fillFraction: number;   // current / terminate, clamped 0..1 (gauge fill)
}

const clamp01 = (x: number): number => Math.max(0, Math.min(1, x));

/**
 * Risk headroom of a live trader. All inputs are positive drawdown fractions
 * (e.g. 0.04 / 0.05 / 0.07). `currentDd` must be the authoritative current DD
 * (status.live.max_drawdown) — never recomputed from a truncated equity window.
 */
export function riskHeadroom(
  currentDd: number,
  pauseDd: number,
  terminateDd: number,
): RiskHeadroom {
  const zone: RiskZone =
    currentDd >= terminateDd ? "danger" : currentDd >= pauseDd ? "caution" : "safe";
  return {
    zone,
    currentPct: currentDd * 100,
    pausePct: pauseDd * 100,
    terminatePct: terminateDd * 100,
    toPause: pauseDd - currentDd,
    toTerminate: terminateDd - currentDd,
    fillFraction: terminateDd > 0 ? clamp01(currentDd / terminateDd) : 0,
  };
}
```

- [ ] **Step 2: Type-check**

Run: `cd dashboard/web && npx tsc -b`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard/web/src/lib/risk.ts
git commit -m "feat(web): pure riskHeadroom util (distance-to-kill)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `RiskGauge` component + wire into `KillSwitchPanel`

**Files:**
- Modify: `dashboard/web/src/pages/Testnet.tsx`

- [ ] **Step 1: Import the util**

In `Testnet.tsx`, after the `import { cn } from "../lib/utils";` line (line 17), add:

```ts
import { riskHeadroom } from "../lib/risk";
```

- [ ] **Step 2: Add the `RiskGauge` component**

In `Testnet.tsx`, insert this component immediately ABOVE `function KillSwitchPanel(` (line 312):

```tsx
function RiskGauge({
  currentDd, pauseDd, terminateDd, triggered,
}: {
  currentDd: number | null;
  pauseDd: number;
  terminateDd: number;
  triggered: boolean;
}) {
  if (currentDd === null) {
    return <p className="text-xs text-muted-foreground">風險餘裕：等待 equity 資料</p>;
  }
  const h = riskHeadroom(currentDd, pauseDd, terminateDd);
  const pausePos = terminateDd > 0 ? Math.min(100, (pauseDd / terminateDd) * 100) : 0;
  const markerPos = h.fillFraction * 100;
  const over = h.toTerminate <= 0;
  const zoneColor =
    triggered || h.zone === "danger" ? "bg-red-500"
    : h.zone === "caution" ? "bg-amber-500"
    : "bg-emerald-500";
  const fmt = (v: number) => `${(v * 100).toFixed(2)}%`;

  return (
    <div className="space-y-1.5">
      <div className="relative h-3 rounded-full overflow-hidden bg-muted">
        {/* safe zone (green) up to pause */}
        <div
          className="absolute inset-y-0 left-0 bg-emerald-200 dark:bg-emerald-900/40"
          style={{ width: `${pausePos}%` }}
        />
        {/* caution zone (amber) pause..terminate */}
        <div
          className="absolute inset-y-0 bg-amber-200 dark:bg-amber-900/40"
          style={{ left: `${pausePos}%`, right: 0 }}
        />
        {/* pause threshold line */}
        <div className="absolute inset-y-0 w-px bg-amber-600" style={{ left: `${pausePos}%` }} />
        {/* current DD marker */}
        <div
          className={cn("absolute inset-y-0 w-1.5 rounded", zoneColor)}
          style={{ left: `${markerPos}%`, transform: "translateX(-50%)" }}
        />
      </div>
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-muted-foreground">
          當前 DD <span className="font-semibold text-foreground tabular-nums">{fmt(currentDd)}</span>
          {" / 終止 "}
          <span className="tabular-nums">{fmt(terminateDd)}</span>
        </span>
        <span className={cn("tabular-nums", over ? "text-red-600 font-semibold dark:text-red-400" : "text-muted-foreground")}>
          {over
            ? `已逾終止 ${fmt(-h.toTerminate)}`
            : `距暫停 ${fmt(Math.max(0, h.toPause))} · 距終止 ${fmt(h.toTerminate)}`}
        </span>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Wire `RiskGauge` into `KillSwitchPanel`**

In `Testnet.tsx`, replace the `KillSwitchPanel` signature + opening (lines 312-314):

```tsx
function KillSwitchPanel({ ks }: { ks: KillswitchBlock }) {
  return (
    <div className="space-y-2">
```

with (add the `currentDd` prop + render the gauge first):

```tsx
function KillSwitchPanel({ ks, currentDd }: { ks: KillswitchBlock; currentDd: number | null }) {
  return (
    <div className="space-y-3">
      <RiskGauge
        currentDd={currentDd}
        pauseDd={ks.pause_drawdown}
        terminateDd={ks.terminate_drawdown}
        triggered={ks.triggered}
      />
```

(The rest of `KillSwitchPanel` — the triggered banner and the two threshold cards — stays unchanged.)

- [ ] **Step 4: Pass `currentDd` at the call site**

In `Testnet.tsx` (inside `TestnetCard`, line 531), replace:

```tsx
          <KillSwitchPanel ks={killswitch} />
```

with:

```tsx
          <KillSwitchPanel ks={killswitch} currentDd={live.max_drawdown} />
```

(`live` is already destructured at the top of `TestnetCard`: `const { live, vs_backtest, killswitch, alerts } = status;`.)

- [ ] **Step 5: Type-check + build**

Run: `cd dashboard/web && npm run build`
Expected: `tsc -b` clean + `vite build` succeeds.

- [ ] **Step 6: Commit**

```bash
git add dashboard/web/src/pages/Testnet.tsx
git commit -m "feat(web): distance-to-kill risk gauge in the kill-switch panel

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: preview verify + wrap

**Files:** none (verification only)

- [ ] **Step 1: Start backend + web dev server**

Start FastAPI (`cd dashboard/server && uvicorn main:app --port 8000`), then `preview_start` the vite dev server (it proxies `/api` — see `dashboard/web/vite.config.ts`).

- [ ] **Step 2: Load Testnet page + snapshot**

`preview_eval: window.location.href = "/testnet"` (or navigate), then `preview_snapshot`.
Expected: each live trader card's Kill Switch section shows the gauge (green/amber zones, pause line, current-DD marker) + the headroom line "當前 DD … 距暫停 … 距終止 …".

- [ ] **Step 3: Verify the zones render**

If local data has no live trader with DD, temporarily exercise the component states via `preview_eval` injecting a known status, OR confirm visually against whatever live/paper data exists. Confirm: safe (marker in green, left of pause line), caution (marker in amber band), and the `currentDd === null` "等待 equity 資料" fallback.

- [ ] **Step 4: Screenshot proof + console check**

`preview_screenshot` of a trader card showing the gauge. `preview_console_logs` — no React errors from the new component.

---

## Self-Review

**Spec coverage:**
- Pure `riskHeadroom` util (zone / headroom / fillFraction) → Task 1 ✓
- `RiskGauge` component (zones, pause line, marker, headroom text, null/over states) → Task 2 step 2 ✓
- Consume `live.max_drawdown` directly (no frontend DD recompute) → Task 2 step 4 ✓
- Enhance `KillSwitchPanel`, no new panel, no card refactor → Task 2 steps 3-4 ✓
- No schema/backend change, no vitest → honored ✓
- Verify via tsc/build + preview → Tasks 1, 2, 3 ✓

**Placeholder scan:** No TBD/TODO; util + component shown in full; edits give exact old/new strings; run steps have commands + expected. ✓

**Type consistency:** `riskHeadroom(currentDd, pauseDd, terminateDd): RiskHeadroom` — signature in Task 1 matches the call in `RiskGauge` (Task 2). `RiskGauge` props (`currentDd: number | null`, `pauseDd`, `terminateDd`, `triggered`) match the render in `KillSwitchPanel` (Task 2 step 3). `KillSwitchPanel`'s new `currentDd: number | null` prop matches `live.max_drawdown` (`LiveBlock.max_drawdown: number | null`) passed at the call site (Task 2 step 4). `ks.pause_drawdown` / `ks.terminate_drawdown` / `ks.triggered` are existing `KillswitchBlock` fields. ✓
