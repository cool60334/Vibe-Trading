# Stage 3 Interval-Correct Backtest (factor-data namespacing) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stage-3 backtests at 15m/30m read interval-correct factor data so they are *real* sub-hour backtests (not 1H mislabeled), with zero 1H regression.

**Architecture:** One shared `active_manifests_dir()` helper resolves the manifests directory from `RESEARCH_INTERVAL` (`research/manifests` for 1H/unset, `research/manifests/<interval>` for sub-hour). Five hardcoded-root resolvers delegate to it. The backtest runner sets `RESEARCH_INTERVAL` from the run's `config.json` (authoritative per-run source) before executing the signal engine, so the zero-arg `load_factor_values(symbol)` inside generated/curated engines auto-resolves to the right parquet without editing any engine.

**Tech Stack:** Python 3, pandas, pytest. Research code under `research/` (pytest from `research/`); backtest runner under `agent/` (pytest from `agent/`). Two suites run separately.

**Scope:** factor/feature data + strategy-manifest interval namespacing + runner interval propagation. NOT the dashboard read layer / API / frontend selector (separate frontend case — this only completes its write-side prerequisite). NOT the "pure" param-threaded version (deferred). See spec: `docs/superpowers/specs/2026-06-15-stage3-interval-namespacing-design.md`.

---

## File Structure

**Modified**
- `research/lib/timeframe.py` — add `active_manifests_dir()`.
- `research/lib/factor_io.py` — default manifests dir via the helper; `load_factor_values` index-frequency guard.
- `research/factor_extended.py` — `resolve_manifests_dir()` delegates.
- `research/factor_regime.py` — `_resolve_manifests_dir()` delegates.
- `research/pipeline/stage1_factors.py` — `main()` manifests_dir via helper.
- `research/emit_manifest.py` — `main()` manifests_dir via helper.
- `agent/backtest/runner.py` — set `RESEARCH_INTERVAL` from `config.json` before running the engine.

**New tests**
- `research/tests/test_timeframe.py` (append) — `active_manifests_dir`.
- `research/tests/test_factor_io_interval.py` — factor_io default resolution + index guard.
- `research/tests/test_manifests_dir_delegation.py` — the four research resolvers delegate.
- `agent/tests/test_runner_interval_env.py` — runner interval-env helper.

**Zero 1H regression:** the helper returns the bare root for `1H`/unset, identical to today. New behaviour only triggers for sub-hour intervals.

---

## Task 1: `active_manifests_dir()` helper

**Files:**
- Modify: `research/lib/timeframe.py`
- Test: `research/tests/test_timeframe.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_timeframe.py`:

```python
from pathlib import Path

from lib.timeframe import active_manifests_dir


def test_active_manifests_dir_default_is_root(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    p = active_manifests_dir()
    assert p.name == "manifests" and p.parent.name == "research"


def test_active_manifests_dir_1H_is_root(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    assert active_manifests_dir().name == "manifests"


def test_active_manifests_dir_subhour_is_namespaced(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    p = active_manifests_dir()
    assert p.name == "30m" and p.parent.name == "manifests"


def test_active_manifests_dir_invalid_raises(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "7m")
    import pytest
    with pytest.raises(ValueError, match="RESEARCH_INTERVAL"):
        active_manifests_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_timeframe.py -k active_manifests -v`
Expected: FAIL — `ImportError: cannot import name 'active_manifests_dir'`.

- [ ] **Step 3: Write minimal implementation**

Add to the top of `research/lib/timeframe.py` (after `from __future__ import annotations`):

```python
import os
from pathlib import Path

_RESEARCH_DIR = Path(__file__).resolve().parent.parent  # research/
_MANIFESTS_BASE = _RESEARCH_DIR / "manifests"
```

Add at the end of `research/lib/timeframe.py`:

```python
def active_manifests_dir() -> Path:
    """Manifests directory for the active RESEARCH_INTERVAL.

    research/manifests for 1H or unset (zero 1H regression); research/manifests/<interval>
    for a supported sub-hour interval. Raises ValueError for an unsupported value.
    """
    iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
    if not iv or iv == "1H":
        return _MANIFESTS_BASE
    if iv not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"RESEARCH_INTERVAL={iv!r} is not supported; valid: {sorted(SUPPORTED_INTERVALS)}"
        )
    return _MANIFESTS_BASE / iv
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_timeframe.py -k active_manifests -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/timeframe.py research/tests/test_timeframe.py
git commit -m "feat(research): active_manifests_dir helper resolves interval namespace"
```

---

## Task 2: factor_io default dir via helper + index guard

**Files:**
- Modify: `research/lib/factor_io.py`
- Test: `research/tests/test_factor_io_interval.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_factor_io_interval.py
"""factor_io interval-aware default + index guard — run from research/ (pytest tests/)."""
import warnings

import pandas as pd
import pytest

from lib import factor_io


def test_default_dir_follows_research_interval(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    d = factor_io._default_manifests_dir()
    assert d.name == "30m" and d.parent.name == "manifests"


def test_default_dir_root_when_unset(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    assert factor_io._default_manifests_dir().name == "manifests"


def test_load_factor_values_reads_namespaced_dir(monkeypatch, tmp_path):
    # Write a 30m parquet under <base>/30m and confirm the zero-arg load finds it.
    monkeypatch.setattr(factor_io, "_MANIFESTS_BASE_OVERRIDE", tmp_path, raising=False)
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    sub = tmp_path / "30m"
    idx = pd.date_range("2025-01-01", periods=10, freq="30min", tz="UTC")
    df = pd.DataFrame({"mom_4": range(10)}, index=idx, dtype="float64")
    factor_io.dump_factor_values("eth", {"mom_4": df["mom_4"]}, sub)
    out = factor_io.load_factor_values("eth")  # no manifests_dir -> default -> 30m
    assert list(out.columns) == ["mom_4"] and len(out) == 10


def test_load_factor_values_warns_on_index_freq_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(factor_io, "_MANIFESTS_BASE_OVERRIDE", tmp_path, raising=False)
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    sub = tmp_path / "30m"
    idx = pd.date_range("2025-01-01", periods=10, freq="1h", tz="UTC")  # 1H data, 30m expected
    factor_io.dump_factor_values("eth", {"mom_4": pd.Series(range(10), index=idx, dtype="float64")}, sub)
    with pytest.warns(UserWarning, match="index spacing"):
        factor_io.load_factor_values("eth")
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_factor_io_interval.py -v`
Expected: FAIL — `_default_manifests_dir` / `_MANIFESTS_BASE_OVERRIDE` not defined; no warning emitted.

- [ ] **Step 3: Write minimal implementation**

In `research/lib/factor_io.py`, replace the constant block (lines 36-38) with an override-aware resolver:

```python
_LIB_DIR = Path(__file__).resolve().parent       # research/lib/
_RESEARCH_DIR = _LIB_DIR.parent                  # research/
# Test hook: when set (monkeypatched), the manifests base is taken from here
# instead of the active_manifests_dir() helper. None in production.
_MANIFESTS_BASE_OVERRIDE: Path | None = None


def _default_manifests_dir() -> Path:
    """Active manifests dir: interval-namespaced via RESEARCH_INTERVAL.

    Honors a test override (_MANIFESTS_BASE_OVERRIDE / <interval>) when set.
    """
    if _MANIFESTS_BASE_OVERRIDE is not None:
        import os
        iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
        if iv and iv != "1H":
            return _MANIFESTS_BASE_OVERRIDE / iv
        return _MANIFESTS_BASE_OVERRIDE
    from lib.timeframe import active_manifests_dir
    return active_manifests_dir()
```

Replace every `else _DEFAULT_MANIFESTS_DIR` with `else _default_manifests_dir()` in: `load_factor_values` (144), `load_factor_meta` (174), `load_features` (288), `load_features_meta` (317), `load_evidence` (392), `append_feature_column` (437).

Add the index guard inside `load_factor_values`, just before `return pd.read_parquet(...)` (replace that return):

```python
    df = pd.read_parquet(parquet_path, engine="pyarrow")
    _warn_if_index_freq_mismatch(df)
    return df
```

Add the guard helper near the top of the module (after imports):

```python
import os
import warnings


def _warn_if_index_freq_mismatch(df: pd.DataFrame) -> None:
    """Warn if the parquet's median index spacing doesn't match RESEARCH_INTERVAL.

    Catches the silent "stale 1H factors loaded for a 30m run" class of bug.
    """
    iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
    if not iv or len(df) < 3:
        return
    try:
        from lib.timeframe import bars_per_hour
        expected_min = 60 / bars_per_hour(iv)
    except Exception:
        return
    deltas = df.index.to_series().diff().dropna()
    if deltas.empty:
        return
    median_min = deltas.median().total_seconds() / 60.0
    if abs(median_min - expected_min) > 0.5:
        warnings.warn(
            f"factor_values index spacing ~{median_min:.0f}m != expected {expected_min:.0f}m "
            f"for RESEARCH_INTERVAL={iv!r}; loaded a mismatched-interval parquet.",
            UserWarning,
            stacklevel=2,
        )
```

(Leave `dump_factor_values` etc. unchanged — they take an explicit `manifests_dir`.)

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_factor_io_interval.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/factor_io.py research/tests/test_factor_io_interval.py
git commit -m "fix(research): factor_io default dir follows RESEARCH_INTERVAL + index guard"
```

---

## Task 3: factor_extended / factor_regime / stage1 / emit resolvers delegate

**Files:**
- Modify: `research/factor_extended.py` (`resolve_manifests_dir` ~63-71)
- Modify: `research/factor_regime.py` (`_resolve_manifests_dir` ~233)
- Modify: `research/pipeline/stage1_factors.py` (`main` ~191)
- Modify: `research/emit_manifest.py` (`main` ~854)
- Test: `research/tests/test_manifests_dir_delegation.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_manifests_dir_delegation.py
"""All root-resolvers honor RESEARCH_INTERVAL — run from research/ (pytest tests/)."""
import importlib


def test_factor_extended_resolver_namespaced(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    import factor_extended
    importlib.reload(factor_extended)
    assert factor_extended.resolve_manifests_dir().name == "30m"


def test_factor_regime_resolver_namespaced(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")
    import factor_regime
    importlib.reload(factor_regime)
    assert factor_regime._resolve_manifests_dir().name == "15m"


def test_resolvers_root_when_unset(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    import factor_extended
    importlib.reload(factor_extended)
    assert factor_extended.resolve_manifests_dir().name == "manifests"
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `research/`): `python -m pytest tests/test_manifests_dir_delegation.py -v`
Expected: FAIL — resolvers still return the bare root for a sub-hour interval.

- [ ] **Step 3: Write minimal implementation**

`research/factor_extended.py` — replace the body of `resolve_manifests_dir()` (63-71):

```python
def resolve_manifests_dir() -> Path:
    """Return the manifests dir for the active RESEARCH_INTERVAL (interval-namespaced)."""
    from lib.timeframe import active_manifests_dir
    return active_manifests_dir()
```

`research/factor_regime.py` — replace the body of `_resolve_manifests_dir()` (233):

```python
def _resolve_manifests_dir() -> Path:
    from lib.timeframe import active_manifests_dir
    return active_manifests_dir()
```

`research/pipeline/stage1_factors.py` — in `main()` (191), replace:

```python
    from lib.timeframe import active_manifests_dir
    manifests_dir = active_manifests_dir()
```

`research/emit_manifest.py` — in `main()` (854), replace:

```python
    from lib.timeframe import active_manifests_dir
    manifests_dir = active_manifests_dir()
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `research/`): `python -m pytest tests/test_manifests_dir_delegation.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add research/factor_extended.py research/factor_regime.py research/pipeline/stage1_factors.py research/emit_manifest.py research/tests/test_manifests_dir_delegation.py
git commit -m "fix(research): factor/emit resolvers honor RESEARCH_INTERVAL namespace"
```

---

## Task 4: runner sets `RESEARCH_INTERVAL` from config.json

**Files:**
- Modify: `agent/backtest/runner.py` (`main`, after config loaded ~389)
- Test: `agent/tests/test_runner_interval_env.py`

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_runner_interval_env.py
"""Runner propagates the run's interval to RESEARCH_INTERVAL — run from agent/ (pytest tests/)."""
import os

from backtest.runner import _apply_run_interval_env


def test_apply_run_interval_env_sets_from_config(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    iv = _apply_run_interval_env({"interval": "30m"})
    assert iv == "30m"
    assert os.environ["RESEARCH_INTERVAL"] == "30m"


def test_apply_run_interval_env_overrides_stale_shell_value(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")  # stale shell value
    _apply_run_interval_env({"interval": "1H"})
    assert os.environ["RESEARCH_INTERVAL"] == "1H"  # config wins


def test_apply_run_interval_env_defaults_1H(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    _apply_run_interval_env({})
    assert os.environ["RESEARCH_INTERVAL"] == "1H"
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `agent/`): `python -m pytest tests/test_runner_interval_env.py -v`
Expected: FAIL — `ImportError: cannot import name '_apply_run_interval_env'`.

- [ ] **Step 3: Write minimal implementation**

In `agent/backtest/runner.py`, ensure `import os` is present at the top (add if missing). Add the helper near the other module-level helpers:

```python
def _apply_run_interval_env(config: dict) -> str:
    """Set RESEARCH_INTERVAL from the run's config so factor_io (called inside the
    signal engine) resolves the interval-namespaced manifests dir. The run's
    config.json is the authoritative source — this overrides any stale shell value
    and prevents cross-run leakage. Returns the applied interval.
    """
    interval = str(config.get("interval", "1H"))
    os.environ["RESEARCH_INTERVAL"] = interval
    return interval
```

In `main()`, immediately after `codes = config.get("codes", [])` (line 391), add:

```python
    _apply_run_interval_env(config)
```

- [ ] **Step 4: Run test to verify it passes**

Run (from `agent/`): `python -m pytest tests/test_runner_interval_env.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add agent/backtest/runner.py agent/tests/test_runner_interval_env.py
git commit -m "fix(engine): runner sets RESEARCH_INTERVAL from run config for factor_io"
```

---

## Task 5: Audit other factor/feature readers

**Files:** investigation; fix only if a hardcoded-root reader is found.

- [ ] **Step 1: Grep for readers that bypass the default**

Run (from repo root):
```bash
grep -rn "load_features\|load_evidence\|load_factor_values" research/pipeline research/strategies/code | grep -v "test"
```

- [ ] **Step 2: Confirm or fix**

For each call site: if it calls with **no** `manifests_dir` (or `manifests_dir=None`), it now auto-resolves via the interval-aware default — no change needed. If any passes an explicit hardcoded `research/manifests` root, change it to `active_manifests_dir()` (or drop the arg to use the default). Signal engines under `research/strategies/code/**` call `load_factor_values(symbol)` with no dir — leave them; Task 4 makes the env correct at runtime.

- [ ] **Step 3: Commit (only if a fix was needed)**

```bash
git add -A
git commit -m "fix(research): route remaining factor readers through interval-aware default"
```

---

## Task 6: Regression gate

**Files:** none (verification).

- [ ] **Step 1: research suite**

Run (from `research/`): `python -m pytest tests/ -q`
Expected: PASS (pre-existing env failures unrelated to this work may appear — confirm against merge base before treating as regressions).

- [ ] **Step 2: agent suite**

Run (from `agent/`): `python -m pytest tests/ -q`
Expected: PASS (same caveat for pre-existing env/OSError failures).

---

## Task 7: ETH @ 30m end-to-end smoke (operational)

**Files:** none (live verification; not CI). Record outcome in the PR description.

- [ ] **Step 1: Run stage1 → stage3 for ETH at 30m**

Run (PowerShell, from repo root):
```powershell
$env:RESEARCH_INTERVAL = "30m"
$env:RESEARCH_ONLY_SYMBOL = "eth"
cd research
python -m pipeline.stage0a_features
python -m pipeline.stage1_factors
python -m pipeline.stage3_backtest
```

- [ ] **Step 2: Verify it is a real 30m backtest**

Confirm ALL of:
- `research/manifests/30m/factor_values_eth.parquet` exists; its index spacing is 30 minutes (48 bars/day), not hourly.
- The stage-3 run card / config.json shows `interval: "30m"`.
- The backtest fetched 30m candles (bar count ≈ days × 48), and no "index spacing != expected" warning is emitted.
- The strategy manifest lands under `research/manifests/30m/<strategy_id>/manifest.json` (not root).

- [ ] **Step 3: Clean up env + record**

```powershell
Remove-Item Env:RESEARCH_INTERVAL
Remove-Item Env:RESEARCH_ONLY_SYMBOL
```

Record in the PR: ETH@30m Sharpe/trades now from a genuine 30m backtest, vs the earlier 1.71 (which was 1H). If mom_4 reversal holds at true 30m, note it; if it collapses, that is the real finding.

---

## Self-Review Notes

- **Spec coverage:** §3 decision 1 (helper) → Task 1; decision 2 (runner from config) → Task 4; decision 3 (audit readers) → Task 5; decision 5 (index guard) → Task 2; decision 6 (emit namespace) → Task 3; the five resolvers → Tasks 2+3. Decision 4 (stage3 explicit env) intentionally dropped — Task 4's runner-self-set-from-config is the authoritative source and supersedes passing env from stage3.
- **Type consistency:** `active_manifests_dir()` is the single resolver used by factor_io, factor_extended, factor_regime, stage1, emit; `_apply_run_interval_env(config)` returns the applied interval string.
- **Zero 1H regression:** helper returns bare root for 1H/unset; `_apply_run_interval_env` sets "1H" by default; verified by `test_resolvers_root_when_unset`, `test_active_manifests_dir_1H_is_root`, `test_apply_run_interval_env_defaults_1H`.
- **Known follow-up:** dashboard-triggered intraday still needs the pipeline job to carry interval (frontend case). Pure param-threaded version (no env) deferred.
