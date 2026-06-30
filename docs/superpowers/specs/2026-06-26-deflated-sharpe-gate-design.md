# Deflated Sharpe Ratio gate (multiple-testing haircut) — Design

**Date:** 2026-06-26
**Status:** Approved (agy-reviewed; pending spec review)
**Scope:** #2a only — add a Deflated Sharpe Ratio (DSR) gate that haircuts the
best train Sharpe by the breadth of the Stage 4 parameter search. The
perturbation test (#2b) is a separate spec.

## Problem

Stage 4 sweeps N parameter combos on the train window and selects `best` by
Sharpe (now also DD-gated, per the DD-aware change). The selected Sharpe is the
**maximum of N noisy trials**: the more combos searched and the wider their
Sharpe dispersion, the more the best Sharpe is inflated by luck-of-the-max
rather than genuine edge. The pipeline has no statistical correction for this
search breadth. This is the survivorship / multiple-testing bias behind
optimistic in-sample results (e.g. `sol_s1`'s headline 1.79). The existing OOS
gate catches *some* of it, but a broad enough search can still produce an OOS
pass by chance.

## Goal

Compute the **Deflated Sharpe Ratio** (Bailey & López de Prado, 2014) for each
optimized strategy and gate on it. DSR is a probability in [0, 1]: it asks
whether the best train Sharpe statistically beats the *expected maximum* Sharpe
of N trials drawn under the null of no skill, where the expected maximum is
estimated from the empirical dispersion of the trial Sharpes. A strategy must
pass `DSR >= 0.95` **in addition to** the existing OOS gates.

## Non-goals (explicit)

- **Not OOS deflation.** DSR deflates the **train** Sharpe — that is where the
  N-trial selection happened. OOS is a single path with no parameter search, so
  deflating it is statistically meaningless (agy confirmed).
- **Not fatal (initially).** The gate is **non-fatal**. The normal-returns
  assumption (below) biases DSR *high* for crypto's negatively-skewed fat tails,
  and OOS windows are short, so DSR is noisy. Surface it first; promote to fatal
  later if it proves reliable.
- **No return-moment plumbing.** Uses the normal-returns simplification
  (skew = 0, kurtosis = 3); `metrics.csv` carries no higher moments and threading
  them through the shared backtest engine is out of scope.
- **Not the perturbation test (#2b).**

## Design

### 1. New pure lib `research/lib/deflated_sharpe.py`

```python
"""Deflated Sharpe Ratio (Bailey & López de Prado, 2014), normal-returns form."""
import numpy as np
import scipy.stats as st

# bars per year per interval — used to de-annualise Sharpe into per-bar units.
_BARS_PER_YEAR = {"15m": 35040, "30m": 17520, "1H": 8760, "4H": 2190, "1D": 365}


def bars_per_year(interval: str) -> int:
    """Bars per year for a pipeline interval. Defaults to 1H if unknown."""
    return _BARS_PER_YEAR.get(interval, 8760)


def deflated_sharpe(
    best_sr_per_bar: float,
    trial_srs_per_bar: "np.ndarray | list[float]",
    T: int,
    expected_sr: float = 0.0,
) -> float:
    """Probability the best trial's true per-bar Sharpe exceeds the expected
    maximum Sharpe of N trials under the null (skew=0, kurt=3).

    All Sharpes MUST be per-bar (de-annualised). ``T`` is the number of bars in
    the train window. Returns 1.0 for N < 2 (a single trial has no selection
    bias). Returns a probability in [0, 1].
    """
    srs = np.asarray(list(trial_srs_per_bar), dtype=float)
    srs = srs[np.isfinite(srs)]
    N = srs.size
    if N < 2 or T < 2:
        return 1.0

    var_srs = float(np.var(srs, ddof=1))
    if var_srs <= 0.0:
        return 1.0  # all trials identical → no dispersion to deflate against

    gamma = 0.5772156649  # Euler-Mascheroni
    max_z = (1 - gamma) * st.norm.ppf(1 - 1.0 / N) + gamma * st.norm.ppf(
        1 - 1.0 / (N * np.e)
    )
    expected_max_sr = expected_sr + np.sqrt(var_srs) * max_z

    sr_std = np.sqrt((1.0 + 0.5 * best_sr_per_bar**2) / (T - 1))
    return float(st.norm.cdf((best_sr_per_bar - expected_max_sr) / sr_std))
```

Notes:
- `trial_srs` is the set of **all valid combos'** Sharpes — the full search
  space the optimizer evaluated, NOT the DD-gated survivors. Using only
  survivors would understate N and overstate DSR (agy). `best_sr` is the
  selected best's train Sharpe and is itself one of `trial_srs`.
- Variance-of-trial-Sharpes is the standard approximation for the expected
  maximum when per-combo return paths are unavailable (only `metrics.csv`
  exists). It also self-corrects for grid neighbours being correlated: a tight
  Sharpe cluster → small variance → low benchmark → small haircut.
- Defensive returns of `1.0` (N<2, T<2, zero variance) make the gate pass when
  deflation is undefined, never spuriously fail.

### 2. `research/pipeline/stage4_optimize.py` — compute & emit

On the non-skip path (after `best` is chosen), before/at
`build_optimization_block`:

- `ppy = bars_per_year(cfg.interval)` (interval from the active config).
- `trial_srs = [c.sharpe for c in results if c.metrics is not None]` (all valid
  combos, annualised) → de-annualise: divide each by `sqrt(ppy)`.
- `best_sr_per_bar = best.sharpe / sqrt(ppy)`.
- `T = ` number of bars in the train window (`(train_end - train_start)` in days
  × bars-per-day for the interval; derived from `window` + interval).
- `dsr = deflated_sharpe(best_sr_per_bar, trial_srs_per_bar, T)`.
- `n_trials = len(trial_srs)`.

`build_optimization_block` gains `deflated_sharpe` and `n_trials` and writes them
into `optimization.json`.

### 3. `dashboard/server/schemas.py`

- `OptimizationBlock` += `deflated_sharpe: Optional[float] = None` and
  `n_trials: Optional[int] = None` (optional → archived manifests parse
  unchanged).
- New constant `GATE_MIN_DEFLATED_SHARPE: float = 0.95`.

### 4. `research/emit_manifest.py` — gate

- `compute_gate(backtest, optimization=None)` — add the optional second arg
  (default `None` keeps every existing `compute_gate(backtest)` caller and test
  working).
- When `optimization is not None and optimization.deflated_sharpe is not None`,
  append a **non-fatal** threshold:

  ```python
  GateThreshold(
      name="deflated_sharpe",
      threshold=GATE_MIN_DEFLATED_SHARPE,
      actual=optimization.deflated_sharpe,
      passed=optimization.deflated_sharpe >= GATE_MIN_DEFLATED_SHARPE,
      fatal=False,
  )
  ```

  When `deflated_sharpe` is `None` (no sweep / archived run), the threshold is
  **not added** — it does not drag `overall_pass`. Backward compatible.
- Pass `optimization` at the existing call site (`gate = compute_gate(backtest,
  optimization)`).

## Data flow

`stage4` already has every input: per-combo train Sharpes (`ComboResult.sharpe`),
the selected best, the train window, and the interval. It computes DSR and writes
it into `optimization.json`. `emit_manifest` already loads `optimization.json`
into an `OptimizationBlock`; it now threads `deflated_sharpe` into the gate. **No
backtest-engine change, no new per-combo IO.**

## Edge cases

- Stage 4 fail-soft skip (all combos bust DD) → no `optimization.json` → no DSR.
  Fine.
- `N < 2`, `T < 2`, or zero trial-Sharpe variance → DSR = 1.0 (passes).
- `deflated_sharpe is None` on a manifest → DSR threshold omitted; existing gates
  unaffected.
- De-annualisation uses an interval-derived `ppy` constant, a close approximation
  of the data-derived `bpy` the backtest used to annualise; the small mismatch is
  negligible for a 0.95 gate.

## Testing (TDD)

**lib (`research/tests/test_deflated_sharpe.py`):**
1. `N < 2` → 1.0; `T < 2` → 1.0; zero-variance trials → 1.0.
2. Best Sharpe far above a tight cluster → DSR near 1.0.
3. Best Sharpe ≈ mean of a wide spread → DSR low (< 0.5).
4. Monotonicity: holding best fixed, DSR decreases as N grows (more trials → higher benchmark).
5. `bars_per_year` returns the right constant per interval and defaults to 8760.

**stage4 (`research/tests/test_stage4_optimize.py`):**
6. `build_optimization_block` carries `deflated_sharpe` and `n_trials` when supplied; validates against `OptimizationBlock`.
7. (orchestration, pure-function boundary) the de-annualise + T derivation feed the lib — covered by the lib tests plus a thin helper test if extracted.

**emit_manifest (`research/tests/test_emit_manifest.py`):**
8. `optimization.deflated_sharpe` present → gate has a `deflated_sharpe` threshold, `fatal=False`, pass/fail correct at the 0.95 boundary.
9. `optimization=None` or `deflated_sharpe=None` → no `deflated_sharpe` threshold; `compute_gate(backtest)` (one-arg) still works.

(Research and dashboard pytest suites run separately — established convention.)

## Follow-ups (out of scope, recorded)

- **Promote DSR to fatal** once its reliability is confirmed against live paper-forwards.
- **Fat-tailed DSR:** emit return skew/kurtosis from the backtest and drop the
  normal-returns assumption (crypto is negatively skewed / fat-tailed, so the
  normal form biases DSR high).
- **#2b perturbation test** (factor-side noise robustness) — separate spec.
- **Effective-N via return-path clustering** (PCA/ONC) instead of the variance
  proxy — heavier, only if the variance approximation proves too lax.
