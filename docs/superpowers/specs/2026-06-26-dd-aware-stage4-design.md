# DD-aware Stage 4 combo selection — Design

**Date:** 2026-06-26
**Status:** Approved (pending spec review)
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
@property
def max_drawdown(self) -> float:
    if not self.metrics:
        return float("inf")
    raw = self.metrics.get("max_drawdown")
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return float("inf")
```

- `abs()` unconditionally — `metrics.csv` may store DD negative; this matches
  the `emit_manifest` convention (DD is a positive fraction).
- Missing field or unparseable → `inf` → conservatively fails the gate. (Same
  defensive spirit as `sharpe` returning `-inf` when metrics are absent.)

### 3. `rank_combos` — extend the gate

```python
def rank_combos(combos, min_trades=MIN_TRADE_COUNT_GATE, dd_ceiling=DD_CEILING):
    valid = [c for c in combos if c.metrics is not None]
    gated = [
        c for c in valid
        if c.trade_count >= min_trades and c.max_drawdown <= dd_ceiling
    ]
    pool = gated if gated else valid
    return sorted(pool, key=lambda c: c.sharpe, reverse=True)
```

- Ranking metric unchanged (sharpe desc). DD enters only as a hard gate,
  exactly like the existing `trade_count` gate.
- **Fallback** when no combo passes both gates: revert to the current
  behaviour (all `valid`, sharpe desc). The caller and summary surface that the
  chosen `best` may exceed the DD budget. (A Pareto-front fallback is a noted
  follow-up, not built now.)
- Boundary: `<=` so exactly `0.10` passes, `0.1001` fails — consistent with the
  deploy gate's `dd_actual <= GATE_MAX_DRAWDOWN`.

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

Unit tests on the pure functions (`rank_combos`, `ComboResult.max_drawdown`):

1. High-sharpe/high-DD vs lower-sharpe/low-DD → returns the low-DD combo first.
2. All combos exceed ceiling → fallback returns the highest-sharpe combo
   (current behaviour preserved).
3. Missing `max_drawdown` field → combo excluded from the gated pool.
4. Boundary: DD exactly `0.10` passes; `0.1001` fails.
5. `ComboResult.max_drawdown`: negative value → `abs`; missing/unparseable →
   `inf`.
6. DD gate interacts with trade_count gate: a combo must pass **both** to be
   gated.

(Research and dashboard pytest suites run separately — established convention.)

## Follow-ups (out of scope, recorded)

- **Sub-window DD stability:** split train into segments, require each
  segment's max DD ≤ ceiling. Kills single-window survivorship bias. Needs
  per-segment DD (not in current `metrics.csv`) and introduces a segment-count
  choice — bigger change, deferred.
- **Pareto-front fallback:** when all combos bust the ceiling, pick from the
  (max sharpe, min DD) front instead of pure sharpe.
- **B2:** make the OOS `max_drawdown` gate fatal in `emit_manifest`.
- **B3:** grid-provenance tripwire to detect re-tuning against the holdout.
