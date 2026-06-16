# Order-Flow Maker-Aware Bookend Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bracket the net-of-fee verdict for an ETH 15m order-flow strategy with a taker lower bound and an optimistic-maker upper bound, after first proving the factor IC is lookahead-free.

**Architecture:** Task 1 is a gating lookahead audit (cheap, decisive — if the IC peeks at the future, abort). Then a backward-compatible `entry_is_maker` engine knob, a stage3 `--exec-regime` flag, a hand-curated order-flow strategy, and a decision reporter computing net Sharpe + profit/cost ratio + analytic breakeven maker-rate + breakeven fill-rate.

**Tech Stack:** Python, pandas, scipy, existing research pipeline (`research/pipeline/stage3_backtest.py`), the backtest engine (`agent/backtest/engines/crypto.py`), existing curated-strategy DSL (`research/strategies/`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-16-orderflow-maker-backtest-design.md`

**Test/run convention:** research tests run from `research/` (`cd research && python -m pytest tests/<file> -v`); engine tests run from `agent/` (`cd agent && python -m pytest ...`). Run research and dashboard/agent suites **separately**. Server-only steps (real cache + network) are marked — do not run them locally.

**Conventions:** commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch `quant-trading-dashboard` (no new branch/worktree).

---

## File Structure

| Path | Responsibility | New/Modify |
|---|---|---|
| `research/scripts/of_lookahead_audit.py` | Compare factor IC under "enter at signal-bar close" vs "enter one bar later" — proves no same-bar peek. | New |
| `research/lib/orderflow_eval.py` | Add `execution_ic(factor, price, entry_lag_bars, hold_bars)` reused by the audit + reporter. | Modify |
| `agent/backtest/engines/crypto.py` | `entry_is_maker` config knob (maker fee + no-slippage on entry). | Modify |
| `agent/backtest/engines/test_crypto.py` (or existing engine test) | Engine knob tests. | New/Modify |
| `research/pipeline/stage3_backtest.py` | `--exec-regime taker\|maker` threading the knob; register both runs. | Modify |
| `research/strategies/eth_of_contrarian.yaml` (+ compiled signal engine) | Hand-curated order-flow strategy. | New |
| `research/scripts/of_backtest_report.py` | Decision reporter: Sharpe, profit/cost, breakeven maker-rate, breakeven fill-rate. | New |

---

## Task 1: Lookahead audit (GATING — do first)

**Why first (gemini):** 1-bar lookahead is the order-flow death trap. If the headline IC (−0.05 @15m) secretly peeks at the future, every later cost-modelling step is futile. Prove it is clean before building anything.

**The convention (already verified, confirm in code):** OKX candles are indexed by bar-OPEN time with `close` = bar-END price (`research/lib/okx_data.py`). Order-flow `factor[T]` aggregates trades in `[T, T+15m)`, so it is known at `T+15m` = the close of the bar indexed at `T`. The eval uses `price = candles["close"]` as the base, so "enter at `price[T]`" = enter at the bar-close = the moment the factor becomes known. That is lookahead-free. This task proves it empirically: an explicit one-bar **entry lag** should not collapse the IC (a same-bar peek would).

**Files:**
- Modify: `research/lib/orderflow_eval.py` (add `execution_ic`)
- Test: `research/tests/test_orderflow_eval.py` (append)
- Create: `research/scripts/of_lookahead_audit.py`

- [ ] **Step 1: Write the failing test**

```python
# append to research/tests/test_orderflow_eval.py
from lib.orderflow_eval import execution_ic


def test_execution_ic_entry_lag_shifts_base():
    # factor[t] predicts the return realised entering at bar t's close.
    # With entry_lag_bars=0 the IC is strong; entry_lag_bars=1 enters one bar
    # later (strictly conservative) — for a persistent signal it stays same-sign.
    idx = pd.date_range("2025-01-01", periods=200, freq="15min", tz="UTC")
    rng = np.random.default_rng(1)
    factor = pd.Series(rng.normal(size=200), index=idx)
    # price whose 1-bar forward return correlates with factor
    ret = factor.shift(0) * 0.01 + rng.normal(scale=0.001, size=200)
    price = pd.Series(100 * (1 + ret).cumprod(), index=idx)
    ic0 = execution_ic(factor, price, entry_lag_bars=0, hold_bars=1, min_obs=30)
    ic1 = execution_ic(factor, price, entry_lag_bars=1, hold_bars=1, min_obs=30)
    assert ic0 > 0.3              # strong at zero-lag
    assert np.sign(ic1) == np.sign(ic0) or np.isnan(ic1)  # not a sign flip artifact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py::test_execution_ic_entry_lag_shifts_base -v`
Expected: FAIL — `ImportError: cannot import name 'execution_ic'`.

- [ ] **Step 3: Implement `execution_ic`** (append to `research/lib/orderflow_eval.py`)

```python
def execution_ic(
    factor: pd.Series, price: pd.Series, entry_lag_bars: int, hold_bars: int,
    min_obs: int = 30,
) -> float:
    """IC of factor vs the return of entering `entry_lag_bars` after the signal
    bar's close and holding `hold_bars`. entry_lag_bars=0 == enter at the signal
    bar's close (the factor-known time). A same-bar lookahead would show a large
    IC drop from lag 0 to lag 1; a real signal persists."""
    entry = price.shift(-entry_lag_bars)
    exit_ = price.shift(-(entry_lag_bars + hold_bars))
    fwd = exit_ / entry - 1.0
    return _ic(factor, fwd, min_n=min_obs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py -v`
Expected: PASS (all prior + new).

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow_eval.py research/tests/test_orderflow_eval.py
git commit -m "feat(orderflow-eval): execution_ic with entry lag for lookahead audit"
```

- [ ] **Step 6: Create the audit driver** `research/scripts/of_lookahead_audit.py`

```python
"""Lookahead audit: factor IC vs entry lag. A same-bar peek collapses from
lag 0 to lag 1; a real signal persists. Run on the server (DAYS must cover the
whole order-flow cache)."""
from pathlib import Path

import pandas as pd

from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import execution_ic
from lib.okx_data import fetch_candles

INTERVAL = "15m"
DAYS = 400
of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")
price = fetch_candles("ETH-USDT-SWAP", DAYS, bar=INTERVAL)["close"].reindex(of.index).ffill()
feats = orderflow_factors(of, of.index, INTERVAL)

print(f"{'factor':24} {'lag0 (signal-close)':>20} {'lag1 (next bar)':>16}")
for name, f in feats.items():
    ic0 = execution_ic(f, price, entry_lag_bars=0, hold_bars=4, min_obs=200)  # ~1h hold
    ic1 = execution_ic(f, price, entry_lag_bars=1, hold_bars=4, min_obs=200)
    print(f"{name:24} {ic0:+20.4f} {ic1:+16.4f}")
```

- [ ] **Step 7: Run on server + DECISION GATE**

Run (server): `cd research && PYTHONPATH=. python scripts/of_lookahead_audit.py`

**Decision:**
- **lag0 ≈ lag1 (same sign, similar magnitude)** → no same-bar peek → IC is clean → **proceed to Task 2.**
- **lag0 ≫ lag1 (collapses toward 0)** → the edge only exists if you act *inside* the signal bar = lookahead/unrealistic → **STOP**; the headline IC is an artifact, re-frame the whole 4c (the signal is not tradeable at bar granularity). Report this and escalate to the human before any further task.

---

## Task 2: Engine `entry_is_maker` knob

**Files:**
- Modify: `agent/backtest/engines/crypto.py`
- Test: `agent/backtest/engines/test_crypto.py` (create if absent; otherwise the engine's existing test module)

**Current behavior (`crypto.py`):** `calc_commission` charges `taker_rate` on opens, `maker_rate` on closes. `apply_slippage` always shifts price unfavorably. We add a config knob so opens can be maker with no entry slippage, default off (zero regression).

- [ ] **Step 1: Write the failing tests**

```python
# agent/backtest/engines/test_crypto.py  (adapt import path to the package layout)
from backtest.engines.crypto import CryptoEngine


def _eng(**cfg):
    base = {"maker_rate": 0.0002, "taker_rate": 0.00055, "slippage": 0.0005}
    base.update(cfg)
    return CryptoEngine(base)


def test_default_open_is_taker():
    e = _eng()  # entry_is_maker defaults False
    assert e.calc_commission(1.0, 100.0, 1, is_open=True) == 0.00055 * 100


def test_entry_is_maker_open_charges_maker():
    e = _eng(entry_is_maker=True)
    assert e.calc_commission(1.0, 100.0, 1, is_open=True) == 0.0002 * 100


def test_close_always_maker():
    for flag in (True, False):
        e = _eng(entry_is_maker=flag)
        assert e.calc_commission(1.0, 100.0, 1, is_open=False) == 0.0002 * 100


def test_maker_entry_skips_slippage():
    e = _eng(entry_is_maker=True)
    assert e.apply_slippage(100.0, 1, is_open=True) == 100.0  # no slip on maker entry


def test_taker_entry_keeps_slippage():
    e = _eng()  # default taker
    assert e.apply_slippage(100.0, 1, is_open=True) == 100.0 * (1 + 0.0005)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agent && python -m pytest backtest/engines/test_crypto.py -v`
Expected: FAIL — `TypeError: apply_slippage() got an unexpected keyword argument 'is_open'` / maker-entry assertions fail.

- [ ] **Step 3: Implement the knob** in `agent/backtest/engines/crypto.py`

In `__init__`, after `self.funding_rate = ...`:

```python
        self.entry_is_maker: bool = bool(config.get("entry_is_maker", False))
```

Replace `calc_commission`:

```python
    def calc_commission(self, size: float, price: float, _direction: int, is_open: bool) -> float:
        """Maker/Taker separated. Closes always maker. Opens are taker by default,
        or maker when ``entry_is_maker`` is set (maker-execution bookend)."""
        if is_open:
            rate = self.maker_rate if self.entry_is_maker else self.taker_rate
        else:
            rate = self.maker_rate
        return size * price * rate
```

Replace `apply_slippage` to accept `is_open` and skip slippage for maker entries:

```python
    def apply_slippage(self, price: float, direction: int, is_open: bool = True) -> float:
        """Unfavourable slippage, except a maker entry assumes a resting limit
        fill with no spread crossing."""
        if is_open and self.entry_is_maker:
            return price
        return price * (1 + direction * self.slippage_rate)
```

NOTE: `apply_slippage` gains an `is_open` param with a default, so existing callers that don't pass it still compile. **Check the call sites** in `BaseEngine`/`crypto.py` (e.g. the liquidation path at `crypto.py:78` passes only `(mark_price, -pos.direction)` — that is a close-ish/liquidation, leave it defaulting to `is_open=True` but it is a taker liquidation so it must still slip; pass `is_open=False` there to keep slippage). Update the liquidation call:

```python
                liq_price = self.apply_slippage(mark_price, -pos.direction, is_open=False)
```

Also find the entry/exit call sites in `BaseEngine` that call `apply_slippage(price, direction)` and pass `is_open=True` for opens and `is_open=False` for closes so the maker-entry skip only applies to opens. (Read `agent/backtest/engines/base.py` to locate them.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agent && python -m pytest backtest/engines/test_crypto.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full engine suite for regression**

Run: `cd agent && python -m pytest backtest/ -q`
Expected: PASS — default `entry_is_maker=False` keeps all existing backtests identical.

- [ ] **Step 6: Commit**

```bash
git add agent/backtest/engines/crypto.py agent/backtest/engines/test_crypto.py
git commit -m "feat(engine): entry_is_maker knob for maker-execution bookend"
```

---

## Task 3: stage3 `--exec-regime` flag

**Files:**
- Modify: `research/pipeline/stage3_backtest.py`
- Test: `research/tests/test_stage3_backtest.py` (append)

**Goal:** a CLI flag that threads `entry_is_maker` into the engine config and tags the run, reusing the existing run-registration path (mirror how `--stress` is handled).

- [ ] **Step 1: Read the existing `--stress` wiring** in `research/pipeline/stage3_backtest.py` — find the `argparse` setup, where the engine config dict is built, and where runs are registered (the `update_stress_runs` writer). Mirror that exact pattern for `--exec-regime`.

- [ ] **Step 2: Write the failing test** (append to `research/tests/test_stage3_backtest.py`, matching the module's existing test style — adapt the helper names to what the file already uses)

```python
def test_exec_regime_maker_sets_entry_is_maker(monkeypatch):
    # The engine config built for --exec-regime maker must carry entry_is_maker=True
    import pipeline.stage3_backtest as s3
    cfg = s3.build_engine_config(exec_regime="maker")   # adapt to real builder name
    assert cfg["entry_is_maker"] is True


def test_exec_regime_taker_default(monkeypatch):
    import pipeline.stage3_backtest as s3
    cfg = s3.build_engine_config(exec_regime="taker")
    assert cfg.get("entry_is_maker", False) is False
```

If stage3 has no isolated `build_engine_config`, this task's first real step is to **extract** the engine-config construction into a small testable function `build_engine_config(..., exec_regime="taker")` (a refactor that also makes `--stress` cleaner), then test it.

- [ ] **Step 3: Run test to verify it fails.**
Run: `cd research && python -m pytest tests/test_stage3_backtest.py -k exec_regime -v`
Expected: FAIL (no `build_engine_config` / no `entry_is_maker`).

- [ ] **Step 4: Implement** — add `--exec-regime` to argparse (choices `["taker","maker"]`, default `"taker"`); in the engine-config builder set `entry_is_maker = (exec_regime == "maker")`; tag the registered run with the regime (extend the run dict the stress writer already produces). For the maker regime, also accept a `--maker-rate` override (default the config's `maker_rate`) so the reporter can drive the breakeven sweep.

- [ ] **Step 5: Run test + full stage3 suite.**
Run: `cd research && python -m pytest tests/test_stage3_backtest.py -v`
Expected: PASS; existing stage3 tests unaffected (default taker == legacy).

- [ ] **Step 6: Commit**

```bash
git add research/pipeline/stage3_backtest.py research/tests/test_stage3_backtest.py
git commit -m "feat(stage3): --exec-regime taker|maker for bookend backtest"
```

---

## Task 4: Component 5 — order-flow source registry entry

**Files:**
- Modify: `research/lib/sources.py`
- Test: `research/tests/test_sources_registry.py` (append)

**Goal:** register `binance_orderflow` so stage0/strategy metadata can reference the order-flow category. (If Task 5 confirms the hand-curated strategy reads factor values directly without needing a registered source, this task may be trimmed — but the category Literal is needed wherever the factors are declared.)

- [ ] **Step 1: Write the failing test** (append to `research/tests/test_sources_registry.py`)

```python
def test_binance_orderflow_source_registered():
    from lib.sources import SOURCE_REGISTRY
    spec = SOURCE_REGISTRY["binance_orderflow"]
    assert spec.status == "available"
    assert spec.category == "orderflow"
```

- [ ] **Step 2: Run test, confirm FAIL** (KeyError).
Run: `cd research && python -m pytest tests/test_sources_registry.py -k orderflow -v`

- [ ] **Step 3: Implement** — add to `SOURCE_REGISTRY` in `research/lib/sources.py`:

```python
    "binance_orderflow": SourceSpec(
        fetcher=None,  # served from the cached parquet via the orderflow loader, not a series fetcher
        status="available",
        description="Binance aggTrades-derived per-bar order-flow features (15m/30m cache)",
        category="orderflow",
    ),
```

Then extend the `FactorCandidate.category` Literal (find its definition — likely in the stage0 schema) to include `"orderflow"`. Add/adjust any category-validation test accordingly.

- [ ] **Step 4: Run test, confirm PASS.** Run the sources + stage0 schema test files.

- [ ] **Step 5: Commit**

```bash
git add research/lib/sources.py research/tests/test_sources_registry.py
git commit -m "feat(sources): register binance_orderflow source + orderflow category"
```

---

## Task 5: Hand-curated order-flow strategy

**Files:**
- Create: `research/strategies/eth_of_contrarian.yaml` (+ compiled signal engine, per the curated-strategy convention)
- Test: per the existing curated-strategy test pattern

**This task is integration-heavy — start by reading, not writing.**

- [ ] **Step 1: Verify the wiring prerequisite (BLOCKER CHECK).** Confirm the order-flow factor values are available to the backtest. stage0a writes features to a parquet; the compiled signal engine reads `factor_values_*.parquet`. Check (server or by reading stage0a's write path + signal_engine's read path) that `trade_count_imbalance` / `price_impact` appear in the parquet the stage3 backtest loads at the `15m` namespace. If they do NOT, the first sub-step is to ensure stage0a persists order-flow features into `factor_values` (mirror how funding/oi features are persisted). **If this cannot be made to work, report BLOCKED — the backtest cannot see the factors.**

- [ ] **Step 2: Read the curated-strategy pattern.** Open an existing curated strategy (e.g. `research/strategies/` eth_s5 yaml + its compiled `signal_engine`) and the DSL/compiler (`stage2b_compile_signal`, the `StrategySpec` schema with `regime_filter`/`size_mult`/`signal_invalidation`). Match its structure exactly.

- [ ] **Step 3: Author `eth_of_contrarian.yaml`** — a short-horizon contrarian:
  - factor: `price_impact` (or `trade_count_imbalance`), interval `15m`
  - entry: extreme factor z-score (contrarian — short when buy-pressure extreme, long when sell-pressure extreme), threshold tuned to trade only the informative tail (consistent with the quantile finding)
  - exit: `signal_invalidation` or a fixed ~4-bar (~1h) hold (matches the ~1h half-life)
  - keep sizing/leverage conservative (1x); no regime overlay initially
  Mirror the exact YAML keys the compiler expects (from Step 2).

- [ ] **Step 4: Compile + smoke** — run `stage2b_compile_signal` for the new strategy; confirm it produces a signal engine without error. Add/extend a test asserting the compiled signal is non-trivial (produces both long and short entries on a fixture).

- [ ] **Step 5: Commit**

```bash
git add research/strategies/eth_of_contrarian.yaml research/strategies/code/eth_of_contrarian/
git commit -m "feat(strategy): hand-curated eth order-flow contrarian (15m)"
```

---

## Task 6: Decision reporter

**Files:**
- Modify: `research/lib/orderflow_eval.py` (add `breakeven_maker_rate`, `profit_cost_ratio`)
- Test: `research/tests/test_orderflow_eval.py` (append)
- Create: `research/scripts/of_backtest_report.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to research/tests/test_orderflow_eval.py
from lib.orderflow_eval import breakeven_maker_rate, profit_cost_ratio


def test_breakeven_maker_rate_zero_when_gross_zero():
    # gross pnl 0 -> breakeven maker rate is 0 (any positive fee loses)
    assert breakeven_maker_rate(gross_pnl=0.0, notional=1000.0, round_trips=10) == 0.0


def test_breakeven_maker_rate_positive_edge():
    # gross 5.0 over 10 round trips on 1000 notional each -> per-side breakeven
    # rate = gross / (round_trips * notional * 2 sides)
    r = breakeven_maker_rate(gross_pnl=5.0, notional=1000.0, round_trips=10)
    assert abs(r - 5.0 / (10 * 1000.0 * 2)) < 1e-12


def test_profit_cost_ratio():
    assert profit_cost_ratio(net_pnl=20.0, total_cost=10.0) == 2.0
    assert profit_cost_ratio(net_pnl=-5.0, total_cost=10.0) == -0.5
```

- [ ] **Step 2: Run tests, confirm FAIL** (ImportError).

- [ ] **Step 3: Implement** (append to `research/lib/orderflow_eval.py`)

```python
def breakeven_maker_rate(gross_pnl: float, notional: float, round_trips: int) -> float:
    """Per-side maker rate at which net PnL = 0. Two sides per round trip.
    Returns max(0, ...) — a non-positive gross edge breaks even only at a rebate."""
    denom = round_trips * notional * 2.0
    if denom <= 0:
        return 0.0
    return max(0.0, gross_pnl / denom)


def profit_cost_ratio(net_pnl: float, total_cost: float) -> float:
    """Average net profit per unit of round-trip cost. >1 means the edge clears
    fees with margin; <1 means working for the exchange."""
    if total_cost == 0:
        return float("inf") if net_pnl > 0 else 0.0
    return net_pnl / total_cost
```

- [ ] **Step 4: Run tests, confirm PASS.**

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow_eval.py research/tests/test_orderflow_eval.py
git commit -m "feat(orderflow-eval): breakeven maker-rate + profit/cost ratio"
```

- [ ] **Step 6: Create the reporter** `research/scripts/of_backtest_report.py` that loads the taker + maker regime runs (from the stage3 run registry / strategy_runs.json the runs are written to — confirm the path from Task 3), and for each prints: net Sharpe, total fees, avg net profit/round-trip, `profit_cost_ratio`, `breakeven_maker_rate`, and a **breakeven fill-rate** = the fill fraction at which net PnL hits 0 (scale the maker-regime gross edge linearly by assumed fill rate, solve for 0). Then apply the §2 decision rule and print one of: `ALIVE (paper)`, `DEAD (stop)`, `AMBIGUOUS (build fill model)`.

  Write the file with the actual metric calls (reusing the functions above); the run-loading path is the one Task 3 registered to. Do not run it locally (needs server run artifacts).

- [ ] **Step 7: Commit the reporter.**

```bash
git add research/scripts/of_backtest_report.py
git commit -m "feat(orderflow-eval): bookend backtest decision reporter"
```

---

## Task 7: Run the bookend backtest (server) + verdict

- [ ] **Step 1 (server):** pull the branch. Build the OF strategy's factor values into the 15m namespace if Task 5 Step 1 required it.
- [ ] **Step 2 (server):** run stage3 for `eth_of_contrarian` @15m under both regimes:
  ```bash
  RESEARCH_INTERVAL=15m RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.stage3_backtest --exec-regime taker --stress
  RESEARCH_INTERVAL=15m RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.stage3_backtest --exec-regime maker --stress
  ```
- [ ] **Step 3 (server):** `cd research && PYTHONPATH=. python scripts/of_backtest_report.py`
- [ ] **Step 4: Apply the verdict** (§2 / spec §7): ALIVE → paper; DEAD → stop, do not buy L2; AMBIGUOUS → spec the fill-gated + adverse-selection round.

---

## Plan Self-Review Notes

- **Spec coverage:** §2 regimes+rule → Tasks 2,3,6,7; §3 components 1-5 → Tasks 2,3,4,5,6; §4 lookahead gate → Task 1 (first, gating); §5 adverse-selection out-of-scope → reflected (breakeven fill-rate in Task 6, full model deferred); §6 tests → each task; §7 verdict → Task 7.
- **Ordering rationale:** lookahead audit (Task 1) gates everything per gemini — a same-bar peek aborts before any engine work.
- **Unverified externals to confirm during implementation (flagged in-task, not placeholders):** stage3's exact engine-config build + run-registration path (Task 3 Step 1 reads it); `BaseEngine.apply_slippage` call sites (Task 2 Step 3); the curated-strategy DSL keys + the factor_values persistence of order-flow features (Task 5 Steps 1-2, with an explicit BLOCKED path if the factors are not in the parquet); the run-artifact path the reporter loads (Task 6 Step 6).
- **No production code without a failing test:** Tasks 1,2,3,4,6 are TDD; Task 5 is integration (tested via compile smoke); Task 7 is operational.
