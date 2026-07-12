# Talos Foundry OHLCV Refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reusable, cron-able script that fetches full-span OHLCV per configured symbol and writes `ohlcv_<sym>.parquet` next to the feature store, so a Foundry run can execute against real `research/manifests/`.

**Architecture:** A `research/pipeline/refresh_ohlcv.py` module fetches OKX candles (depth derived from each symbol's feature index), validates coverage with the run's own `orchestrator._align_ohlcv`, and atomically writes the parquet into the same interval-aware manifests dir `load_features` reads. A thin `scripts/refresh_foundry_ohlcv.sh` wraps it for cron.

**Tech Stack:** Python 3.11, pandas/pyarrow, OKX public API (`lib.okx_data`), pytest, bash. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-12-foundry-ohlcv-refresh-design.md`

## Global Constraints

- **No test hits the network.** Every test monkeypatches `fetch_candles`.
- **tz-aware only.** Depth uses `datetime.now(timezone.utc)`, never naive `utcnow()` (the feature index is UTC-aware; `naive - aware` raises `TypeError`).
- **One resolved manifests dir for read AND write** — the default `_REPO_ROOT / cfg.feature_store_path` is already interval-namespaced (`.../15m` under `RESEARCH_INTERVAL`).
- **Coverage gate is the run's own `_align_ohlcv`** (imported), not a hand-rolled 95% check — one source of truth.
- **Per-symbol independence:** one symbol's failure logs and continues; exit 0 iff all succeeded, else 1.
- **Atomic write** via `_atomic_to_parquet` (temp file in the same dir + `os.replace`); old parquet left intact on any failure.
- **Filename** `ohlcv_<short>.parquet` via `_symbol_short`, matching `features_<short>.parquet`.
- **Path bootstrap:** the module adds both repo-root (for `research.hermes.orchestrator`) and `research/` (for bare `pipeline.`/`lib.`) to `sys.path`.
- **Three pytest scopes never mix.** Run research tests from repo root: `python -m pytest research/tests/`.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Never push without being asked.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `research/pipeline/refresh_ohlcv.py` (create) | `refresh_symbol` (fetch+validate+atomic-write one symbol) + `main` (loop config symbols, exit 0/1) | 1 |
| `research/tests/test_refresh_ohlcv.py` (create) | unit tests (mock `fetch_candles`, no network) | 1 |
| `scripts/refresh_foundry_ohlcv.sh` (create) | thin cron wrapper (venv + `python -m`, exit-code passthrough) | 2 |

---

## Task 1: `refresh_ohlcv.py` — module + tests

**Files:**
- Create: `research/pipeline/refresh_ohlcv.py`
- Test: `research/tests/test_refresh_ohlcv.py`

**Interfaces:**
- Consumes: `pipeline.config.load_config`; `lib.okx_data.fetch_candles(symbol, days, bar) -> DataFrame`; `lib.factor_io.load_features(symbol, manifests_dir=None)`, `_atomic_to_parquet(df, path)`, `_symbol_short(symbol) -> str`; `research.hermes.orchestrator._align_ohlcv(ohlcv, feature_index)` (raises `ValueError` if `close` coverage < 95%).
- Produces:
  - `refresh_symbol(sym_cfg, cfg, manifests_dir) -> bool` (uses `sym_cfg.name`, `sym_cfg.okx_swap`, `cfg.interval`).
  - `main(argv=None) -> int` (0 all ok, 1 any failed).
  - `BUFFER_DAYS = 3`.

**Why:** This is the whole deliverable — fetch/validate/write for each configured symbol, with the coverage gate delegated to the run so the two can't drift.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_refresh_ohlcv.py (create)
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ETH = SimpleNamespace(name="eth", okx_swap="ETH-USDT-SWAP")
CFG = SimpleNamespace(interval="1H")


def _write_features(mdir, short="eth", start="2022-06-26", periods=200):
    idx = pd.date_range(start, periods=periods, freq="h", tz="UTC")
    pd.DataFrame({"rsi_14": np.arange(periods, dtype=float)}, index=idx).to_parquet(
        mdir / f"features_{short}.parquet")
    return idx


def _ohlcv_for(idx):
    return pd.DataFrame({c: np.arange(len(idx), dtype=float)
                         for c in ["open", "high", "low", "close", "volume"]}, index=idx)


def test_writes_ohlcv_parquet_next_to_features(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path)
    monkeypatch.setattr(mod, "fetch_candles", lambda sym, days, bar: _ohlcv_for(idx))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is True
    out = pd.read_parquet(tmp_path / "ohlcv_eth.parquet")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.tz is not None


def test_depth_derives_from_features_earliest_tz_aware(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path, start="2022-06-26", periods=100)
    captured = {}
    def fake(sym, days, bar): captured["days"] = days; return _ohlcv_for(idx)
    monkeypatch.setattr(mod, "fetch_candles", fake)
    mod.refresh_symbol(ETH, CFG, tmp_path)         # must not raise TypeError on the subtraction
    assert captured["days"] >= (datetime.now(timezone.utc) - idx.min()).days


def test_gappy_fetch_fails_via_align_ohlcv(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path, periods=200)
    half = idx[:80]                                 # covers 40% of the feature index -> < 95%
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: _ohlcv_for(half))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert not (tmp_path / "ohlcv_eth.parquet").exists()


def test_missing_features_fails_without_fetching(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    called = {"fetch": False}
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: called.__setitem__("fetch", True))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert called["fetch"] is False                # no features -> never fetched


def test_empty_features_fails_before_fetch(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    pd.DataFrame({"rsi_14": pd.Series([], dtype=float)},
                 index=pd.DatetimeIndex([], tz="UTC")).to_parquet(tmp_path / "features_eth.parquet")
    called = {"fetch": False}
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: called.__setitem__("fetch", True))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert called["fetch"] is False                # NaT-guard fires before fetch


def test_fetch_failure_leaves_old_parquet_intact(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path)
    (tmp_path / "ohlcv_eth.parquet").write_bytes(b"OLD")     # sentinel
    def boom(*a, **k): raise RuntimeError("OKX 500")
    monkeypatch.setattr(mod, "fetch_candles", boom)
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert (tmp_path / "ohlcv_eth.parquet").read_bytes() == b"OLD"   # untouched


def test_atomic_write_used(tmp_path, monkeypatch):
    from pathlib import Path
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path)
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: _ohlcv_for(idx))
    seen = []
    real = mod._atomic_to_parquet
    monkeypatch.setattr(mod, "_atomic_to_parquet",
                        lambda df, path: (seen.append(Path(path).name), real(df, path))[1])
    mod.refresh_symbol(ETH, CFG, tmp_path)
    assert seen == ["ohlcv_eth.parquet"]


def test_main_attempts_all_symbols_and_exits_1_on_any_failure(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    cfg = SimpleNamespace(
        symbols=[SimpleNamespace(name="btc", okx_swap="BTC-USDT-SWAP"),
                 SimpleNamespace(name="eth", okx_swap="ETH-USDT-SWAP")],
        feature_store_path="research/manifests", interval="1H")
    monkeypatch.setattr(mod, "load_config", lambda path=None: cfg)
    done = []
    monkeypatch.setattr(mod, "refresh_symbol",
                        lambda s, c, m: (done.append(s.name), s.name == "eth")[1])  # btc fails
    rc = mod.main(["--manifests-dir", str(tmp_path)])
    assert rc == 1 and done == ["btc", "eth"]       # both attempted despite btc failing


def test_only_symbol_env_is_honored_by_load_config(monkeypatch):
    # RESEARCH_ONLY_SYMBOL filtering lives in load_config (reused); verify the reuse.
    monkeypatch.setenv("RESEARCH_ONLY_SYMBOL", "eth")
    import importlib
    import research.pipeline.refresh_ohlcv as mod
    cfg = mod.load_config()
    assert [s.name for s in cfg.symbols] == ["eth"]
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_refresh_ohlcv.py -v`
Expected: FAIL (`research.pipeline.refresh_ohlcv` does not exist).

- [ ] **Step 3: Implement**

```python
# research/pipeline/refresh_ohlcv.py (create)
"""Refresh full-span OHLCV parquets for the Foundry.

Fetches OKX candles per configured symbol, validates coverage against that
symbol's feature index using the run's own _align_ohlcv gate, and atomically
writes ohlcv_<sym>.parquet next to features_<sym>.parquet. Mirrors
refresh_factors: per-symbol independent; exit 0 if all ok, 1 if any failed."""
from __future__ import annotations

import sys
from pathlib import Path

# ── path bootstrap: research/ (bare pipeline.*/lib.*) + repo root (research.hermes.*)
_THIS = Path(__file__).resolve()
_RESEARCH_DIR = _THIS.parents[1]
_REPO_ROOT = _THIS.parents[2]
for _p in (_REPO_ROOT, _RESEARCH_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import logging
from datetime import datetime, timezone

from pipeline.config import load_config
from lib.okx_data import fetch_candles
from lib.factor_io import load_features, _atomic_to_parquet, _symbol_short
from research.hermes.orchestrator import _align_ohlcv

log = logging.getLogger(__name__)
BUFFER_DAYS = 3


def refresh_symbol(sym_cfg, cfg, manifests_dir) -> bool:
    """Fetch full-span OKX candles for one symbol, validate coverage against its
    feature index, and atomically write ohlcv_<short>.parquet. Returns True on
    success. Any failure logs and returns False, leaving the old parquet intact."""
    short = _symbol_short(sym_cfg.name)
    try:
        feats = load_features(sym_cfg.name, manifests_dir=manifests_dir)
    except FileNotFoundError as exc:
        log.error("%s: no features (%s); skipping", short, exc)
        return False
    if feats.empty:
        log.error("%s: features parquet is empty; skipping", short)
        return False
    # both sides tz-aware: naive utcnow() would raise TypeError against the UTC index
    days = (datetime.now(timezone.utc) - feats.index.min()).days + BUFFER_DAYS
    try:
        ohlcv = fetch_candles(sym_cfg.okx_swap, days, bar=cfg.interval)
    except Exception as exc:      # noqa: BLE001 - one symbol's fetch must not sink the rest
        log.error("%s: OKX fetch failed (%s); old parquet left intact", short, exc)
        return False
    try:
        _align_ohlcv(ohlcv, feats.index)          # the run's own gate (close non-NaN >= 95%)
    except ValueError as exc:
        log.error("%s: fetched OHLCV does not cover features (%s); not writing", short, exc)
        return False
    _atomic_to_parquet(ohlcv, Path(manifests_dir) / f"ohlcv_{short}.parquet")
    log.info("%s: wrote ohlcv (%d bars, %s .. %s)", short, len(ohlcv),
             ohlcv.index.min(), ohlcv.index.max())
    return True


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(
        prog="refresh_ohlcv",
        description="Fetch full-span OHLCV per configured symbol for the Foundry")
    ap.add_argument("--config", default=None, help="research_config.yaml path")
    ap.add_argument("--manifests-dir", type=Path, default=None,
                    help="override output dir (default: repo/<cfg.feature_store_path>, interval-aware)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)                # honors RESEARCH_ONLY_SYMBOL / RESEARCH_INTERVAL
    mdir = args.manifests_dir.resolve() if args.manifests_dir is not None \
        else _REPO_ROOT / cfg.feature_store_path
    log.info("refresh_ohlcv: manifests=%s symbols=%s", mdir, [s.name for s in cfg.symbols])

    results = {s.name: refresh_symbol(s, cfg, mdir) for s in cfg.symbols}
    failed = [s for s, ok in results.items() if not ok]
    if failed:
        log.error("refresh_ohlcv: failed symbols: %s", failed)
        return 1
    log.info("refresh_ohlcv: all symbols ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest research/tests/test_refresh_ohlcv.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/refresh_ohlcv.py research/tests/test_refresh_ohlcv.py
git commit -m "feat(research): full-span OHLCV refresh for Foundry real-manifests runs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `scripts/refresh_foundry_ohlcv.sh` cron wrapper

**Files:**
- Create: `scripts/refresh_foundry_ohlcv.sh`

**Interfaces:**
- Consumes: `research.pipeline.refresh_ohlcv` (Task 1) via `python -m`.
- Produces: a cron entry point that passes the module's exit code through.

**Why:** Parity with `scripts/refresh_factors.sh` — the cron/operator entry point. It runs from repo root (so `research.hermes.orchestrator` resolves) and forwards args/exit code.

- [ ] **Step 1: Write the script**

```bash
# scripts/refresh_foundry_ohlcv.sh (create)
#!/usr/bin/env bash
# Refresh full-span OHLCV parquets for the Foundry — ohlcv_<sym>.parquet written
# next to features_<sym>.parquet for every configured symbol. Mirrors
# refresh_factors.sh. Exit 0 on success; non-zero if any symbol failed (old
# parquets left intact).
#
# Optional env:
#   RESEARCH_VENV        path to a venv activate script (…/bin/activate)
#   RESEARCH_ONLY_SYMBOL restrict to one coin (honored by load_config)
#   RESEARCH_INTERVAL    candle interval (default 1H)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "[$(ts)] refresh_foundry_ohlcv: start"

if [[ -n "${RESEARCH_VENV:-}" ]]; then
  # shellcheck disable=SC1090
  source "$RESEARCH_VENV/bin/activate"
fi

cd "$REPO_ROOT"           # repo root on path so research.hermes.orchestrator resolves
rc=0
python -m research.pipeline.refresh_ohlcv "$@" || rc=$?

echo "[$(ts)] refresh_foundry_ohlcv: done (exit $rc)"
exit "$rc"
```

- [ ] **Step 2: Verify the script parses**

Run: `bash -n scripts/refresh_foundry_ohlcv.sh`
Expected: no output, exit 0 (syntax OK). The wrapper's logic is exercised by Task 1's tests (it only forwards to the tested module); the real network run is the connectivity proof, not a unit test.

- [ ] **Step 3: Make it executable + commit**

```bash
chmod +x scripts/refresh_foundry_ohlcv.sh
git add scripts/refresh_foundry_ohlcv.sh
git commit -m "feat(scripts): cron wrapper for the Foundry OHLCV refresh

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §N1 refresh module (features-derived depth, tz-aware, empty guard, `_align_ohlcv` reuse, `_symbol_short` filename, atomic same-dir write, per-symbol exit 0/1) → Task 1. ✅
- §N2 cron wrapper → Task 2. ✅
- §3 error table: missing features (`test_missing_features_fails_without_fetching`), empty features (`test_empty_features_fails_before_fetch`), fetch raise (`test_fetch_failure_leaves_old_parquet_intact`), coverage fail via `_align_ohlcv` (`test_gappy_fetch_fails_via_align_ohlcv`), all-ok write (`test_writes_ohlcv_parquet_next_to_features`), `RESEARCH_ONLY_SYMBOL` (`test_only_symbol_env_is_honored_by_load_config`). ✅
- §4 test matrix: every row mapped, plus the tz-aware assertion folded into `test_depth_derives_from_features_earliest_tz_aware`. ✅
- Non-goals respected: no incremental, no staleness gate, no new source. ✅

**Placeholder scan:** none. `BUFFER_DAYS = 3` is concrete; the coverage threshold lives in `_align_ohlcv` (reused, not restated).

**Type consistency:** `refresh_symbol(sym_cfg, cfg, manifests_dir) -> bool` and `main(argv=None) -> int` identical across defs, tests, and the wrapper's `python -m` call. `fetch_candles(symbol, days, bar)` matches `lib.okx_data`. `_align_ohlcv(ohlcv, feats.index)` matches `orchestrator`. `_symbol_short`/`_atomic_to_parquet`/`load_features` match `factor_io`. The bootstrap adds repo-root + research/ exactly as verified.

**Known deferred (documented in spec):** the live OKX endpoint is proven only by a real run, not by the (mocked) tests.
