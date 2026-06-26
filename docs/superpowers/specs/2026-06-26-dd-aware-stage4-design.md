# DD-aware Stage 4 combo selection — Design

**Date:** 2026-06-26
**Status:** Approved (agy spec review folded — 4 fixes: hierarchical fallback, NaN guard, all-bust-DD skip, tests)
**Scope:** B1 only — make the Stage 4 grid sweep drawdown-aware when picking `best`.

## Problem

Stage 4 (`research/pipeline/stage4_optimize.py`) runs a deterministic parameter
grid sweep on the **train** window, then validates the single best combo on a
held-out **OOS** window. The sweep already isolates the holdout at the data
level (the OOS window is never seen during tuning).

But `rank_combos` picks `best` by **train sharpe alone**, gated only on
`trade_count >= 10`. Drawdown never enters the selection. So the highest-sharpe
combo is frequently also a high-drawdown combo. When that combo's OOS holdout
later fails the deploy drawdown ceiling, the operator's only recourse is to
manually narrow the YAML `parameter_search_ranges` (e.g. shrink the `size_mult`
grid) and **re-run Stage 4** — tuning against the holdout. That manual loop is
the real contamination source: it turns the OOS set into a validation set.
(Confirmed previously: `sol_s1`'s 1.79 OOS Sharpe is optimistic for exactly
this reason.)

## Goal

Make Stage 4 select `best` under the **same drawdown budget the deploy gate
enforces**, computed on the train window. Then the operator has no reason to
re-tune against the holdout, because a DD-busting combo can no longer become
`best`. The OOS holdout reverts to an honest one-shot test: if it still busts
the budget, that is genuine out-of-sample fragility (an honest reject), not a
signal to re-tune.

## Non-goals (explicit)

- Not touching the deploy gate (`emit_manifest.compute_gate`); the OOS
  `max_drawdown` threshold stays non-fatal. (That is B2, deferred.)
- No grid-provenance / re-run tripwire. (That is B3, deferred.)
- No composite/Calmar scoring — rejected because a tunable DD-penalty weight
  re-creates the contamination loop in a new form (operator re-tunes the weight
  instead of the grid). Both review AIs converged on this.

## Design

All changes in `research/pipeline/stage4_optimize.py`.

### 1. DD ceiling constant — single source of truth

Import the deploy ceiling rather than redefining it:

```python
from schemas import GATE_MAX_DRAWDOWN  # 0.10, deploy single source of truth
DD_CEILING = GATE_MAX_DRAWDOWN
```

(`schemas` is already on `sys.path` in this module and `GATE_MAX_DRAWDOWN` is
already imported elsewhere — `dashboard/server/schemas.py:42`.) Using the deploy
constant directly keeps Stage 4's objective permanently aligned with the gate;
there is no new magic number to drift.

### 2. `ComboResult.max_drawdown` property

Mirror the existing `sharpe` / `trade_count` properties:

```python
import math  # module-level

@property
def max_drawdown(self) -> float:
    if not self.metrics:
        return float("inf")
    raw = self.metrics.get("max_drawdown")
    try:
        val = abs(float(raw))
    except (TypeError, ValueError):
        return float("inf")
    return float("inf") if math.isnan(val) else val
```

- `abs()` unconditionally — `metrics.csv` may store DD negative; this matches
  the `emit_manifest` convention (DD is a positive fraction).
- Missing field or unparseable → `inf` → conservatively fails the gate. (Same
  defensive spirit as `sharpe` returning `-inf` when metrics are absent.)
- **NaN guard:** `float("NaN")` does not raise, so a literal `"NaN"` in the CSV
  (pandas writes these) would slip through `abs(float(...))` as `nan`. `nan <=
  ceiling` is `False` so it would still be excluded, but a property yielding
  `nan` is a dirty state for any later sort/serialisation — normalise it to
  `inf` explicitly.

### 3. `rank_combos` — hierarchical gate (trade_count, then DD)

Apply the two gates in sequence so that adding DD never weakens the existing
trade_count protection. (If DD were ANDed into a single gate, an all-bust-DD
sweep would fall back to the full `valid` pool and let a sub-min-trade combo win
on raw sharpe — a regression.)

```python
def rank_combos(combos, min_trades=MIN_TRADE_COUNT_GATE, dd_ceiling=DD_CEILING):
    valid = [c for c in combos if c.metrics is not None]
    trade_gated = [c for c in valid if c.trade_count >= min_trades]
    pool = trade_gated if trade_gated else valid       # existing trade_count fallback, unchanged
    dd_gated = [c for c in pool if c.max_drawdown <= dd_ceiling]
    return sorted(dd_gated, key=lambda c: c.sharpe, reverse=True)  # may be EMPTY
```

- The trade_count gate keeps its own fallback to `valid` (existing behaviour,
  untouched). The DD gate is layered on top of that pool.
- The DD gate has **no fallback**: if no combo in the pool meets the budget,
  `rank_combos` returns an empty list. There is deliberately no "pick the
  least-bad busted combo" path — that case is handled by a skip (§3b), not by
  selecting a non-compliant `best`.
- Ranking metric unchanged (sharpe desc). DD enters only as a hard gate.
- Boundary: `<=` so exactly `0.10` passes, `0.1001` fails — consistent with the
  deploy gate's `dd_actual <= GATE_MAX_DRAWDOWN`.

### 3b. All-bust-DD → fail-soft skip (no OOS)

In `_optimize_strategy`, after ranking:

```python
ranked = rank_combos(results)
have_metrics = any(r.metrics is not None for r in results)
if have_metrics and not ranked:
    msg = (f"all {len(results)} combos exceed train DD ceiling {DD_CEILING} — "
           "skipping (redesign for DD; do not re-tune against OOS)")
    print(f"  [SKIP] {msg}")
    return OptimizationCheckResult(strategy_id=strategy_id, ok=False,
                                   skipped=True, error=msg)
best = ranked[0] if ranked else None
```

- Mirrors the `archetype_misfit` / `missing_factor` skips: non-fatal
  (`skipped=True`), writes no `optimization.json`, runs **no OOS holdout**, and
  the multi-strategy loop continues. `compute_exit_code` already treats skips as
  non-fatal, so the stage still exits 0.
- This is the contamination firewall (the user's chosen behaviour for the
  all-bust case): when train cannot meet the budget the OOS numbers are never
  computed, so they cannot bias a later grid edit. The operator sees only the
  train failure (the summary shows 0 combos passed the DD gate) and must
  redesign the concept rather than re-tune against the holdout.
- The `have_metrics` guard distinguishes this from the all-errored case (no
  combo produced metrics at all), which keeps its existing `best = None` path
  and still writes `optimization.json` with `source_run = None`.

### 4. `_summarise` — report the DD gate

Add a line mirroring the `trade_count` reporting:

```text
- combos passing max_drawdown <= 0.10: <n>
```

So the sweep log shows how many combos the DD gate removed, and makes a
fallback (0 passing) visible.

## Data flow

`backtest.runner` already writes `max_drawdown` into `metrics.csv` (confirmed:
`emit_manifest` reads it). `ComboResult.metrics` already carries the full row.
**No backtest-engine change.** The behaviour change is entirely in selection:
`ranked[0]` becomes the highest-sharpe combo whose train DD ≤ 0.10; the OOS
holdout step and all downstream stages are untouched.

## Testing (TDD)

Unit tests on the pure functions (`rank_combos`, `ComboResult.max_drawdown`)
plus one orchestration test for the skip:

1. High-sharpe/high-DD vs lower-sharpe/low-DD (both pass trade_count) →
   `rank_combos` returns the low-DD combo first.
2. **trade_count not weakened by DD layering:** when some combos pass
   trade_count, a sub-min-trade combo with higher sharpe (and passing DD) is
   never selected — the trade_count gate still filters it out first.
3. **All combos bust DD** (some pass trade_count) → `rank_combos` returns an
   empty list (no DD fallback).
4. **Skip path:** in `_optimize_strategy`, combos ran (have_metrics) but
   `rank_combos` empty → returns `skipped=True`, writes no `optimization.json`,
   runs no OOS holdout. (All-errored / no-metrics case is NOT skipped via this
   branch — keeps the `best=None` path.)
5. Boundary: DD exactly `0.10` passes; `0.1001` fails.
6. `ComboResult.max_drawdown`: negative value → `abs`; missing field →
   `inf`; unparseable string → `inf`; literal `"NaN"` → `inf` (not `nan`).

(Research and dashboard pytest suites run separately — established convention.)

## Follow-ups (out of scope, recorded)

- **Sub-window DD stability:** split train into segments, require each
  segment's max DD ≤ ceiling. Kills single-window survivorship bias. Needs
  per-segment DD (not in current `metrics.csv`) and introduces a segment-count
  choice — bigger change, deferred.
- **Pareto-front fallback:** an alternative to the §3b skip — when all combos
  bust the ceiling, instead of skipping, surface the (max sharpe, min DD) front
  for a deliberate human call. Deferred: the skip is the simpler, more
  contamination-proof default and was the user's choice for B1.
- **B2:** make the OOS `max_drawdown` gate fatal in `emit_manifest`.
- **B3:** grid-provenance tripwire to detect re-tuning against the holdout.
