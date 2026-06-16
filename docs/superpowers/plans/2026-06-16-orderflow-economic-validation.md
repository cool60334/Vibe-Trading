# Order-Flow 因子經濟驗證 (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decide whether the ETH order-flow factors (real signal, low IC ~0.025 / high IR ~1.0-1.3, sub the 0.03 gate) are economically tradeable — by running the right tests instead of the mis-calibrated IC-magnitude gate.

**Architecture:** Three cheap analyses on the already-cached order-flow factors + candles (decay → tail → incremental), each a tested metric function in `research/lib/orderflow_eval.py` plus a thin driver. Each has a go/no-go gate. Only if they survive do we pay for the expensive net-of-fee backtest (Task 4, needs the deferred sources.py registry entry + stage2-5).

**Tech Stack:** pandas, scipy.stats (spearman), existing cached parquet (`research/data/orderflow/of_eth_{15m,30m}_v1.parquet`), existing OHLCV fetch. pytest.

**Background:** Phase-1 POC (`docs/superpowers/specs|plans/2026-06-16-intraday-orderflow-poc*`) found peak |IC| 0.020-0.027 at both 30m and 15m (under the 0.03 single-use gate) BUT realized IR jumped at 15m (price_impact −1.32, count_imbalance −0.94). gemini review: the IC-magnitude gate is a low-frequency ruler; by the Fundamental Law `IR ≈ IC·√breadth`, 15m's ~4× breadth makes IC 0.025 + IR>1 genuinely tradeable-quality, not noise. Conclusion corrected from "No-Go" to "real signal, economics undecided."

**Test/run convention:** research tests run from `research/`. Run pytest **separately** from the dashboard suite. Examples use `cd research && python -m pytest ...`.

**The four factors:** `trade_imbalance`, `trade_count_imbalance`, `large_trade_ratio` (orthogonal, corr~0 to the others), `price_impact`. The first/last/fourth are short-horizon contrarian; `large_trade_ratio` is positive/momentum building to 8h.

---

## File Structure

| Path | Responsibility | New/Modify |
|---|---|---|
| `research/lib/orderflow_eval.py` | Pure metric functions: `decay_profile`, `quantile_returns`, `incremental_ic`. | New |
| `research/tests/test_orderflow_eval.py` | Unit tests for the three metrics. | New |
| `research/scripts/of_decay.py` | Driver: per-factor per-bar decay + half-life + maker/taker verdict. | New |
| `research/scripts/of_quantile.py` | Driver: decile + tail (top/bottom 1%) forward returns. | New |
| `research/scripts/of_incremental.py` | Driver: IC of each factor residualized on funding_z. | New |

(Task 4 file changes are described inline, gated on Tasks 1-3.)

---

## Task 1: Decay analysis — half-life + maker-feasibility

**Why first:** It is the cheapest test and can decide everything. If the signal persists beyond the immediate next bar (half-life ≥ 2-3 bars), a **maker** (limit) order placed at bar close can fill within the persistence window → fees become **rebates**, and the low-IC concern largely dissolves. If it decays within 1 bar, you must cross the spread as a **taker** and the ~0.025 IC likely loses to fees.

**Files:**
- Create: `research/lib/orderflow_eval.py`
- Test: `research/tests/test_orderflow_eval.py`
- Create: `research/scripts/of_decay.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_orderflow_eval.py
import numpy as np
import pandas as pd

from lib.orderflow_eval import decay_profile


def test_decay_profile_perfect_one_bar_signal():
    # factor perfectly predicts the NEXT-bar return, nothing after
    idx = pd.date_range("2025-01-01", periods=6, freq="15min", tz="UTC")
    price = pd.Series([100, 101, 100, 101, 100, 101], index=idx, dtype=float)
    # factor = sign of next-bar return
    factor = pd.Series([1, -1, 1, -1, 1, np.nan], index=idx)
    prof = decay_profile(factor, price, max_bars=3)
    # 1-bar IC should be strongly positive; longer horizons weaker/noisy
    assert prof[1] > 0.8
    assert abs(prof[2]) <= prof[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py::test_decay_profile_perfect_one_bar_signal -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.orderflow_eval'`.

- [ ] **Step 3: Write minimal implementation**

```python
# research/lib/orderflow_eval.py
"""Economic-validation metrics for order-flow factors. Pure functions on
aligned (factor, price) Series. No pipeline dependency."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def _fwd_return(price: pd.Series, k: int) -> pd.Series:
    return price.shift(-k) / price - 1.0


def _ic(factor: pd.Series, fwd: pd.Series) -> float:
    df = pd.concat([factor, fwd], axis=1).dropna()
    if len(df) < 30:
        return float("nan")
    return float(spearmanr(df.iloc[:, 0], df.iloc[:, 1]).correlation)


def decay_profile(factor: pd.Series, price: pd.Series, max_bars: int) -> dict[int, float]:
    """Spearman IC of `factor` vs k-bar-forward return, k = 1..max_bars."""
    return {k: _ic(factor, _fwd_return(price, k)) for k in range(1, max_bars + 1)}


def half_life_bars(profile: dict[int, float]) -> int | None:
    """First k where |IC| drops below half the 1-bar |IC|. None if never."""
    if not profile or np.isnan(profile.get(1, float("nan"))):
        return None
    ic1 = abs(profile[1])
    for k in sorted(profile):
        if abs(profile[k]) < ic1 / 2:
            return k
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow_eval.py research/tests/test_orderflow_eval.py
git commit -m "feat(orderflow-eval): decay_profile + half_life metric"
```

- [ ] **Step 6: Write the decay driver**

```python
# research/scripts/of_decay.py
"""Per-factor decay profile + half-life + maker/taker verdict (ETH 15m)."""
from pathlib import Path

import pandas as pd

from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import decay_profile, half_life_bars
from lib.okx_data import fetch_candles  # same fetch stage0a uses

INTERVAL = "15m"
of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")
candles = fetch_candles("ETH-USDT-SWAP", bar=INTERVAL)  # adapt symbol/bar to actual signature
price = candles["close"].reindex(of.index).ffill()

feats = orderflow_factors(of, of.index, INTERVAL)
print(f"{'factor':22} {'half-life':>9}  IC by bar (1=15m)")
for name, f in feats.items():
    prof = decay_profile(f, price, max_bars=8)
    hl = half_life_bars(prof)
    bars = " ".join(f"{k}:{prof[k]:+.3f}" for k in sorted(prof))
    print(f"{name:22} {str(hl):>9}  {bars}")
```

- [ ] **Step 7: Run it on the server (where the cache + data live)**

Run: `cd research && python scripts/of_decay.py`
Expected: a half-life (in 15m bars) per factor + the per-bar IC decay.

**Decision gate (Task 1):**
- **half-life ≥ 2 bars** for the contrarian factors → signal persists; **maker execution viable** → fee regime is favorable → strongly proceed to Task 2.
- **half-life = 1 bar** (decays immediately) → must taker; record it — the Task 4 backtest must use taker fees + spread, and the bar is high. Still proceed to Task 2/3 (tail + incremental may still justify), but temper expectations.

---

## Task 2: Tail / quantile analysis

**Why:** Spearman IC is linear and dilutes nonlinear edges. Order-flow alpha often concentrates in extremes (liquidity-exhaustion gaps). Decile + top/bottom-1% forward returns reveal monotonicity and tail concentration the IC hides.

**Files:**
- Modify: `research/lib/orderflow_eval.py` (add `quantile_returns`)
- Test: `research/tests/test_orderflow_eval.py` (append)
- Create: `research/scripts/of_quantile.py`

- [ ] **Step 1: Write the failing test**

```python
# append to research/tests/test_orderflow_eval.py
from lib.orderflow_eval import quantile_returns


def test_quantile_returns_monotone():
    idx = pd.date_range("2025-01-01", periods=100, freq="15min", tz="UTC")
    factor = pd.Series(np.linspace(-1, 1, 100), index=idx)
    # next-bar return increases with the factor -> top bucket > bottom bucket
    price = pd.Series(100 + np.cumsum(np.linspace(-1, 1, 100)), index=idx)
    q = quantile_returns(factor, price, fwd_bars=1, n_q=5)
    assert q.index.tolist() == [0, 1, 2, 3, 4]
    assert q.iloc[-1] > q.iloc[0]  # monotone increasing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py::test_quantile_returns_monotone -v`
Expected: FAIL — `ImportError: cannot import name 'quantile_returns'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to research/lib/orderflow_eval.py
def quantile_returns(factor: pd.Series, price: pd.Series, fwd_bars: int, n_q: int) -> pd.Series:
    """Mean k-bar-forward return per factor quantile bucket (0..n_q-1)."""
    fwd = _fwd_return(price, fwd_bars)
    df = pd.concat([factor.rename("f"), fwd.rename("r")], axis=1).dropna()
    df["q"] = pd.qcut(df["f"], n_q, labels=False, duplicates="drop")
    return df.groupby("q")["r"].mean()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow_eval.py research/tests/test_orderflow_eval.py
git commit -m "feat(orderflow-eval): quantile_returns metric"
```

- [ ] **Step 6: Write + run the quantile driver**

```python
# research/scripts/of_quantile.py
from pathlib import Path
import pandas as pd
from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import quantile_returns
from lib.okx_data import fetch_candles

INTERVAL = "15m"
of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")
price = fetch_candles("ETH-USDT-SWAP", bar=INTERVAL)["close"].reindex(of.index).ffill()
feats = orderflow_factors(of, of.index, INTERVAL)
for name, f in feats.items():
    print(f"\n== {name} (deciles, 1-bar fwd return, bps) ==")
    q = quantile_returns(f, price, fwd_bars=1, n_q=10) * 1e4
    print(q.round(2).to_string())
    # tails
    qt = quantile_returns(f, price, fwd_bars=1, n_q=100) * 1e4
    print(f"  bottom 1%: {qt.iloc[0]:.2f} bps | top 1%: {qt.iloc[-1]:.2f} bps")
```

Run: `cd research && python scripts/of_quantile.py`

**Decision gate (Task 2):** look for **monotone deciles** (clean linear signal) or **fat tails** (extreme buckets much larger than the linear IC suggests). Either strengthens the case. Flat/non-monotone deciles weaken it.

---

## Task 3: Incremental alpha vs funding_z

**Why:** Even a sub-threshold factor adds portfolio value if it is **orthogonal** to the one factor that already survives (funding_z). Residualize each OF factor on funding_z, then IC the residual — that is the marginal contribution.

**Files:**
- Modify: `research/lib/orderflow_eval.py` (add `incremental_ic`)
- Test: `research/tests/test_orderflow_eval.py` (append)
- Create: `research/scripts/of_incremental.py`

- [ ] **Step 1: Write the failing test**

```python
# append to research/tests/test_orderflow_eval.py
from lib.orderflow_eval import incremental_ic


def test_incremental_ic_removes_shared_component():
    idx = pd.date_range("2025-01-01", periods=200, freq="15min", tz="UTC")
    rng = np.random.default_rng(0)
    control = pd.Series(rng.normal(size=200), index=idx)
    # factor is a pure copy of control -> zero incremental signal
    factor = control.copy()
    price = pd.Series(100 + np.cumsum(control.values), index=idx)  # return ~ control
    inc = incremental_ic(factor, control, price, fwd_bars=1)
    assert abs(inc) < 0.1  # residual carries no independent predictive power
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py::test_incremental_ic_removes_shared_component -v`
Expected: FAIL — `ImportError: cannot import name 'incremental_ic'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to research/lib/orderflow_eval.py
def incremental_ic(factor: pd.Series, control: pd.Series, price: pd.Series, fwd_bars: int) -> float:
    """IC of `factor` residualized on `control` vs forward return."""
    df = pd.concat([factor.rename("f"), control.rename("c")], axis=1).dropna()
    # OLS residual of f on c (+intercept)
    c = df["c"].values
    A = np.vstack([c, np.ones_like(c)]).T
    coef, *_ = np.linalg.lstsq(A, df["f"].values, rcond=None)
    resid = pd.Series(df["f"].values - A @ coef, index=df.index)
    return _ic(resid, _fwd_return(price, fwd_bars))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_orderflow_eval.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow_eval.py research/tests/test_orderflow_eval.py
git commit -m "feat(orderflow-eval): incremental_ic vs control factor"
```

- [ ] **Step 6: Write + run the incremental driver**

```python
# research/scripts/of_incremental.py
from pathlib import Path
import pandas as pd
from lib import orderflow
from lib.orderflow_factors import orderflow_factors
from lib.orderflow_eval import incremental_ic
from lib.okx_data import fetch_candles, fetch_funding_history

INTERVAL = "15m"
of = orderflow.read_cache("eth", INTERVAL, Path("data/orderflow")).to_pandas().set_index("ts")
candles = fetch_candles("ETH-USDT-SWAP", bar=INTERVAL)
price = candles["close"].reindex(of.index).ffill()
# funding_z on the same index (native 8h funding, z-scored — mirror stage0a's transform)
fund = fetch_funding_history("ETH-USDT-SWAP")["funding_rate"].reindex(of.index, method="ffill")
funding_z = (fund - fund.rolling(720).mean()) / fund.rolling(720).std()

feats = orderflow_factors(of, of.index, INTERVAL)
print(f"{'factor':22} {'raw IC@1':>9} {'incremental IC':>15}")
for name, f in feats.items():
    raw = incremental_ic(f, pd.Series(0.0, index=f.index), price, 1)  # control=const -> raw
    inc = incremental_ic(f, funding_z, price, 1)
    print(f"{name:22} {raw:+9.4f} {inc:+15.4f}")
```

Run: `cd research && python scripts/of_incremental.py`

**Decision gate (Task 3):** if incremental IC ≈ raw IC, the factor is orthogonal to funding_z → full portfolio value retained. If incremental collapses toward 0, it was redundant with funding_z.

---

## Task 4 (GATED): Net-of-fee backtest with slippage

**Run only if Tasks 1-3 warrant** (e.g. maker viable, OR clean tails, OR strong incremental alpha). This is the expensive path and the final arbiter.

**Prerequisite — implement the deferred component 5** so order-flow factors enter strategy construction:
- Modify `research/lib/sources.py`: add `binance_orderflow` SourceSpec (`available`), new category `"orderflow"`; extend `FactorCandidate.category` Literal with `"orderflow"`.
- This lets stage0 discovery propose order-flow candidates → stage1/2 build a strategy spec.

**Backtest configuration (critical for microstructure):**
- **Fees:** use the execution regime from Task 1 — maker rebate if half-life ≥ 2 bars, else taker fee. Set in research_config fees.
- **Slippage model:** add a non-zero slippage assumption (microstructure factors "see it but can't fill"). Start with the config's existing slippage and stress 1×/2×/3× via stage3 `--stress`.
- **Strategy archetype:** a short-horizon contrarian entry on the strongest factor (`price_impact` or `trade_count_imbalance`), optionally gated/sized by the orthogonal `large_trade_ratio`.

- [ ] **Step 1:** Implement component 5 (registry + category) with tests (mirror existing source entries; assert `binance_orderflow` is `available` and category validates).
- [ ] **Step 2:** Re-run stage0→1→2→2b→2.5→3 @15m eth so an order-flow strategy spec is built.
- [ ] **Step 3:** Run stage3 `--stress` @15m to get train + OOS × 1×/2×/3× cost. **Decision = worst-of-all net Sharpe > 0 with slippage.**
- [ ] **Step 4:** Read the stress runs; **Go** = net-positive after fees+slippage across stress multipliers → real intraday strategy, consider live paper. **No-Go** = fees eat it → definitively done at the trades-only level; only then weigh paid L2.

---

## Plan Self-Review Notes

- **Spec coverage:** corrects the Phase-1 conclusion per the gemini review (IC gate mis-calibrated for intraday). Tasks 1-3 are the cheap pre-checks gemini flagged (decay→maker/taker, tail/quantile, incremental vs funding_z); Task 4 is the net-of-fee + slippage backtest, gated.
- **Sequencing rationale:** cheapest-and-most-decisive first. Task 1 (decay→maker feasibility) can by itself reframe the whole fee problem before any heavy work.
- **Unverified externals (confirm at run):** `fetch_candles`/`fetch_funding_history` exact signatures + symbol arg (adapt the driver calls to how stage0a calls them); the funding_z transform should mirror stage0a's `apply_ic_eval_transform` for funding (native 8h) rather than the rough rolling(720) used in the driver — if precise parity matters, import stage0a's transform instead.
- **No production-code-without-test:** the three metric functions are TDD'd; the driver scripts are thin orchestration over tested functions (analysis scripts, acceptable without their own tests).
