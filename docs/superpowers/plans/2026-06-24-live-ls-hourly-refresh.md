# Live L/S hourly refresh (Phase-1, SOL) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep `research/manifests/factor_values_sol.parquet` fresh every hour so the live trader's freshness guard stops refusing `sol_s1` trades, with all manifest writes funneled through the existing serial `pipeline_manager`.

**Architecture:** A scheduler sidecar enqueues an hourly `live_refresh` pipeline job (steps `0a`→`1`) that `pipeline_manager` runs serially. `stage0a`, when `LIVE_OI_REFRESH=1`, runs `dump_oi --live` for its symbol first. `factor_io` parquet/meta writes are made atomic to remove a cross-container read race against the trader.

**Tech Stack:** Python 3.11, pandas/pyarrow, FastAPI server + standalone `pipeline_manager` (subprocess runner), Docker Compose. Tests: pytest (research root and dashboard/server root run separately).

---

## Test invocation (read first)

- **Research tests:** `cd research && python -m pytest tests/<file>.py -v`
- **Dashboard server tests:** `cd dashboard/server && python -m pytest <file>.py -v`
- Never run both roots in one pytest process (separate sys.path roots).

## File structure

- Modify `research/lib/factor_io.py` — add atomic write helpers; use in `dump_factor_values` + `dump_features`.
- Modify `research/pipeline/stage0a_features.py` — add `LIVE_OI_REFRESH`-gated live OI dump in `_process_symbol`.
- Modify `dashboard/server/pipeline_jobs.py` — add `live_refresh` kind; make `write_job` atomic.
- Modify `dashboard/server/pipeline_manager.py` — set `LIVE_OI_REFRESH` on a `live_refresh` job's `0a` step; prioritize `live_refresh` in `_oldest_queued`.
- Create `dashboard/server/freshness_scheduler.py` — `tick()` + loop.
- Create `dashboard/server/test_freshness_scheduler.py` — scheduler tests.
- Modify `dashboard/docker-compose.pipeline.yml` — add `freshness-scheduler` service.

---

## Task 1: Atomic writes in factor_io

**Files:**
- Modify: `research/lib/factor_io.py`
- Test: `research/tests/test_factor_io.py`

- [ ] **Step 1: Write the failing test**

Add **both** tests to `research/tests/test_factor_io.py`. The first is the strictly-failing driver; the second is the atomicity invariant (a regression guard that must hold after Step 3):

```python
def test_atomic_helpers_exist():
    from lib import factor_io
    assert hasattr(factor_io, "_atomic_to_parquet")
    assert hasattr(factor_io, "_atomic_write_text")


def test_dump_factor_values_atomic_no_temp_left(tmp_path):
    import pandas as pd
    from lib.factor_io import dump_factor_values
    idx = pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")
    series = {"f1": pd.Series(range(5), index=idx, dtype="float64")}
    dump_factor_values("sol", series, tmp_path)
    # parquet + meta written, NO leftover temp files
    assert (tmp_path / "factor_values_sol.parquet").exists()
    assert (tmp_path / "factor_values_sol.meta.json").exists()
    leftovers = list(tmp_path.glob("*.tmp")) + list(tmp_path.glob("*.tmp.*"))
    assert leftovers == [], f"temp files left behind: {leftovers}"
    # content round-trips
    df = pd.read_parquet(tmp_path / "factor_values_sol.parquet")
    assert list(df.columns) == ["f1"]
    assert len(df) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_factor_io.py::test_atomic_helpers_exist -v`
Expected: FAIL with `AttributeError` (helpers not defined yet).

- [ ] **Step 3: Write minimal implementation**

In `research/lib/factor_io.py`, add `import os` and `import tempfile` to the imports, then add module-level helpers (place above `dump_factor_values`):

```python
def _atomic_to_parquet(df: "pd.DataFrame", path: Path) -> None:
    """Write a parquet file atomically: write to a temp file in the same
    directory, then os.replace onto the target so a concurrent reader (the
    separate trader container) never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".parquet.tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        df.to_parquet(tmp_path, engine="pyarrow", compression="snappy")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (temp file + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
```

Then in `dump_factor_values`, replace:

```python
    df.to_parquet(parquet_path, engine="pyarrow", compression="snappy")
```
with
```python
    _atomic_to_parquet(df, parquet_path)
```

and replace:

```python
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
```
with
```python
    _atomic_write_text(meta_path, json.dumps(meta, indent=2))
```

Apply the **same two replacements** in `dump_features` (its `df.to_parquet(...)` and `meta_path.write_text(...)`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_factor_io.py tests/test_factor_io_features.py -v`
Expected: PASS (all existing round-trip tests + the new ones).

- [ ] **Step 5: Commit**

```bash
git add research/lib/factor_io.py research/tests/test_factor_io.py
git commit -m "fix(factor_io): atomic parquet/meta writes (cross-container read race)"
```

---

## Task 2: stage0a LIVE_OI_REFRESH-gated live dump

**Files:**
- Modify: `research/pipeline/stage0a_features.py` (helper + call in `_process_symbol` before section 3c, ~line 555)
- Test: `research/tests/test_stage0a_features.py`

- [ ] **Step 1: Write the failing test**

Add to `research/tests/test_stage0a_features.py`:

```python
def test_maybe_live_oi_refresh_gated_by_env(monkeypatch):
    import pipeline.stage0a_features as s0a

    calls = []
    monkeypatch.setattr(s0a, "_run_live_oi_dump", lambda sym: calls.append(sym))

    class _Cfg:
        name = "sol"
        binance_usdt = "SOLUSDT"

    # env unset -> no dump
    monkeypatch.delenv("LIVE_OI_REFRESH", raising=False)
    s0a._maybe_live_oi_refresh(_Cfg())
    assert calls == []

    # env set -> dump for this symbol
    monkeypatch.setenv("LIVE_OI_REFRESH", "1")
    s0a._maybe_live_oi_refresh(_Cfg())
    assert calls == ["SOLUSDT"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_stage0a_features.py::test_maybe_live_oi_refresh_gated_by_env -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_maybe_live_oi_refresh'`.

- [ ] **Step 3: Write minimal implementation**

In `research/pipeline/stage0a_features.py`, add `import os` if not already imported, then add two module-level functions (place near the other `_` helpers, above `_process_symbol`):

```python
def _env_truthy(val: "str | None") -> bool:
    return val is not None and val.strip().lower() not in ("", "0", "false", "no", "off")


def _run_live_oi_dump(binance_symbol: str) -> None:
    """Overlay a fresh Binance L/S tail onto the OI cache for one symbol.
    Imported lazily so non-live runs never touch dump_oi. dump_oi is fail-soft
    on the live tail; any hard error here is caught by the caller."""
    from dump_oi import main as dump_oi_main
    dump_oi_main(["--live", "--symbols", binance_symbol])


def _maybe_live_oi_refresh(sym_cfg) -> None:
    """When LIVE_OI_REFRESH is set, refresh this symbol's OI cache (live tail)
    before features are built. Fail-soft: on any error the feature build proceeds
    on the existing cache; the trader's index_end freshness guard then governs
    whether it is fresh enough to trade."""
    if not _env_truthy(os.environ.get("LIVE_OI_REFRESH")):
        return
    try:
        _run_live_oi_dump(sym_cfg.binance_usdt)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: live OI refresh failed: %s — using existing OI cache", sym_cfg.name, exc)
```

Then in `_process_symbol`, immediately **before** section "── 3c. Binance OI/L-S positioning cache" (the `oi_ls_df = None` / `oi_metrics.load_oi_parquet(...)` block, ~line 555), insert:

```python
        # ── 3b2. Optional live OI refresh (hourly L/S Phase-1) ───────────────
        _maybe_live_oi_refresh(sym_cfg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_stage0a_features.py -v`
Expected: PASS (new test + existing).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_features.py
git commit -m "feat(stage0a): LIVE_OI_REFRESH-gated live OI dump before feature build"
```

---

## Task 3: pipeline_jobs — live_refresh kind + atomic write_job

**Files:**
- Modify: `dashboard/server/pipeline_jobs.py` (`create_job` ~line 116, `write_job` ~line 91, imports)
- Test: `dashboard/server/test_pipeline_jobs.py`

- [ ] **Step 1: Write the failing test**

Add to `dashboard/server/test_pipeline_jobs.py`:

```python
def test_create_live_refresh_job(tmp_path):
    import pipeline_jobs as pj
    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol", interval="1H")
    assert job["kind"] == "live_refresh"
    assert job["symbol"] == "sol"
    assert [s["stage"] for s in job["steps"]] == ["0a", "1"]
    assert job["status"] == "queued"
    assert job["stress"] is False


def test_write_job_atomic_no_temp_left(tmp_path):
    import pipeline_jobs as pj
    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    job_dir = pj.jobs_dir(tmp_path) / job["job_id"]
    leftovers = list(job_dir.glob("*.tmp")) + list(job_dir.glob("*.tmp.*"))
    assert leftovers == []
    assert pj.read_job(tmp_path, job["job_id"])["symbol"] == "sol"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && python -m pytest test_pipeline_jobs.py::test_create_live_refresh_job -v`
Expected: FAIL with `ValueError: invalid kind 'live_refresh'`.

- [ ] **Step 3: Write minimal implementation**

In `dashboard/server/pipeline_jobs.py`:

(a) Add `import os` to the imports.

(b) In `create_job`, add a branch before the `else: raise ValueError`:

```python
    elif kind == "live_refresh":
        stage = None
        steps = [{"stage": s, "status": "pending", "exit_code": None}
                 for s in ("0a", "1")]
```

(c) Make `write_job` atomic — replace its body:

```python
def write_job(repo_root, job: dict) -> None:
    p = _job_path(repo_root, job["job_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    os.replace(tmp, p)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && python -m pytest test_pipeline_jobs.py -v`
Expected: PASS (new tests + existing).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/pipeline_jobs.py dashboard/server/test_pipeline_jobs.py
git commit -m "feat(pipeline_jobs): live_refresh job kind + atomic write_job"
```

---

## Task 4: pipeline_manager — LIVE_OI_REFRESH env + live_refresh priority

**Files:**
- Modify: `dashboard/server/pipeline_manager.py` (`_default_runner` ~line 31, `execute_job` runner call ~line 113, `_oldest_queued` ~line 77)
- Test: `dashboard/server/test_pipeline_manager.py`

- [ ] **Step 1: Write the failing test**

Add to `dashboard/server/test_pipeline_manager.py`:

```python
def test_oldest_queued_prioritizes_live_refresh(tmp_path):
    import pipeline_jobs as pj
    from pipeline_manager import Manager
    # research job created FIRST (older), live_refresh SECOND (newer)
    pj.create_job(tmp_path, kind="pipeline", symbol="sol")
    live = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    mgr = Manager(tmp_path, runner=lambda *a, **k: 0)
    picked = mgr._oldest_queued()
    assert picked["job_id"] == live["job_id"]


def test_live_refresh_sets_env_on_0a_step(tmp_path):
    import pipeline_jobs as pj
    from pipeline_manager import Manager
    seen = {}

    def fake_runner(repo_root, stage_id, symbol, log_fp, stress=False,
                    interval="1H", live_refresh=False):
        seen[stage_id] = live_refresh
        return 0

    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    mgr = Manager(tmp_path, runner=fake_runner)
    mgr.execute_job(job)
    assert seen.get("0a") is True
    assert seen.get("1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && python -m pytest test_pipeline_manager.py::test_oldest_queued_prioritizes_live_refresh test_pipeline_manager.py::test_live_refresh_sets_env_on_0a_step -v`
Expected: FAIL — first test fails (no priority, returns the older research job); second fails (`live_refresh` kwarg not passed).

- [ ] **Step 3: Write minimal implementation**

In `dashboard/server/pipeline_manager.py`:

(a) `_default_runner` — add a `live_refresh` parameter and set the env:

```python
def _default_runner(repo_root: Path, stage_id: str, symbol: Optional[str], log_fp,
                    stress: bool = False, interval: str = "1H",
                    live_refresh: bool = False) -> int:
```

Inside it, after the `RESEARCH_INTERVAL` block, add:

```python
    if live_refresh and stage_id == "0a":
        env["LIVE_OI_REFRESH"] = "1"
    else:
        env.pop("LIVE_OI_REFRESH", None)
```

(b) `_oldest_queued` — prioritize `live_refresh`:

```python
    def _oldest_queued(self) -> Optional[dict]:
        queued = [j for j in pj.list_jobs(self.repo_root, limit=10000)
                  if j.get("status") == "queued"]
        # live_refresh jobs jump ahead of research jobs so the hourly factor
        # refresh never starves behind a manually-queued full pipeline.
        queued.sort(key=lambda j: (0 if j.get("kind") == "live_refresh" else 1,
                                   j.get("created_at", "")))
        return queued[0] if queued else None
```

(c) `execute_job` — pass the flag to the runner. Replace the `rc = self._runner(...)` call with:

```python
                rc = self._runner(
                    self.repo_root, step["stage"], step_symbol, fp,
                    job.get("stress", False), job.get("interval", "1H"),
                    job.get("kind") == "live_refresh",
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && python -m pytest test_pipeline_manager.py -v`
Expected: PASS (new tests + existing).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/pipeline_manager.py dashboard/server/test_pipeline_manager.py
git commit -m "feat(pipeline_manager): LIVE_OI_REFRESH on 0a step + live_refresh queue priority"
```

---

## Task 5: freshness_scheduler

**Files:**
- Create: `dashboard/server/freshness_scheduler.py`
- Test: `dashboard/server/test_freshness_scheduler.py`

- [ ] **Step 1: Write the failing test**

Create `dashboard/server/test_freshness_scheduler.py`:

```python
from datetime import datetime, timezone, timedelta

import pipeline_jobs as pj
import freshness_scheduler as fs


def test_tick_enqueues_when_none_active(tmp_path):
    now = datetime.now(timezone.utc)
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)
    assert len(ids) == 1
    jobs = pj.list_jobs(tmp_path, limit=10)
    assert jobs[0]["kind"] == "live_refresh"
    assert jobs[0]["symbol"] == "sol"


def test_tick_skips_when_active_exists(tmp_path):
    now = datetime.now(timezone.utc)
    fs.tick(tmp_path, ["sol"], now, interval_sec=3600)      # enqueue one
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)  # second tick
    assert ids == []  # queued job still active -> no duplicate


def test_tick_reenqueues_when_active_is_zombie(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    # create a stale 'running' job by hand
    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    job["status"] = "running"
    job["created_at"] = old.isoformat()
    pj.write_job(tmp_path, job)
    now = datetime.now(timezone.utc)
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)  # >2*interval old
    assert len(ids) == 1  # zombie ignored -> fresh job enqueued
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && python -m pytest test_freshness_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'freshness_scheduler'`.

- [ ] **Step 3: Write minimal implementation**

Create `dashboard/server/freshness_scheduler.py`:

```python
"""Hourly L/S factor-refresh scheduler (Phase-1).

Enqueues a ``live_refresh`` pipeline job for each configured symbol when none is
active; the decoupled pipeline_manager executes it serially. Writes only — never
runs stages itself.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pipeline_jobs as pj

logger = logging.getLogger("pipeline.freshness")


def _active_live_refresh(repo_root, symbol: str, now: datetime,
                         max_age_sec: float) -> Optional[dict]:
    """Return an active (queued/running) live_refresh job for *symbol*, ignoring
    zombies older than *max_age_sec* so a stuck job cannot block enqueues."""
    for j in pj.list_jobs(repo_root, limit=10000):
        if j.get("kind") != "live_refresh" or j.get("symbol") != symbol:
            continue
        if j.get("status") not in ("queued", "running"):
            continue
        created = j.get("created_at")
        if created:
            age = (now - datetime.fromisoformat(created)).total_seconds()
            if age > max_age_sec:
                continue  # zombie — treat as dead
        return j
    return None


def tick(repo_root, symbols: list[str], now: datetime,
         interval_sec: float) -> list[str]:
    """One scheduling pass. Returns the job_ids enqueued this pass."""
    enqueued: list[str] = []
    for sym in symbols:
        if _active_live_refresh(repo_root, sym, now, 2 * interval_sec) is None:
            job = pj.create_job(repo_root, kind="live_refresh", symbol=sym,
                                interval="1H")
            enqueued.append(job["job_id"])
    return enqueued


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    repo_root = Path(os.environ.get("REPO_ROOT", "/repo"))
    symbols = [s.strip() for s in os.environ.get("FRESHNESS_SYMBOLS", "sol").split(",") if s.strip()]
    interval_sec = float(os.environ.get("FRESHNESS_INTERVAL_SEC", "3600"))
    logger.info("freshness_scheduler start: symbols=%s interval=%ss", symbols, interval_sec)
    while True:
        now = datetime.now(timezone.utc)
        try:
            ids = tick(repo_root, symbols, now, interval_sec)
            if ids:
                logger.info("enqueued live_refresh jobs: %s", ids)
        except Exception:  # noqa: BLE001
            logger.exception("freshness tick failed")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && python -m pytest test_freshness_scheduler.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/freshness_scheduler.py dashboard/server/test_freshness_scheduler.py
git commit -m "feat(freshness): hourly live_refresh scheduler (tick + loop)"
```

---

## Task 6: Compose service + deploy-config check

**Files:**
- Modify: `dashboard/docker-compose.pipeline.yml`

- [ ] **Step 1: Add the scheduler service**

Append under `services:` in `dashboard/docker-compose.pipeline.yml` (sibling of `pipeline-runner`):

```yaml
  freshness-scheduler:
    build:
      context: ..
      dockerfile: dashboard/Dockerfile.pipeline
    command: ["python", "-m", "freshness_scheduler"]
    environment:
      REPO_ROOT: /repo
      FRESHNESS_SYMBOLS: sol
      FRESHNESS_INTERVAL_SEC: "3600"
    volumes:
      - ..:/repo
      - ../runs:/repo/runs
    restart: unless-stopped
```

- [ ] **Step 2: Validate compose syntax**

Run: `cd dashboard && docker compose -f docker-compose.pipeline.yml config`
Expected: prints the merged config with both `pipeline-runner` and `freshness-scheduler`, no errors.

- [ ] **Step 3: Verify the scheduler module imports in the image command**

Run: `cd dashboard && docker compose -f docker-compose.pipeline.yml run --rm freshness-scheduler python -c "import freshness_scheduler; print('ok')"`
Expected: prints `ok` (confirms `pipeline_jobs` importable from the image workdir).

- [ ] **Step 4: Commit**

```bash
git add dashboard/docker-compose.pipeline.yml
git commit -m "feat(deploy): freshness-scheduler sidecar in pipeline compose"
```

- [ ] **Step 5: Deploy-config check (no code, document)**

Confirm the live `sol_s1` `control.json` on the server sets `FACTOR_MAX_AGE_DAYS=0.5` (12h) under its per-strategy `env` block (see `project_sol_paper_forward_phase0` / `dashboard/trader/manager.py:86`). If absent, add it so the trader's `index_end` guard bites before the ~12–16h edge half-life. Note this in `deploy_dashboard_testnet_runbook` memory.

---

## Final verification

- [ ] Research suite: `cd research && python -m pytest tests/test_factor_io.py tests/test_factor_io_features.py tests/test_stage0a_features.py -v` → all pass.
- [ ] Server suite: `cd dashboard/server && python -m pytest test_pipeline_jobs.py test_pipeline_manager.py test_freshness_scheduler.py -v` → all pass.
- [ ] `docker compose -f dashboard/docker-compose.pipeline.yml config` → valid.

## Deployment note

On the server (in repo root): `git pull`, then
`cd dashboard && docker compose -f docker-compose.pipeline.yml up -d --build`
to (re)build `pipeline-runner` + start `freshness-scheduler`. The dashboard
(`docker-compose.yml`) and trader (`docker-compose.trader.yml`) are untouched, so
the running paper-forward trader is not interrupted. Watch:
`docker compose -f docker-compose.pipeline.yml logs -f freshness-scheduler`
then confirm `live_refresh` jobs appear in the dashboard JobsPanel and
`research/manifests/factor_values_sol.meta.json` `generated_at` advances hourly.
```
