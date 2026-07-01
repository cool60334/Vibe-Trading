# Combinatorial Purged Cross-Validation (CPCV) — Design

**Date:** 2026-06-26
**Status:** Approved (agy-reviewed; pending spec review)
**Scope:** #3 — add a CPCV validation step that replaces the single walk-forward
OOS path with a distribution of out-of-sample Sharpes, gated on the
distribution's mean and 5th percentile. Opt-in (per-strategy command), not part
of the always-on serial pipeline.

## Problem

Validation today is a **single** walk-forward path: Stage 4 tunes on one train
window `[2022-05, 2025-01)` and evaluates the best params on one OOS window
`[2025-01, today]`. One path is one lucky/unlucky draw — a strategy can pass (or
fail) OOS by the accident of where the split fell. Combined with a broad param
sweep (picking the best of ~40 combos overfits that specific train window), the
single OOS number carries low confidence. This is why deployment currently needs
3–5 months of paper-forward before trusting a strategy.

## Goal

Produce a **distribution** of OOS Sharpes via CPCV (Bailey & López de Prado):
partition the timeline into N blocks, form all C(N,k) combinatorial train/test
splits, **re-select the best params on each split's train blocks** (Fork A —
honest per-path out-of-sample selection), and evaluate on its test blocks. The
distribution's mean is an unbiased estimate of true performance; its lower tail
measures robustness. A strategy that survives the distribution needs far less
paper-forward. Gate on `cpcv_mean_sharpe` (main) and `cpcv_p05_sharpe`
(defensive).

## Non-goals (explicit)

- **Not ML CPCV.** Strategies are rule-based; "training" a path = re-running the
  deterministic Stage 4 sweep on that path's train blocks, not fitting a model.
- **Not always-on.** ~400 backtests per strategy is too heavy for every pipeline
  pass; CPCV is an opt-in command run when a strategy is a promotion candidate.
- **Not fatal initially.** Gates are non-fatal (surface first), like the DSR gate.
- **Not replacing** the existing Stage 4 OOS holdout — CPCV is an add-on.

## Key correctness requirements (agy review)

1. **Warm-up (fixes fatal boundary bias).** Backtesting a block in isolation
   starts flat — it misses the position carried in from the previous block and
   force-settles at the block end, corrupting the pooled returns. Every block
   backtest must run over `[block_start − max_hold_hours, block_end]` and the
   warm-up prefix (`max_hold_hours`) is **dropped** before returns are cached.
2. **Sample-level purge, not block-level.** Blocks are ~5 months; dropping a
   whole adjacent train block to prevent 7-day leakage would discard months of
   training data. Instead, when a train block is adjacent to a test block, drop
   only the `PURGE_BARS` (= max forward horizon, 168h = 168 bars at 1H) at the
   touching boundary before pooling.
3. **Resolution N=10, k=3 → 120 paths.** (N=6,k=2 gives only 15 — too few for a
   stable distribution.)
4. **Pooled Sharpe** is order-independent (Sharpe = mean/std of per-bar returns);
   pooling returns across non-contiguous blocks is valid once (1) and (2) hold.

## Design

### 1. Pure lib `research/lib/cpcv.py`

```python
def make_blocks(start_date, end_date, n_blocks, interval) -> list[tuple[str, str]]:
    """N contiguous, equal-length (start, end) date blocks over the range."""

def combinatorial_splits(n_blocks, k_test) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """All C(n_blocks, k_test) (train_block_ids, test_block_ids) index tuples."""

def purge_boundary_bars(
    block_returns: dict[int, pd.Series], train_ids, test_ids, purge_bars
) -> dict[int, pd.Series]:
    """Return a copy of the train blocks' per-bar returns with `purge_bars` rows
    dropped at any boundary touching a test block (head if the block precedes a
    test block, tail if it follows one — both if sandwiched)."""

def pooled_sharpe(block_returns, block_ids, bars_per_year) -> float:
    """Concatenate the per-bar returns of `block_ids`, return annualised Sharpe
    (mean/std * sqrt(ppy)). Returns nan if <2 pooled bars or zero std."""

def cpcv_distribution(
    combo_block_returns: dict[str, dict[int, pd.Series]],  # combo_id -> {block_id: returns}
    splits, purge_bars, bars_per_year,
) -> dict:
    """For each split: pick the best combo by purged pooled TRAIN Sharpe, then
    score that combo's pooled TEST Sharpe. Returns
    {n_paths, path_sharpes: [...], cpcv_mean_sharpe, cpcv_p05_sharpe,
     pct_paths_positive}."""
```

All pure, no IO — fully unit-testable.

### 2. Orchestration `research/pipeline/stage4_cpcv.py`

A standalone command (mirrors `stage4_optimize` structure; reuses its combo
helpers `expand_param_ranges`, `sample_combos`, `apply_overrides_to_spec`,
`_compile_signal_code`, `_scaffold_combo_run`, `_invoke_backtest`).

```
python -m research.pipeline.stage4_cpcv --strategy <id> [--blocks 10 --k 3 --max 40]
```

Steps per strategy:
1. Load YAML, expand `parameter_search_ranges`, sample `--max` combos (same as
   Stage 4). `max_hold_hours` = the spec's `time_based` exit (fallback: max of
   `horizons_h`).
2. `blocks = make_blocks(period_start, oos_end, --blocks, interval)` over the
   **full** history (train+OOS), since CPCV re-splits everything.
3. **Precompute combo×block matrix:** for each combo × each block, scaffold a run
   over `[block_start − max_hold_hours, block_end]`, backtest, read
   `artifacts/equity.csv`, convert to per-bar returns, **drop the warm-up
   prefix**, cache as `combo_block_returns[combo_id][block_id]`.
   (10 blocks × ~40 combos ≈ 400 backtests — offline, once per strategy.)
4. `splits = combinatorial_splits(--blocks, --k)`; `dist =
   cpcv_distribution(combo_block_returns, splits, PURGE_BARS, ppy)`.
5. Write `research/manifests/<id>/cpcv.json` (a `CPCVBlock`).

`PURGE_BARS = max(horizons_h)` bars; `ppy = bars_per_year(interval)` (reuse
`lib.deflated_sharpe.bars_per_year`).

### 3. Schema `dashboard/server/schemas.py`

```python
class CPCVBlock(_Manifest):
    n_paths: int
    cpcv_mean_sharpe: Optional[float] = None
    cpcv_p05_sharpe: Optional[float] = None
    pct_paths_positive: Optional[float] = None
    n_blocks: Optional[int] = None
    k_test: Optional[int] = None
```

Add `cpcv: Optional[CPCVBlock] = None` to `StrategyManifest`. New constants:
`GATE_MIN_CPCV_MEAN_SHARPE = 1.0`, `GATE_MIN_CPCV_P05_SHARPE = 0.0`.

### 4. Gate `research/emit_manifest.py`

`compute_gate(backtest, optimization=None, cpcv=None)`; when `cpcv is not None`,
append two **non-fatal** thresholds:
- `cpcv_mean_sharpe >= GATE_MIN_CPCV_MEAN_SHARPE` (main)
- `cpcv_p05_sharpe > GATE_MIN_CPCV_P05_SHARPE` (defensive)

Absent when `cpcv is None` (backward compatible). `emit_manifest` loads
`cpcv.json` (like `optimization.json`) and threads it into the gate and manifest.

## Data flow

Each block backtest already emits `artifacts/equity.csv`
(`agent/backtest/engines/base.py:709`). CPCV reads those, pools per-bar returns,
and computes the distribution. No backtest-engine change. The combo×block matrix
is the only new (bounded) compute cost.

## Edge cases

- A combo whose block backtest errors / has no equity.csv → that block's returns
  are absent; `pooled_sharpe` skips missing blocks and returns nan if a split's
  train or test pool is empty → that path is dropped from the distribution.
- `--k >= --blocks` or `n_paths == 0` → error out (bad config).
- Purge removing all of a train block → that block contributes nothing; if all
  train pools empty for a split, the split is dropped.
- Strategy with no `parameter_search_ranges` → nothing to sweep; skip (like
  Stage 4).
- `cpcv.json` absent (CPCV never run) → no CPCV gate; manifest unaffected.

## Testing (TDD)

**lib (`research/tests/test_cpcv.py`):**
1. `make_blocks` → N contiguous, non-overlapping, covering the range.
2. `combinatorial_splits(10, 3)` → exactly 120 splits; train/test disjoint; union = all blocks.
3. `purge_boundary_bars` drops exactly `purge_bars` at a train block adjacent to a test block; head vs tail vs sandwiched; non-adjacent blocks untouched.
4. `pooled_sharpe` = annualised mean/std of concatenated returns; nan on <2 bars / zero std.
5. `cpcv_distribution`: a combo that is best on some splits' train wins those paths; `cpcv_mean_sharpe`/`cpcv_p05_sharpe`/`pct_paths_positive` computed correctly on a hand-built returns matrix; empty-pool paths dropped.

**schema (`dashboard/server/test_schemas.py`):** `CPCVBlock` defaults; `StrategyManifest.cpcv` optional; new constants' values.

**emit_manifest (`research/tests/test_emit_manifest.py`):** CPCV present → two non-fatal thresholds with correct pass/fail; `cpcv=None` → absent; `compute_gate(backtest)` and `compute_gate(backtest, optimization)` still work.

(Research and dashboard pytest suites run separately.)

## Follow-ups (out of scope, recorded)

- **Promote CPCV gates to fatal** once trusted; **shorten paper-forward** policy
  (e.g. survive CPCV → 1-month paper instead of 3–5).
- **DSR on the CPCV mean Sharpe** (deflate the mean by the 120-path search).
- **Auto-run in pipeline** for promotion candidates (currently manual command).
- **Parallelise** the combo×block matrix (currently serial subprocess backtests).
