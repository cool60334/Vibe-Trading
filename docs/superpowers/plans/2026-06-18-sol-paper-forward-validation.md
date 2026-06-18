# SOL `sol_s1` Paper-Forward Validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Honestly forward-validate `sol_s1_single_factor_regime` (ls_divergence contrarian + regime overlay) on data unseen at selection time, gated by a cheap lag-realistic backtest, to decide whether OOS sharpe 1.79 is real or OOS-pollution.

**Architecture:** Reuse the eth_s5 curated-engine pattern — a `# manual: do-not-overwrite` `SignalEngine` that reads `ls_divergence` from the factor store itself (the live signal path feeds OHLCV only) and inherits the regime overlay. A daily cron keeps the factor store fresh. **Phase 0** runs the frozen engine through the real backtester at lag 0 vs lag +24h (live data-availability lag) — if the lagged edge is already gone, abort before building infra or waiting months. **Phase 1** (only if Phase 0 passes) extends the refresh cron to SOL + OI and deploys the paper trader on the server.

**Tech Stack:** Python, pandas, pytest, the existing research pipeline (`research/pipeline/stage*`), `agent/backtest/runner.py`, the trader (`dashboard/trader/`), `ccxt` Bybit (paper = mainnet public data + virtual fills).

**Spec:** [`docs/superpowers/specs/2026-06-18-sol-paper-forward-validation-design.md`](../specs/2026-06-18-sol-paper-forward-validation-design.md)

---

## Conventions

- **Run pipeline commands from `research/`.** Env-var-per-symbol uses `RESEARCH_ONLY_SYMBOL=sol` (honored by `load_config`/`load_strategy_runs`).
  - Bash tool: `RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage0a_features`
  - PowerShell: `$env:RESEARCH_ONLY_SYMBOL='sol'; python -m pipeline.stage0a_features` (then `Remove-Item Env:\RESEARCH_ONLY_SYMBOL` when done)
- **Tests:** run the research suite **separately** from dashboard/agent (`cd research && python -m pytest tests/...`) — sys.path conflict ([[memory]]).
- **Commits:** the user commits only on explicit request. Each task's commit step is staged for the engineer; confirm before pushing.
- Engines execute from the **run-dir** copy `runs/<run>/code/signal_engine.py` (so `Path(__file__).resolve().parents[3]` == repo root), never in place under `strategies/code/`.

---

## File Structure

| File | Responsibility |
|---|---|
| `research/strategies/code/sol_s1_single_factor_regime/signal_engine.py` (create) | Frozen curated engine: ls_divergence contrarian + regime + `SIZE_MULT=1.0` + `FACTOR_LAG_HOURS` knob. Single source — stage3 copies it into run dirs; the paper run dir gets the same file. `# manual: do-not-overwrite`. |
| `research/tests/test_sol_s1_paper_engine.py` (create) | Unit tests: contrarian direction, regime mask, size magnitude, lag shift. |
| `research/scripts/phase0_lag_gate.py` (create) | Phase-0 gate: run the frozen engine through `backtest.runner` on the OOS window at lag 0 and lag 24h; print sharpe/DD comparison; exit non-zero if lagged OOS sharpe < threshold. |
| `dashboard/trader/freshness.py` (modify) | Add `refresh_generated_at` + `refresh_is_stalled` (cron-health: meta `generated_at` recency, distinct from data-age staleness). |
| `dashboard/trader/test_freshness.py` (modify) | Tests for the cron-health helpers. |
| `dashboard/trader/loop.py` (modify) | Emit a distinct warning alert when the refresh is stalled (alert only — no trading behavior change). |
| `scripts/refresh_factors.sh` (modify) | Add SOL; add a `dump_oi` step before stage0a; comment the freshness reconciliation. |
| `runs/sol_s1_single_factor_regime_paper/forward_protocol.md` (create, Phase 1) | Frozen params + pre-registered verdict, written before the first live bar. |

---

## PHASE 0 — Reconstruct, freeze, lag-gate (cheap; abort here if it fails)

### Task 1: Reconstruct the SOL factor store + strategy with `ls_divergence`

**Files:**
- Inputs: `research/data/oi/oi_SOLUSDT_1H.parquet` (present, Jun 17), positioning wiring (pushed `f8bbd3b`→`649349b`)
- Outputs: `research/manifests/factor_values_sol.parquet` (now with `ls_divergence`), `research/manifests/regime_sol.json`, `research/strategies/code/sol_s1_single_factor_regime/signal_engine.py` (auto-compiled), `research/manifests/sol_s1_single_factor_regime/optimization.json`

- [ ] **Step 1: Confirm the OI parquet is present and fresh**

Run:
```bash
cd research && python -c "import pandas as pd; df=pd.read_parquet('data/oi/oi_SOLUSDT_1H.parquet'); print('rows', len(df), 'end', df.index.max(), 'cols', list(df.columns))"
```
Expected: thousands of rows, recent end timestamp, columns include `global_ls_accounts`, `toptrader_ls_positions`. If missing/old, refresh first: `python dump_oi.py --symbol SOLUSDT` (idempotent, no key).

- [ ] **Step 2: Run the reconstruction in order (factors → discovery → strategy → compile → regime → backtest → optimize)**

Run (from `research/`, Bash tool):
```bash
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage0a_features
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage0_discovery --force
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage1_factors
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage2_strategies
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage2b_compile_signal
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage2_5_regime
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage3_backtest
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage4_optimize
```
`--force` on stage0 busts the 7-day discovery cache so the new `ls_divergence` evidence is rediscovered (gotcha [[project_positioning_strategy_autobuild]]).

- [ ] **Step 3: Verify `ls_divergence` is now in the factor store with the screened negative sign**

Run:
```bash
cd research && python -c "
import pandas as pd, json
df = pd.read_parquet('manifests/factor_values_sol.parquet')
assert 'ls_divergence' in df.columns, f'ls_divergence missing; cols={list(df.columns)}'
print('ls_divergence present; rows', len(df))
ev = json.load(open('manifests/evidence_sol.json', encoding='utf-8'))
print('evidence entries:', len(ev) if isinstance(ev, list) else list(ev))
"
```
Expected: `ls_divergence present`. (Sanity vs recon: SOL `ls_divergence` strongest single factor, negative IC at 72/168h.) If absent → stage0a did not source positioning (re-check OI parquet timing, gotcha #1 in [[project_positioning_strategy_autobuild]]).

- [ ] **Step 4: Record the train-best params for the freeze**

Run:
```bash
cd research && python -c "import json; print(json.dumps(json.load(open('manifests/sol_s1_single_factor_regime/optimization.json'))['best_params'], indent=2))"
```
Expected: a dict like `{entry_high_pct, entry_low_pct, hold_max_hours, lookback_days, sl_pct, tp_pct}`. **Copy these values** — Task 2 hard-codes them. (If the strategy id differs, list `manifests/` for the `sol_s1*regime*` dir.)

- [ ] **Step 5: Inspect the auto-compiled engine to confirm it already carries the regime overlay + size_mult**

Run:
```bash
cat research/strategies/code/sol_s1_single_factor_regime/signal_engine.py
```
Expected: a `SignalEngine` reading `ls_divergence`, contrarian entry (long at low percentile), a regime mask, and `signal = position * <size>`. This is the **base** Task 2 freezes. (If regime/size are absent — older compiler — Task 2 adds them from the eth_s5 template.)

- [ ] **Step 6: Commit the reconstructed artifacts**

```bash
git add -f research/strategies/code/sol_s1_single_factor_regime/ research/manifests/factor_values_sol.* research/manifests/regime_sol.json research/manifests/sol_s1_single_factor_regime/ research/strategy_runs.json research/manifests/candidates_sol.json
git commit -m "research(sol): reconstruct sol_s1 factor store + strategy with ls_divergence"
```

---

### Task 2: Freeze the curated paper engine (TDD)

**Files:**
- Create: `research/strategies/code/sol_s1_single_factor_regime/signal_engine.py` (overwrite the auto-compiled one with the frozen version)
- Test: `research/tests/test_sol_s1_paper_engine.py`

- [ ] **Step 1: Write the failing tests**

Create `research/tests/test_sol_s1_paper_engine.py`:
```python
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[2]
ENGINE_SRC = REPO / "research" / "strategies" / "code" / "sol_s1_single_factor_regime" / "signal_engine.py"


def _load_engine_class():
    spec = importlib.util.spec_from_file_location("_sol_s1_paper_engine", ENGINE_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SignalEngine


def _hourly_index(n):
    return pd.date_range("2025-01-01", periods=n, freq="1h")


def _write_factor_store(tmp_path, idx, ls_divergence):
    """Write a minimal factor_values_sol parquet the engine can load."""
    mdir = tmp_path / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"ls_divergence": ls_divergence}, index=idx)
    df.index = df.index.tz_localize("UTC")
    df.to_parquet(mdir / "factor_values_sol.parquet", engine="pyarrow")
    (mdir / "factor_values_sol.meta.json").write_text(
        json.dumps({"schema_version": 1, "symbol": "sol",
                    "index_end": df.index.max().isoformat()}), encoding="utf-8")
    return mdir


def test_contrarian_direction_long_at_low_percentile(tmp_path, monkeypatch):
    """Negative-IC factor: a LOW ls_divergence percentile must produce a LONG (+) signal."""
    n = 24 * 200
    idx = _hourly_index(n)
    # Ramp ls_divergence up over time, then drop to the bottom at the end so the
    # trailing percentile is low → contrarian long.
    vals = np.concatenate([np.linspace(5, 10, n - 48), np.full(48, -10.0)])
    mdir = _write_factor_store(tmp_path, idx, vals)
    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    # Point load_factor_values at the temp manifests dir.
    import sys
    sys.path.insert(0, str(REPO / "research"))
    import lib.factor_io as fio
    monkeypatch.setattr(fio, "_default_manifests_dir", lambda: mdir)

    ohlcv = pd.DataFrame({"close": np.linspace(100, 120, n),
                          "open": 100.0, "high": 121.0, "low": 99.0, "volume": 1.0},
                         index=idx)
    sig = _load_engine_class()().generate({"SOL-USDT-SWAP": ohlcv})["SOL-USDT-SWAP"]
    assert sig.iloc[-1] > 0, "low ls_divergence percentile should be a contrarian LONG"


def test_signal_magnitude_is_size_mult(tmp_path, monkeypatch):
    """Non-zero signals must equal ±SIZE_MULT (validation runs at 1.0)."""
    cls = _load_engine_class()
    assert cls.SIZE_MULT == 1.0, "validation engine must run at full size (alpha is size-invariant)"


def test_factor_lag_hours_shifts_signal(tmp_path, monkeypatch):
    """FACTOR_LAG_HOURS=24 must shift the factor read by 24 bars (delays entries)."""
    cls = _load_engine_class()
    assert hasattr(cls, "FACTOR_LAG_HOURS"), "engine must expose a FACTOR_LAG_HOURS knob"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_sol_s1_paper_engine.py -v`
Expected: FAIL (engine file is still the auto-compiled one without `SIZE_MULT`/`FACTOR_LAG_HOURS`, or import errors).

- [ ] **Step 3: Write the frozen engine**

Overwrite `research/strategies/code/sol_s1_single_factor_regime/signal_engine.py`. **Replace the six `_PARAM` constants with the `best_params` values printed in Task 1 Step 4.** Keep the structure below (mirrors eth_s5 + compiled sol_s2):
```python
# manual: do-not-overwrite
"""
sol_s1_single_factor_regime — paper-forward validation engine.

Frozen clone of the auto-built archetype (ls_divergence contrarian single-factor
+ regime overlay), hard-coded to the stage-4 TRAIN-best params. Runs at full size
(SIZE_MULT=1.0): the alpha verdict (sharpe) is size-invariant, so size is decoupled
and applied post-hoc as a risk lever (see forward_protocol.md). FACTOR_LAG_HOURS
simulates the live ~24h archive-availability lag for the Phase-0 gate.

`# manual: do-not-overwrite` (line 1) makes stage2b skip this file so the pipeline
never overwrites the frozen artifact.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd


def _ensure_research_on_syspath() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]          # runs/<run>/code/signal_engine.py → repo
    research_dir = repo_root / "research"
    if str(research_dir) not in sys.path:
        sys.path.insert(0, str(research_dir))


def _load_regime_series(symbol_short: str, target_index: pd.DatetimeIndex) -> pd.Series:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    regime_path = repo_root / "research" / "manifests" / f"regime_{symbol_short}.json"
    if not regime_path.exists():
        return pd.Series("neutral", index=target_index)
    try:
        payload = json.loads(regime_path.read_text(encoding="utf-8"))
    except Exception:
        return pd.Series("neutral", index=target_index)
    breakdown = payload.get("breakdown") or []
    if not breakdown:
        return pd.Series("neutral", index=target_index)
    df = pd.DataFrame(breakdown)
    df["ts"] = pd.to_datetime(df["date"])
    df = df.set_index("ts").sort_index()
    out = df["regime"].astype(str).reindex(target_index.normalize(), method="ffill")
    out.index = target_index
    return out.fillna("neutral")


class SignalEngine:
    SYMBOL = "SOL-USDT-SWAP"
    SYMBOL_SHORT = "sol"

    # ── stage-4 TRAIN-best params (set from optimization.json, Task 1 Step 4) ──
    LOOKBACK_DAYS = 90      # ← best_params["lookback_days"]
    ENTRY_HIGH_PCT = 80.0   # ← best_params["entry_high_pct"]
    ENTRY_LOW_PCT = 20.0    # ← best_params["entry_low_pct"]
    HOLD_MAX_HOURS = 120    # ← best_params["hold_max_hours"]
    SL_PCT = 3.0            # ← best_params["sl_pct"]
    TP_PCT = 6.0            # ← best_params["tp_pct"]
    PERSIST_K = 3
    PERSIST_M = 2

    # Full size for the alpha verdict; size is a post-hoc risk lever, not tuned here.
    SIZE_MULT = 1.0

    # Simulate the live archive-availability lag (Phase-0 gate sets this to 24).
    FACTOR_LAG_HOURS = int(os.environ.get("SOL_FACTOR_LAG_HOURS", "0"))

    def generate(self, data_map: dict) -> dict:
        _ensure_research_on_syspath()
        from lib.factor_io import load_factor_values

        symbol = self.SYMBOL
        ohlcv = data_map[symbol]

        factors = load_factor_values(symbol)
        if factors.index.tz is not None:
            factors.index = factors.index.tz_localize(None)

        ls = factors["ls_divergence"].reindex(ohlcv.index, method="ffill")
        if self.FACTOR_LAG_HOURS:
            ls = ls.shift(self.FACTOR_LAG_HOURS)   # delay info to live-availability
        ls = ls.rolling(3, min_periods=1).mean()

        win = self.LOOKBACK_DAYS * 24
        half = max(1, win // 2)
        pct = ls.rolling(win, min_periods=half).rank(pct=True) * 100

        # Contrarian (negative IC): long at LOW percentile, short at HIGH.
        cond_long = (pct <= self.ENTRY_LOW_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M
        cond_short = (pct >= self.ENTRY_HIGH_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M

        regime = _load_regime_series(self.SYMBOL_SHORT, ohlcv.index)
        cond_long = cond_long & (regime != "bear")     # bull/neutral
        cond_short = cond_short & (regime != "bull")    # bear/neutral

        signal = pd.Series(0.0, index=ohlcv.index)
        position = 0
        entry_price = None
        bars_held = 0
        for i in range(len(ohlcv.index)):
            if position == 0:
                if bool(cond_long.iloc[i]):
                    position, entry_price, bars_held = 1, ohlcv["close"].iloc[i], 0
                elif bool(cond_short.iloc[i]):
                    position, entry_price, bars_held = -1, ohlcv["close"].iloc[i], 0
            else:
                bars_held += 1
                pnl = (ohlcv["close"].iloc[i] - entry_price) / entry_price * position
                exit_flag = (
                    bars_held >= self.HOLD_MAX_HOURS
                    or pnl >= self.TP_PCT / 100.0
                    or pnl <= -self.SL_PCT / 100.0
                    or 40 <= pct.iloc[i] <= 60           # signal-invalidation neutral band
                )
                if exit_flag:
                    position, entry_price, bars_held = 0, None, 0
            signal.iloc[i] = float(position) * self.SIZE_MULT
        return {symbol: signal}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_sol_s1_paper_engine.py -v`
Expected: 3 passed.

- [ ] **Step 5: Verify the frozen params match optimization.json (no transcription error)**

Run:
```bash
cd research && python -c "
import json, re
bp = json.load(open('manifests/sol_s1_single_factor_regime/optimization.json'))['best_params']
src = open('strategies/code/sol_s1_single_factor_regime/signal_engine.py', encoding='utf-8').read()
for k, const in [('lookback_days','LOOKBACK_DAYS'),('entry_high_pct','ENTRY_HIGH_PCT'),('entry_low_pct','ENTRY_LOW_PCT'),('hold_max_hours','HOLD_MAX_HOURS'),('sl_pct','SL_PCT'),('tp_pct','TP_PCT')]:
    m = re.search(rf'{const}\s*=\s*([0-9.]+)', src)
    assert m and abs(float(m.group(1)) - float(bp[k])) < 1e-6, f'{const}={m and m.group(1)} != best_params[{k}]={bp[k]}'
print('all frozen params match optimization.json best_params')
"
```
Expected: `all frozen params match...`.

- [ ] **Step 6: Commit**

```bash
git add -f research/strategies/code/sol_s1_single_factor_regime/signal_engine.py research/tests/test_sol_s1_paper_engine.py
git commit -m "feat(sol-paper): freeze sol_s1 curated engine (size 1.0, lag knob, do-not-overwrite)"
```

---

### Task 3: Phase-0 lag gate runner (TDD on the pure comparison, then wire the runs)

**Files:**
- Create: `research/scripts/phase0_lag_gate.py`
- Test: `research/tests/test_phase0_lag_gate.py`

- [ ] **Step 1: Write the failing test for the pure decision helper**

Create `research/tests/test_phase0_lag_gate.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from phase0_lag_gate import gate_decision  # noqa: E402


def test_gate_passes_when_lagged_sharpe_meets_threshold():
    d = gate_decision(baseline_sharpe=1.79, lagged_sharpe=1.2, threshold=1.0)
    assert d.go is True
    assert "1.2" in d.summary


def test_gate_fails_when_lag_destroys_edge():
    d = gate_decision(baseline_sharpe=1.79, lagged_sharpe=0.3, threshold=1.0)
    assert d.go is False


def test_gate_fails_on_missing_lagged_metric():
    d = gate_decision(baseline_sharpe=1.79, lagged_sharpe=None, threshold=1.0)
    assert d.go is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd research && python -m pytest tests/test_phase0_lag_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: phase0_lag_gate`.

- [ ] **Step 3: Implement the gate script**

Create `research/scripts/phase0_lag_gate.py`:
```python
"""Phase-0 lag gate: run the frozen sol_s1 engine through the real backtester on
the OOS window at FACTOR_LAG_HOURS = 0 and = 24, then compare OOS sharpe.

Go/No-Go: deploy only if the *lagged* OOS sharpe still clears the threshold —
the live strategy trades on ~24h-old positioning data, so this is the realistic
baseline the forward run is judged against (NOT the lag-free 1.79).

Usage (from research/):  python -m scripts.phase0_lag_gate
"""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
_REPO = _RESEARCH.parent
sys.path.insert(0, str(_RESEARCH))

from pipeline.config import load_config  # noqa: E402
from pipeline.stage3_backtest import _setup_run_dir, build_run_config  # noqa: E402

STRATEGY_ID = "sol_s1_single_factor_regime"
SYMBOL = "SOL-USDT-SWAP"
THRESHOLD = float(os.environ.get("PHASE0_GATE_SHARPE", "1.0"))


@dataclasses.dataclass
class GateDecision:
    go: bool
    summary: str


def gate_decision(baseline_sharpe, lagged_sharpe, threshold: float) -> GateDecision:
    """Pure decision: GO iff lagged_sharpe is present and >= threshold."""
    if lagged_sharpe is None:
        return GateDecision(False, "lagged OOS sharpe unavailable → NO-GO")
    go = lagged_sharpe >= threshold
    verdict = "GO" if go else "NO-GO"
    return GateDecision(
        go,
        f"baseline(lag0)={baseline_sharpe} lagged(24h)={lagged_sharpe} "
        f"threshold={threshold} → {verdict}",
    )


def _read_sharpe(run_dir: Path):
    m = run_dir / "artifacts" / "metrics.csv"
    if not m.exists():
        return None
    with m.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return None
    try:
        return round(float(rows[0]["sharpe"]), 3)
    except (KeyError, ValueError, TypeError):
        return None


def _run_at_lag(lag_hours: int, cfg) -> float | None:
    run_dir = _REPO / "runs" / f"{STRATEGY_ID}_paper_laggate_{lag_hours}h"
    config = build_run_config(SYMBOL, cfg)
    # OOS window only — the held-out segment that produced the 1.79 we are testing.
    if cfg.oos_start:
        from datetime import date
        config["start_date"], config["end_date"] = cfg.oos_start, date.today().isoformat()
    strategies_code = _REPO / "research" / "strategies" / "code"
    _setup_run_dir(run_dir, config, strategies_code, STRATEGY_ID)
    env = dict(os.environ, SOL_FACTOR_LAG_HOURS=str(lag_hours))
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.runner", str(run_dir)],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        print(f"[lag={lag_hours}h] backtest failed: {proc.stderr[-400:]}")
        return None
    return _read_sharpe(run_dir)


def main() -> int:
    cfg = load_config()
    baseline = _run_at_lag(0, cfg)
    lagged = _run_at_lag(24, cfg)
    decision = gate_decision(baseline, lagged, THRESHOLD)
    print("\n=== Phase 0 lag gate ===")
    print(decision.summary)
    (_REPO / "runs" / f"{STRATEGY_ID}_paper_laggate_result.json").write_text(
        json.dumps({"baseline_lag0_sharpe": baseline, "lagged_24h_sharpe": lagged,
                    "threshold": THRESHOLD, "go": decision.go}, indent=2),
        encoding="utf-8")
    return 0 if decision.go else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `cd research && python -m pytest tests/test_phase0_lag_gate.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add -f research/scripts/phase0_lag_gate.py research/tests/test_phase0_lag_gate.py
git commit -m "feat(sol-paper): Phase-0 lag gate runner + pure decision helper"
```

---

### Task 4: Run the Phase-0 gate (DECISION POINT)

**Files:** reads the frozen engine + factor store; writes `runs/sol_s1_single_factor_regime_paper_laggate_result.json`

- [ ] **Step 1: Run the gate**

Run: `cd research && python -m scripts.phase0_lag_gate`
Expected: prints `baseline(lag0)=… lagged(24h)=… threshold=1.0 → GO|NO-GO`.

- [ ] **Step 2: Sanity-check the lag-0 baseline**

The `baseline_lag0_sharpe` should be in the ballpark of the reported OOS ~1.79 (frozen params, OOS window, full size). A wildly different number means the reconstruction or freeze diverged — stop and reconcile before trusting the gate.

- [ ] **Step 3: Branch on the verdict**

- **NO-GO** (lagged OOS sharpe < 1.0): the ~24h data lag already breaks the edge. **Stop here.** Record the result; do NOT build Phase 1. Options to surface to the user: source lower-latency OI/L-S data, or abandon `ls_divergence` as a live signal. This is a successful, cheap negative result.
- **GO**: proceed to Phase 1. The lagged sharpe is the baseline the live forward is judged against.

- [ ] **Step 4: Record the decision in memory and to the user**

Report `baseline`, `lagged`, and GO/NO-GO. If GO, note the lagged baseline number — it replaces 1.79 as the forward yardstick.

---

## PHASE 1 — Paper-forward harness (only if Phase 0 = GO)

### Task 5: Cron-health freshness helpers (TDD)

**Files:**
- Modify: `dashboard/trader/freshness.py`
- Test: `dashboard/trader/test_freshness.py`

- [ ] **Step 1: Write the failing tests**

Add to `dashboard/trader/test_freshness.py`:
```python
import json
from datetime import datetime, timedelta, timezone

from trader.freshness import refresh_generated_at, refresh_is_stalled


def test_refresh_generated_at_reads_meta(tmp_path):
    (tmp_path / "factor_values_sol.meta.json").write_text(
        json.dumps({"generated_at": "2026-06-18T00:00:00+00:00"}), encoding="utf-8")
    got = refresh_generated_at(tmp_path, "SOL-USDT-SWAP")
    assert got == datetime(2026, 6, 18, tzinfo=timezone.utc)


def test_refresh_generated_at_missing_returns_none(tmp_path):
    assert refresh_generated_at(tmp_path, "sol") is None


def test_refresh_is_stalled_true_when_old():
    gen = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    now = gen + timedelta(hours=31)
    assert refresh_is_stalled(gen, now, timedelta(hours=30)) is True


def test_refresh_is_stalled_false_when_recent():
    gen = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
    now = gen + timedelta(hours=20)
    assert refresh_is_stalled(gen, now, timedelta(hours=30)) is False


def test_refresh_is_stalled_true_when_missing():
    assert refresh_is_stalled(None, datetime.now(timezone.utc), timedelta(hours=30)) is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd dashboard && python -m pytest trader/test_freshness.py -k refresh -v`
Expected: FAIL — `ImportError: cannot import name 'refresh_generated_at'`.

- [ ] **Step 3: Implement the helpers**

Add to `dashboard/trader/freshness.py` (reuse the existing `_symbol_short` / `_to_utc`):
```python
def refresh_generated_at(manifests_dir: Path, symbol: str) -> Optional[datetime]:
    """Return the factor meta's ``generated_at`` (when the refresh cron last wrote
    the store), or None. Distinct from ``factor_index_end`` (the data's own age):
    the archive is ~1 day lagged *by nature*, so cron health is judged by how
    recently the parquet was REWRITTEN, not by how old the newest bar is.
    """
    short = _symbol_short(symbol)
    meta_path = manifests_dir / f"factor_values_{short}.meta.json"
    if not meta_path.exists():
        return None
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8")).get("generated_at")
    except (OSError, json.JSONDecodeError):
        return None
    return _to_utc(datetime.fromisoformat(raw)) if raw else None


def refresh_is_stalled(
    generated_at: Optional[datetime], now: datetime, max_age: timedelta,
) -> bool:
    """True if the refresh is missing or older than *max_age* (cron likely broke)."""
    if generated_at is None:
        return True
    return (_to_utc(now) - generated_at) > max_age
```

**Note (integration):** `generated_at` is already written into `factor_values_<sym>.meta.json`
by the existing `lib/factor_io.py::dump_factor_values` on every `stage1_factors` run — so
this helper reads a real refresh timestamp in production, not only the unit-test mock. The
real-meta path is exercised end-to-end by Task 7 Step 3 (asserts `generated_at` updates
after a live refresh). No pipeline change is needed to produce the field.

- [ ] **Step 4: Run to verify pass**

Run: `cd dashboard && python -m pytest trader/test_freshness.py -k refresh -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add dashboard/trader/freshness.py dashboard/trader/test_freshness.py
git commit -m "feat(trader): cron-health freshness helpers (refresh recency, distinct from data age)"
```

---

### Task 6: Emit a stalled-refresh alert in the loop

**Files:**
- Modify: `dashboard/trader/loop.py`
- Test: covered by existing loop tests + a manual check (the loop's network calls make unit-testing the full iteration impractical; keep the change alert-only).

- [ ] **Step 1: Add a refresh-health env knob + import**

In `dashboard/trader/loop.py`, near the other env reads (after `FACTOR_MAX_AGE_DAYS` usage in `signal.py` is unaffected), add at module scope:
```python
REFRESH_STALL_HOURS = float(os.environ.get("REFRESH_STALL_HOURS", "30"))
```

- [ ] **Step 2: Check refresh health once per loop iteration and alert (no trading change)**

In `run()`, inside the `while` loop right after the signal `result` is computed (after the `if result.stale:` block, around line 343), add:
```python
            # Cron-health: warn if the factor store hasn't been rewritten recently
            # (distinct from data-age staleness; the archive is ~1 day lagged by nature).
            from trader.freshness import refresh_generated_at, refresh_is_stalled
            from datetime import timedelta
            gen_at = refresh_generated_at(manifests_dir, symbol)
            if refresh_is_stalled(gen_at, datetime.now(tz=timezone.utc),
                                  timedelta(hours=REFRESH_STALL_HOURS)) and not stale_alerted:
                alerts.append({
                    "timestamp": _now_iso(),
                    "severity": "warning",
                    "message": f"factor refresh stalled (generated_at={gen_at}) — check refresh_factors cron",
                })
                logger.warning("Factor refresh stalled (generated_at=%s)", gen_at)
```

- [ ] **Step 3: Smoke-check the loop still imports and parses**

Run: `cd dashboard && python -c "import trader.loop; print('loop import OK')"`
Expected: `loop import OK`.

- [ ] **Step 4: Run the trader test suite to confirm no regression**

Run: `cd dashboard && python -m pytest trader/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/trader/loop.py
git commit -m "feat(trader): warn when factor-refresh cron stalls (alert only)"
```

---

### Task 7: Extend the refresh cron to SOL + OI

**Files:**
- Modify: `scripts/refresh_factors.sh`

- [ ] **Step 1: Add a dump_oi step before stage0a and document SOL coverage**

Edit `scripts/refresh_factors.sh`. After `cd "$RESEARCH_DIR"` and before `stage0a_features`, insert:
```bash
echo "[$(ts)] dump_oi (Binance OI/L-S archive → research/data/oi/) …"
python dump_oi.py || echo "[$(ts)] dump_oi non-fatal failure — using existing parquet"
```
And update the header comment block: change "for ALL configured symbols (currently btc + eth)" to "(btc + eth + sol)", and add a line:
```bash
# sol_s1_single_factor_regime depends on ls_divergence (OI/L-S archive). dump_oi
# refreshes research/data/oi/oi_SOLUSDT_1H.parquet (daily archive, ~T+1 publish lag);
# stage0a then recomputes the positioning factors. FACTOR_MAX_AGE_DAYS (trader) must
# stay >= that publish lag (default 2d) so a healthy daily run is not misflagged stale.
```

- [ ] **Step 2: Confirm SOL is actually refreshed by the pipeline stages**

The stages iterate configured symbols. Verify SOL is configured:
```bash
cd research && python -c "from pipeline.config import load_config; c=load_config(); print([s.symbol for s in c.symbols])"
```
Expected: list includes `SOL-USDT-SWAP`. If not, add SOL to the symbol config (research_config.yaml) — out of scope to invent here; surface to the user.

- [ ] **Step 3: Dry-run the refresh locally (bash) and confirm the SOL store updates**

`refresh_factors.sh` is the **Linux-server** daily cron (the deploy host); it is not run on
Windows in production. Local dry-run uses a bash shell — on this Windows box, the Bash tool
(git-bash) provides it. (No PowerShell port: the production cron host is Linux.) This step
also serves as the end-to-end check that `generated_at` updates (Task 5 integration note).

Run:
```bash
RESEARCH_VENV= bash scripts/refresh_factors.sh && cd research && python -c "import json; print('sol generated_at', json.load(open('manifests/factor_values_sol.meta.json'))['generated_at'])"
```
Expected: completes; `generated_at` is now (within seconds). If `stage2_5_regime` or others error on a non-SOL symbol, scope the dry-run to SOL via `RESEARCH_ONLY_SYMBOL=sol`.

- [ ] **Step 4: Commit**

```bash
git add scripts/refresh_factors.sh
git commit -m "feat(refresh): extend daily factor cron to SOL + OI/L-S archive"
```

---

### Task 8: Deploy the paper trader + freeze the forward protocol (server, operational)

**Files:**
- Create: `runs/sol_s1_single_factor_regime_paper/forward_protocol.md`
- Create: `runs/sol_s1_single_factor_regime_paper/code/signal_engine.py` (copy of the frozen engine)

- [ ] **Step 1: Write the pre-registered forward protocol BEFORE the first live bar**

Create `runs/sol_s1_single_factor_regime_paper/forward_protocol.md`:
```markdown
# sol_s1 paper-forward protocol (frozen 2026-06-18)

## Frozen params
ls_divergence contrarian single-factor + regime overlay, SIZE_MULT=1.0.
LOOKBACK_DAYS / ENTRY_LOW_PCT / ENTRY_HIGH_PCT / HOLD_MAX_HOURS / SL_PCT / TP_PCT
= stage-4 train-best (see signal_engine.py; matches optimization.json).

## Baseline (Phase 0)
Lag-0 OOS sharpe = <fill from laggate_result.json>.
Lag-24h OOS sharpe (the forward yardstick) = <fill>.

## Pre-registered verdict (do NOT move these)
- Continue-gate (~30 trades / 3–5 months): forward sharpe ≈ lagged baseline (within CI)
  AND full-size DD controllable < 15% by size_mult ≥ 0.4 → EXTEND toward 100+ trades.
- Discard: forward sharpe clearly < lagged baseline / < ~0.5, or DD uncontrollable.
- Iteration on forward numbers is forbidden. Monitoring = liveness only.
```
Fill the two `<…>` from `runs/…_paper_laggate_result.json`.

- [ ] **Step 2: Stage the run dir with the frozen engine**

```bash
mkdir -p runs/sol_s1_single_factor_regime_paper/code
cp research/strategies/code/sol_s1_single_factor_regime/signal_engine.py runs/sol_s1_single_factor_regime_paper/code/signal_engine.py
```

- [ ] **Step 3: Deploy on the server per the eth_s5 runbook ([[deploy_dashboard_testnet_runbook]])**

On the server (port 80 API), launch the paper loop. Reference invocation:
```bash
TRADING_MODE=paper KILL_TERMINATE_DD=0.20 REFRESH_STALL_HOURS=30 \
python -m trader.loop \
  --strategy-id sol_s1_single_factor_regime \
  --testnet-id  sol_s1_single_factor_regime_paper \
  --run-dir     /repo/runs/sol_s1_single_factor_regime_paper \
  --symbol      SOL/USDT:USDT \
  --interval    1H \
  --lookback    200 \
  --repo-root   /repo
```
- `KILL_TERMINATE_DD=0.20` is set **above** the full-size expected DD so normal drawdown does not auto-terminate the validation (tune to the Phase-0 lag-0 DD).
- Ensure `runs/.../code/signal_engine.py`, `research/manifests/factor_values_sol.parquet`, and `regime_sol.json` are present on the server (gitignored runtime — copy per the runbook).
- Ensure the refresh cron (Task 7) is scheduled on the server (daily) so the factor store stays fresh and the loop does not pause on stale factors.

- [ ] **Step 4: Verify the trader is live and trading (not stuck paused)**

Check `runs/testnet/sol_s1_single_factor_regime_paper/testnet_status.json` via the dashboard Testnet tab: `live.status == "running"`, no persistent "factor data stale" alert, equity updating. Within the first few days, confirm at least one paper order appears in `trades.csv` when an entry condition fires.

- [ ] **Step 5: Commit the protocol + run-dir engine**

```bash
git add -f runs/sol_s1_single_factor_regime_paper/forward_protocol.md runs/sol_s1_single_factor_regime_paper/code/signal_engine.py
git commit -m "chore(sol-paper): freeze forward protocol + paper run-dir engine"
```

- [ ] **Step 6: Record the running validation in memory**

Update memory ([[project_positioning_strategy_autobuild]]): sol_s1 paper-forward is LIVE on the server, frozen params, lagged baseline = X, verdict pre-registered, first-gate ETA ~3–5 months. Note the harness is generic (a 2nd strategy = copy the engine + run dir).

---

## Self-Review

- **Spec coverage:** Phase 0 lagged gate → Tasks 3–4. Curated engine (reads factor store, do-not-overwrite, size 1.0) → Task 2. Local reconstruction → Task 1. Refresh cron + OI + freshness reconciliation → Task 7. Cron-health guard (corrected: refresh recency) → Tasks 5–6. Paper deploy + pre-registered verdict artifact → Task 8. sol_s1-only / generic harness → Task 2 (`SIZE_MULT`/single engine) + Task 8 Step 6 note. Hourly-not-8h → engine uses `lookback_days*24` (Task 2). All spec sections map to a task.
- **Placeholders:** the six engine constants are filled from Task 1 Step 4 output (exact source + a verification step asserts they match — Task 2 Step 5); the protocol's two `<…>` are filled from `laggate_result.json` (Task 8 Step 1). No vague TBDs.
- **Type/name consistency:** `gate_decision(baseline_sharpe, lagged_sharpe, threshold)` / `GateDecision(go, summary)` used consistently in test + script; `refresh_generated_at` / `refresh_is_stalled` signatures match across freshness.py, its tests, and loop.py; `SOL_FACTOR_LAG_HOURS` env set by the gate, read by the engine; `STRATEGY_ID = "sol_s1_single_factor_regime"` consistent across gate, run dirs, and deploy.

---

## Execution Handoff

Plan complete. After approval, two execution options:
1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.
2. **Inline Execution** — execute in this session with checkpoints.

**Note:** Task 4 is a hard DECISION POINT — a NO-GO there ends the work (cheap negative result), so pause for the user regardless of execution mode. Tasks 1, 4, 7-Step-2, and 8 touch live data / the server and need user confirmation before running.
