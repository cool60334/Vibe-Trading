# Foundry Auto-Scheduler (斷点 A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `Foundry → bridge → pipeline` unattended on a cadence via two decoupled schedulers, so Foundry (paid, off the critical path) mines candidates while the pipeline runs on `library ∪ current-overlay` and never starves.

**Architecture:** Two thin write-only schedulers (`foundry_miner_scheduler`, `discovery_pipeline_scheduler`) enqueue `foundry_mine` / `discovery_pipeline` jobs; the existing serial `pipeline_manager` runs both kinds as steps (Foundry/bridge are command steps, then 0a→5 stages). Spend is bounded by a shared daily ledger + kill-switch files; the bridge soft-fails so no-docker never starves the library pipeline.

**Tech Stack:** Python 3.11, pytest, subprocess, Docker (Foundry/bridge), pandas.

## Global Constraints

- Python for every command: `.venv/Scripts/python.exe` from repo root.
- **Two pytest scopes — never mix.** Tasks 1–3 are research: `.venv/Scripts/python.exe -m pytest research/tests/ -q`. Tasks 4–8 are dashboard: `cd dashboard/server && pytest -q`.
- **Production safety (from B):** never write `features_<sym>.parquet` / `factor_values_<sym>.parquet`. Foundry strategies stay blocked from live by B's promote guard.
- **Secrets never serialised:** the LLM API key is read by the foundry subprocess from `agent/.env` (dotenv); it is never a CLI flag, never in a job payload, never in the config snapshot.
- **Two distinct dirs:** `FOUNDRY_RUNS_DIR` (default `runs/foundry_auto/`) is stable+shared (ledger `foundry_spend.jsonl` + `foundry.lock` live here → global daily cap). The overlay dir is per-job (`runs/pipeline_jobs/<job_id>/`).
- Exit codes: `0` = ok/continue; `EXIT_PAUSED = 201` = killswitch-stopped (job `paused`); other non-zero = failure. Bridge infra failure → empty overlay + exit `0` (soft-fail).
- Kill-switch files: `FOUNDRY_PAUSE_FILE` (default `runs/foundry_auto.pause`, money only) and `GLOBAL_PAUSE_FILE` (default `runs/discovery_global.pause`, everything).

## Existing code you will use

```python
# research/hermes/foundry_runner.py  main(): `run` subcommand parses args, takes single_instance_lock,
#   resolve_image_id, build_llm, reconcile_foundry_jobs(...), record_spend in finally, returns 0/2.
# research/hermes/orchestrator.py  run_foundry(...): hypotheses loop; between hypotheses it checks
#   should_early_stop / BudgetExhausted and builds `summary` (dict). run_foundry_job wraps run_foundry.
# research/hermes/foundry_bridge.py  main(): builds DockerSandbox via resolve_image_id, build_overlay,
#   write_overlay; recompute_full_span(code, panel, run_sandbox); build_overlay(...cap=...).
# dashboard/server/pipeline_jobs.py  create_job(repo_root, kind, stage=None, symbol=None, stress=False,
#   interval="1H") -> dict; job has steps=[{"stage","status","exit_code"}], plus cancel/status fields.
#   pipeline_sequence(), stage_command(stage_id), list_jobs, write_job, read_job, log_path, _now, jobs_dir.
# dashboard/server/pipeline_manager.py  Manager.execute_job (step loop, rc handling, finally cleanup_overlay);
#   _default_runner(repo_root, stage_id, symbol, log_fp, stress, interval, live_refresh) -> int;
#   _oldest_queued() (sorts live_refresh ahead); stage_env(overrides); cleanup_overlay(job_dir).
# dashboard/server/freshness_scheduler.py  tick()+main() write-only template.
```

---

### Task 1: Foundry `--pause-file` + killswitch between hypotheses + exit 201

**Files:**
- Modify: `research/hermes/orchestrator.py` (`run_foundry` hypotheses loop; `run_foundry_job` passthrough)
- Modify: `research/hermes/foundry_runner.py` (`run` subcommand: `--pause-file`, return 201)
- Test: `research/tests/test_hermes_foundry_killswitch.py`

**Interfaces:**
- Produces: `run_foundry(..., pause_file=None)` and `run_foundry_job(..., pause_file=None)` accept an optional pause-file path; the returned `summary` gains `"killswitch_paused": True` when it stops for the pause file. `foundry_runner` exposes `EXIT_PAUSED = 201`.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_hermes_foundry_killswitch.py`:

```python
import numpy as np
import pandas as pd
from research.hermes import orchestrator as orch
from research.hermes.orchestrator import run_foundry, Budget
from research.hermes.gatekeeper import GateConfig
from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

_OOS = "2030-01-01"


def _ohlcv(idx):
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": 1.0}, index=idx)


def test_run_foundry_stops_when_pause_file_appears(tmp_path, monkeypatch):
    idx = pd.date_range("2022-01-01", periods=72, freq="1h", tz="UTC")
    panel = pd.DataFrame({"funding_z": np.arange(72.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    # three hypotheses queued; the pause file exists from the start → stop before any
    monkeypatch.setattr(orch, "build_queue", lambda **k: [
        Hypothesis(f"h{i}", "x", SOURCE_ZOO) for i in range(3)])
    calls = {"n": 0}
    def fake_process(*a, **k):
        calls["n"] += 1
        return "rejected"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    pause = tmp_path / "p.pause"
    pause.write_text("stop", encoding="utf-8")

    summary = run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=5, early_stop_after=99),
                          zoo_dir=tmp_path, oos_start=_OOS, ohlcv=_ohlcv(idx),
                          pause_file=str(pause))

    assert summary.get("killswitch_paused") is True
    assert calls["n"] == 0            # stopped before running any hypothesis
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_killswitch.py -q`
Expected: FAIL — `TypeError: run_foundry() got an unexpected keyword argument 'pause_file'`

- [ ] **Step 3: Thread `pause_file` through and check it in the loop**

In `research/hermes/orchestrator.py`, add `pause_file=None` to `run_foundry`'s keyword args and, at the top of the `for hyp in queue:` loop (before `process_hypothesis`), add:

```python
        if pause_file and Path(pause_file).exists():
            log.warning("%s: killswitch pause file present — stopping the sweep", symbol)
            budget_exhausted = False
            summary_paused = True
            break
```

Initialise `summary_paused = False` before the loop, and after building `summary` add:

```python
    if summary_paused:
        summary["killswitch_paused"] = True
```

Add `pause_file=None` to `run_foundry_job`'s signature and pass it through to `run_foundry(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_killswitch.py -q`
Expected: PASS

- [ ] **Step 5: Wire the CLI flag + exit 201**

In `research/hermes/foundry_runner.py`, add near the top:

```python
EXIT_PAUSED = 201
```

Add to the `run` subparser: `r.add_argument("--pause-file", default=None)`. Pass `pause_file=args.pause_file` into the `reconcile_foundry_jobs(...)` call chain (thread it to `run_foundry_job`). After the reconcile, before `return 0`, inspect whether any summary was paused and return 201:

```python
            paused = any(s.get("killswitch_paused") for s in (summaries or []) if isinstance(s, dict))
            ...
            return EXIT_PAUSED if paused else 0
```

(Have `reconcile_foundry_jobs` return the list of summaries if it does not already; thread `pause_file` into its `run_foundry_job` call.)

- [ ] **Step 6: Run the foundry suites**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_killswitch.py research/tests/test_hermes_foundry_runner.py research/tests/test_hermes_orchestrator.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add research/hermes/orchestrator.py research/hermes/foundry_runner.py research/tests/test_hermes_foundry_killswitch.py
git commit -m "feat(hermes): foundry killswitch pause-file between hypotheses + exit 201"
```

---

### Task 2: Bridge soft-failure (no docker → empty overlay, exit 0)

**Files:**
- Modify: `research/hermes/foundry_bridge.py` (`main`)
- Test: `research/tests/test_hermes_foundry_bridge.py` (append)

**Interfaces:**
- Consumes: `write_overlay`, `EXIT_*` (none needed — soft-fail returns 0).
- Produces: `main()` returns 0 and writes an empty overlay when sandbox/docker setup raises `SandboxError`.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_foundry_bridge.py`:

```python
def test_bridge_soft_fails_to_empty_overlay_when_docker_unavailable(tmp_path, monkeypatch):
    # If docker is down, the bridge must NOT fail the discovery_pipeline job —
    # the stages still need to run on library factors. Empty overlay + exit 0.
    from research.hermes import foundry_bridge as fb
    from research.hermes.sandbox import SandboxError

    def boom(*a, **k):
        raise SandboxError("docker daemon unavailable")
    monkeypatch.setattr(fb, "resolve_image_id", boom)

    ov = tmp_path / "ov"
    rc = fb.main(["--symbol", "eth", "--overlay-dir", str(ov),
                  "--image", "talos-sandbox:test",
                  "--manifests-dir", str(tmp_path)])

    assert rc == 0
    assert (ov / "foundry_overlay_eth.parquet").exists()   # empty overlay written
    import pandas as pd
    assert pd.read_parquet(ov / "foundry_overlay_eth.parquet").shape[1] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py::test_bridge_soft_fails_to_empty_overlay_when_docker_unavailable -q`
Expected: FAIL — the `SandboxError` propagates out of `main`.

- [ ] **Step 3: Wrap the sandbox setup in a soft-fail guard**

In `research/hermes/foundry_bridge.py` `main`, wrap the sandbox construction + `build_overlay` so an infra failure writes an empty overlay and returns 0:

```python
    import pandas as pd
    from research.hermes.sandbox import SandboxError
    try:
        sandbox = DockerSandbox(image=resolve_image_id(args.image),
                                timeout_s=args.timeout_s, allow_unpinned=False)
        run_sandbox = make_run_sandbox(sandbox, mdir / "_foundry_scratch")
        df, entries = build_overlay(args.symbol, mdir, panel, run_sandbox, oos_start,
                                    horizon_h=args.horizon_h, cap=args.cap)
    except SandboxError as exc:
        log.warning("bridge: sandbox unavailable (%s); writing empty overlay so the "
                    "pipeline still runs on library factors", exc)
        df, entries = pd.DataFrame(index=panel.index).iloc[:, :0], []
    write_overlay(args.overlay_dir, args.symbol, df, entries)
    print(f"foundry bridge: {len(entries)} candidate(s) -> {args.overlay_dir}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: PASS (all bridge tests)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_bridge.py research/tests/test_hermes_foundry_bridge.py
git commit -m "feat(hermes): bridge soft-fails to an empty overlay when docker is down"
```

---

### Task 3: Bridge sha-keyed recompute cache

**Files:**
- Modify: `research/hermes/foundry_bridge.py` (`recompute_full_span` call site in `build_overlay`)
- Test: `research/tests/test_hermes_foundry_bridge.py` (append)

**Interfaces:**
- Produces: `cached_recompute(code, code_sha, panel, run_sandbox, cache_dir) -> pd.Series` — reuses `<cache_dir>/<code_sha>.parquet` when present, else recomputes and writes it.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_foundry_bridge.py`:

```python
def test_cached_recompute_skips_sandbox_on_sha_hit(tmp_path):
    from research.hermes.foundry_bridge import cached_recompute
    idx = pd.date_range("2024-01-01", periods=20, freq="1h", tz="UTC")
    panel = pd.DataFrame({"close": np.arange(20.0)}, index=idx)
    calls = {"n": 0}
    def run(code, p):
        calls["n"] += 1
        return pd.Series(np.arange(float(len(p))), index=p.index)
    cache = tmp_path / "cache"

    a = cached_recompute("code", "sha_x", panel, run, cache)     # miss → runs
    b = cached_recompute("code", "sha_x", panel, run, cache)     # hit → no run

    assert calls["n"] == 1
    pd.testing.assert_series_equal(a, b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py::test_cached_recompute_skips_sandbox_on_sha_hit -q`
Expected: FAIL — `ImportError: cannot import name 'cached_recompute'`

- [ ] **Step 3: Implement the cache + use it in build_overlay**

Append to `research/hermes/foundry_bridge.py`:

```python
def cached_recompute(code: str, code_sha: str, panel: pd.DataFrame, run_sandbox,
                     cache_dir) -> pd.Series:
    """Reuse a previously recomputed full-span series when the code sha matches,
    so the nightly cadence never re-runs the sandbox for an unchanged factor."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{code_sha}.parquet"
    if cached.exists():
        s = pd.read_parquet(cached).iloc[:, 0]
        return s.reindex(panel.index)
    series = recompute_full_span(code, panel, run_sandbox)
    series.to_frame("v").to_parquet(cached)
    return series
```

In `build_overlay`, replace the `series = recompute_full_span(code, panel, run_sandbox)` line with:

```python
        cache_dir = Path(manifests_dir) / "candidate_features" / "recompute_cache"
        series = cached_recompute(code, meta.get("code_sha256") or fid, panel,
                                  run_sandbox, cache_dir)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_bridge.py research/tests/test_hermes_foundry_bridge.py
git commit -m "feat(hermes): sha-keyed recompute cache so the bridge skips unchanged factors"
```

---

### Task 4: Job kinds `foundry_mine` / `discovery_pipeline` + command-step model + config snapshot

**Files:**
- Modify: `dashboard/server/pipeline_jobs.py`
- Test: `dashboard/server/test_pipeline_jobs_discovery.py`

> pytest scope: `cd dashboard/server && pytest`.

**Interfaces:**
- Produces:
  - `_STEP_COMMAND = {"foundry": ..., "bridge": ...}` marker set; `is_command_step(step_id) -> bool`.
  - `step_command(step_id, symbol, *, overlay_dir, runs_dir, manifests_dir, zoo_dir, image, llm, model, daily_max, pause_file) -> list[str]`.
  - `create_job(kind="foundry_mine"|"discovery_pipeline", symbol=..., config=<dict>)` builds the steps, stamps `job["overlay_dir"]` and `job["config"]` (non-secret).

- [ ] **Step 1: Write the failing test**

Create `dashboard/server/test_pipeline_jobs_discovery.py`:

```python
import pipeline_jobs as pj


def test_foundry_mine_job_has_a_single_foundry_step(tmp_path):
    job = pj.create_job(tmp_path, kind="foundry_mine", symbol="eth",
                        config={"image": "talos:test"})
    assert [s["stage"] for s in job["steps"]] == ["foundry"]
    assert job["config"]["image"] == "talos:test"


def test_discovery_pipeline_job_prepends_bridge_to_the_pipeline(tmp_path):
    job = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    assert job["steps"][0]["stage"] == "bridge"
    assert [s["stage"] for s in job["steps"][1:]] == pj.pipeline_sequence()
    assert job["overlay_dir"].endswith(job["job_id"])


def test_step_command_builds_foundry_and_bridge_argv():
    foundry = pj.step_command("foundry", "eth", overlay_dir="/ov", runs_dir="/rd",
                              manifests_dir="/md", zoo_dir="/zd", image="img",
                              llm="openai", model="gpt-4o-mini", daily_max=6,
                              pause_file="/p.pause")
    assert "foundry_runner" in " ".join(foundry)
    assert "--runs-dir" in foundry and "/rd" in foundry
    assert "--pause-file" in foundry and "/p.pause" in foundry
    bridge = pj.step_command("bridge", "eth", overlay_dir="/ov", runs_dir="/rd",
                             manifests_dir="/md", zoo_dir="/zd", image="img",
                             llm="openai", model="gpt-4o-mini", daily_max=6,
                             pause_file="/p.pause")
    assert "foundry_bridge" in " ".join(bridge)
    assert "--overlay-dir" in bridge and "/ov" in bridge
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && pytest test_pipeline_jobs_discovery.py -q`
Expected: FAIL — `ValueError: invalid kind 'foundry_mine'`

- [ ] **Step 3: Implement kinds, command steps, config snapshot**

In `dashboard/server/pipeline_jobs.py`:

```python
_STEP_COMMAND = {"foundry", "bridge"}


def is_command_step(step_id: str) -> bool:
    return step_id in _STEP_COMMAND


def step_command(step_id, symbol, *, overlay_dir, runs_dir, manifests_dir, zoo_dir,
                 image, llm, model, daily_max, pause_file):
    if step_id == "foundry":
        return ["-m", "research.hermes.foundry_runner", "run",
                "--runs-dir", str(runs_dir), "--manifests-dir", str(manifests_dir),
                "--zoo-dir", str(zoo_dir), "--image", str(image),
                "--llm", str(llm), "--model", str(model),
                "--i-will-spend-real-money", "--daily-max-llm-calls", str(daily_max),
                "--pause-file", str(pause_file)]
    if step_id == "bridge":
        return ["-m", "research.hermes.foundry_bridge", "--symbol", str(symbol),
                "--overlay-dir", str(overlay_dir), "--image", str(image),
                "--manifests-dir", str(manifests_dir)]
    raise KeyError(f"not a command step: {step_id!r}")
```

Extend `create_job` — add `config: Optional[dict] = None` to the signature; add the two kinds before the final `else`:

```python
    elif kind == "foundry_mine":
        stage = None
        steps = [{"stage": "foundry", "status": "pending", "exit_code": None}]
    elif kind == "discovery_pipeline":
        stage = None
        steps = [{"stage": s, "status": "pending", "exit_code": None}
                 for s in ["bridge", *pipeline_sequence()]]
```

In the `job = {...}` dict add:

```python
        "config": dict(config or {}),
        "overlay_dir": str(jobs_dir(repo_root) / _job_id_placeholder),
```

Since `overlay_dir` needs the id, compute `job_id` first: assign `jid = _new_job_id()`, use it for `"job_id": jid` and `"overlay_dir": str(log_path(repo_root, jid).parent)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && pytest test_pipeline_jobs_discovery.py -q`
Expected: PASS

- [ ] **Step 5: Run the dashboard suite**

Run: `cd dashboard/server && pytest -q`
Expected: PASS (existing job tests unaffected — new kinds are additive)

- [ ] **Step 6: Commit**

```bash
git add dashboard/server/pipeline_jobs.py dashboard/server/test_pipeline_jobs_discovery.py
git commit -m "feat(dashboard): foundry_mine/discovery_pipeline job kinds + command-step model"
```

---

### Task 5: Manager runs command steps + `EXIT_PAUSED` + discovery env carry

**Files:**
- Modify: `dashboard/server/pipeline_manager.py` (`execute_job`, `_default_runner`)
- Test: `dashboard/server/test_pipeline_manager_discovery.py`

**Interfaces:**
- Consumes: `is_command_step`, `step_command` (Task 4); `EXIT_PAUSED = 201`.
- Produces: `EXIT_PAUSED = 201` on the manager module; `execute_job` runs command steps, treats 201 as `paused`, and carries a discovery job's `config` env into its stage steps.

- [ ] **Step 1: Write the failing test**

Create `dashboard/server/test_pipeline_manager_discovery.py`:

```python
import pipeline_jobs as pj
import pipeline_manager as pm


def _run_with_fake(tmp_path, job, rc_by_step):
    seen = {}
    def runner(repo_root, step_id, symbol, log_fp, *a, **k):
        seen[step_id] = k.get("config") or (a[-1] if a else None)
        return rc_by_step.get(step_id, 0)
    mgr = pm.Manager(tmp_path, runner=runner)
    mgr.execute_job(job)
    return pj.read_job(tmp_path, job["job_id"]), seen


def test_foundry_step_returning_201_marks_job_paused(tmp_path):
    job = pj.create_job(tmp_path, kind="foundry_mine", symbol="eth", config={})
    done, _ = _run_with_fake(tmp_path, job, {"foundry": pm.EXIT_PAUSED})
    assert done["status"] == "paused"


def test_bridge_failure_does_not_run_stages_but_soft_fail_zero_does(tmp_path):
    # bridge returns 0 (soft-fail path) → stages proceed
    job = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    done, seen = _run_with_fake(tmp_path, job, {})   # all zero
    assert done["status"] == "succeeded"
    assert "0a" in seen                              # stages ran after bridge
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && pytest test_pipeline_manager_discovery.py -q`
Expected: FAIL — `AttributeError: module 'pipeline_manager' has no attribute 'EXIT_PAUSED'`

- [ ] **Step 3: Implement command-step dispatch + 201 + env carry**

In `dashboard/server/pipeline_manager.py` add `EXIT_PAUSED = 201` at module top. In `execute_job`'s step loop, replace the `rc = self._runner(...)` block with a branch:

```python
                    if pj.is_command_step(step["stage"]):
                        rc = self._command_runner(job, step["stage"], fp)
                    else:
                        step_symbol = (job.get("symbol")
                                       if job.get("symbol") and pj.stage_uses_symbol(step["stage"])
                                       else None)
                        rc = self._runner(self.repo_root, step["stage"], step_symbol, fp,
                                          job.get("stress", False), job.get("interval", "1H"),
                                          job.get("kind") == "live_refresh" and step["stage"] == "0a",
                                          config=job.get("config") if job.get("kind") == "discovery_pipeline" else None)
```

After computing `rc`, before the `if rc != 0` failure branch, add the paused case:

```python
                if rc == EXIT_PAUSED:
                    step["exit_code"] = rc; step["status"] = "paused"
                    job["status"] = "paused"; job["exit_code"] = rc
                    job["finished_at"] = pj._now(); pj.write_job(self.repo_root, job)
                    logger.info("job %s paused by killswitch at %s", job["job_id"], step["stage"])
                    return
```

Add `_command_runner` (builds argv via `step_command`, reads the job's non-secret `config`; the LLM key is NOT here — the subprocess reads it from `.env`):

```python
    def _command_runner(self, job, step_id, log_fp) -> int:
        c = job.get("config", {})
        argv = [sys.executable, *pj.step_command(
            step_id, job.get("symbol"), overlay_dir=job["overlay_dir"],
            runs_dir=c.get("runs_dir"), manifests_dir=c.get("manifests_dir"),
            zoo_dir=c.get("zoo_dir"), image=c.get("image"), llm=c.get("llm"),
            model=c.get("model"), daily_max=c.get("daily_max"),
            pause_file=c.get("pause_file"))]
        env = stage_env({"PYTHONPATH": str(self.repo_root) + os.pathsep + os.environ.get("PYTHONPATH", "")})
        proc = subprocess.run(argv, cwd=str(self.repo_root), env=env,
                              stdout=log_fp, stderr=subprocess.STDOUT, text=True)
        return proc.returncode
```

Extend `_default_runner`'s signature with `config: Optional[dict] = None`; when set, merge it into `overrides` plus the overlay flags:

```python
    if config is not None:
        overrides["RESEARCH_INCLUDE_FOUNDRY"] = "1"
        overrides["RESEARCH_FOUNDRY_OVERLAY_DIR"] = config.get("overlay_dir", "")
```

(The manager passes `job["overlay_dir"]` into `config["overlay_dir"]` when calling; simplest is to set `config = {**job.get("config", {}), "overlay_dir": job["overlay_dir"]}` at the call site.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && pytest test_pipeline_manager_discovery.py -q`
Expected: PASS

- [ ] **Step 5: Run the dashboard suite**

Run: `cd dashboard/server && pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard/server/pipeline_manager.py dashboard/server/test_pipeline_manager_discovery.py
git commit -m "feat(dashboard): manager runs command steps, honours EXIT_PAUSED, carries discovery env"
```

---

### Task 6: Manager — foundry_mine low priority + cleanup-on-success + failed-overlay GC

**Files:**
- Modify: `dashboard/server/pipeline_manager.py` (`_oldest_queued`, `execute_job` finally, `reconcile_startup`)
- Test: `dashboard/server/test_pipeline_manager_discovery.py` (append)

**Interfaces:**
- Produces: `_oldest_queued` sorts `foundry_mine` last; `cleanup_overlay` runs only on success; `gc_failed_overlays(repo_root, max_age_days)` deletes stale retained overlays.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/server/test_pipeline_manager_discovery.py`:

```python
def test_foundry_mine_is_selected_after_discovery_pipeline(tmp_path):
    pj.create_job(tmp_path, kind="foundry_mine", symbol="eth", config={})
    dp = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    mgr = pm.Manager(tmp_path)
    assert mgr._oldest_queued()["job_id"] == dp["job_id"]   # pipeline first, mine last


def test_overlay_kept_on_failure_cleaned_on_success(tmp_path):
    job = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    ov = pj.log_path(tmp_path, job["job_id"]).parent
    ov.mkdir(parents=True, exist_ok=True)
    (ov / "foundry_overlay_eth.parquet").write_bytes(b"x")
    def runner(repo_root, step_id, symbol, log_fp, *a, **k):
        return 0 if step_id == "bridge" else 1     # a stage fails
    pm.Manager(tmp_path, runner=runner).execute_job(job)
    assert (ov / "foundry_overlay_eth.parquet").exists()   # retained on failure
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && pytest test_pipeline_manager_discovery.py -q`
Expected: FAIL (mine selected first; overlay deleted by the unconditional finally)

- [ ] **Step 3: Implement priority + conditional cleanup + GC**

`_oldest_queued` sort key — extend the existing tuple so `foundry_mine` sorts last:

```python
        queued.sort(key=lambda j: (
            0 if j.get("kind") == "live_refresh" else
            2 if j.get("kind") == "foundry_mine" else 1,
            j.get("created_at", "")))
```

Replace the `finally: cleanup_overlay(job_dir)` with a success-only call. Move `cleanup_overlay(job_dir)` to run right after `job["status"] = "succeeded"` (before the finally), and delete the `finally` cleanup. Add a GC helper + call it from `reconcile_startup`:

```python
import time

def gc_failed_overlays(repo_root, max_age_days: float = 7.0) -> int:
    root = pj.jobs_dir(repo_root)
    if not root.exists():
        return 0
    removed = 0
    cutoff = time.time() - max_age_days * 86400
    for p in root.glob("*/foundry_overlay_*.parquet"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(); removed += 1
        except OSError:
            continue
    return removed
```

In `reconcile_startup`, after the existing loop, add:
`gc_failed_overlays(self.repo_root, float(os.environ.get("OVERLAY_FAILED_RETENTION_DAYS", "7")))`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && pytest test_pipeline_manager_discovery.py -q`
Expected: PASS

- [ ] **Step 5: Run the dashboard suite**

Run: `cd dashboard/server && pytest -q`
Expected: PASS (the existing `test_pipeline_manager_overlay.py` cleanup test must still pass — success path unchanged)

- [ ] **Step 6: Commit**

```bash
git add dashboard/server/pipeline_manager.py dashboard/server/test_pipeline_manager_discovery.py
git commit -m "feat(dashboard): foundry_mine low priority, cleanup on success only, failed-overlay GC"
```

---

### Task 7: `foundry_miner_scheduler`

**Files:**
- Create: `dashboard/server/foundry_miner_scheduler.py`
- Test: `dashboard/server/test_foundry_miner_scheduler.py`

**Interfaces:**
- Produces: `tick(repo_root, symbols, now, interval_sec) -> list[str]`; `_paused(repo_root) -> bool` (either pause file).

- [ ] **Step 1: Write the failing test**

Create `dashboard/server/test_foundry_miner_scheduler.py`:

```python
from datetime import datetime, timezone
import pipeline_jobs as pj
import foundry_miner_scheduler as fms


def test_tick_enqueues_one_mine_per_symbol(tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDRY_PAUSE_FILE", raising=False)
    monkeypatch.delenv("GLOBAL_PAUSE_FILE", raising=False)
    ids = fms.tick(tmp_path, ["eth", "btc"], datetime.now(timezone.utc), 3600)
    assert len(ids) == 2
    # a second tick dedups against the active jobs
    assert fms.tick(tmp_path, ["eth", "btc"], datetime.now(timezone.utc), 3600) == []


def test_tick_skips_when_pause_file_present(tmp_path, monkeypatch):
    (tmp_path / "p.pause").write_text("x", encoding="utf-8")
    monkeypatch.setenv("FOUNDRY_PAUSE_FILE", str(tmp_path / "p.pause"))
    assert fms.tick(tmp_path, ["eth"], datetime.now(timezone.utc), 3600) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && pytest test_foundry_miner_scheduler.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'foundry_miner_scheduler'`

- [ ] **Step 3: Implement (mirror freshness_scheduler)**

Create `dashboard/server/foundry_miner_scheduler.py`:

```python
"""Foundry miner scheduler: enqueue a foundry_mine job per symbol on a cadence.
Write-only; the serial pipeline_manager executes. Paused by FOUNDRY_PAUSE_FILE
(money) or GLOBAL_PAUSE_FILE (ops)."""
from __future__ import annotations

import logging, os, time
from datetime import datetime, timezone
from pathlib import Path

import pipeline_jobs as pj

logger = logging.getLogger("pipeline.foundry_miner")


def _paused(repo_root) -> bool:
    for env, default in (("FOUNDRY_PAUSE_FILE", "runs/foundry_auto.pause"),
                         ("GLOBAL_PAUSE_FILE", "runs/discovery_global.pause")):
        p = os.environ.get(env) or str(Path(repo_root) / default)
        if Path(p).exists():
            return True
    return False


def _active(repo_root, symbol, now, max_age_sec):
    for j in pj.list_jobs(repo_root, limit=10000):
        if j.get("kind") != "foundry_mine" or j.get("symbol") != symbol:
            continue
        if j.get("status") not in ("queued", "running"):
            continue
        created = j.get("created_at")
        if created and (now - datetime.fromisoformat(created)).total_seconds() > max_age_sec:
            continue
        return j
    return None


def _config(repo_root) -> dict:
    return {
        "image": os.environ.get("DISCOVERY_IMAGE", "talos-sandbox:test"),
        "llm": os.environ.get("DISCOVERY_LLM", "openai"),
        "model": os.environ.get("DISCOVERY_MODEL", "gpt-4o-mini"),
        "daily_max": int(os.environ.get("DISCOVERY_DAILY_MAX_LLM_CALLS", "6")),
        "runs_dir": os.environ.get("FOUNDRY_RUNS_DIR", str(Path(repo_root) / "runs" / "foundry_auto")),
        "zoo_dir": os.environ.get("DISCOVERY_ZOO_DIR", str(Path(repo_root) / "agent" / "src" / "factors" / "zoo")),
        "manifests_dir": str(Path(repo_root) / "research" / "manifests"),
        "pause_file": os.environ.get("FOUNDRY_PAUSE_FILE", str(Path(repo_root) / "runs" / "foundry_auto.pause")),
    }


def tick(repo_root, symbols, now, interval_sec) -> list:
    if _paused(repo_root):
        logger.info("foundry miner paused (pause file present)")
        return []
    out = []
    cfg = _config(repo_root)
    for sym in symbols:
        if _active(repo_root, sym, now, 2 * interval_sec) is None:
            out.append(pj.create_job(repo_root, kind="foundry_mine", symbol=sym, config=cfg)["job_id"])
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    repo_root = Path(os.environ.get("REPO_ROOT", "/repo"))
    symbols = [s.strip() for s in os.environ.get("FOUNDRY_MINE_SYMBOLS", "eth").split(",") if s.strip()]
    interval = float(os.environ.get("FOUNDRY_MINE_INTERVAL_SEC", "86400"))
    while True:
        try:
            ids = tick(repo_root, symbols, datetime.now(timezone.utc), interval)
            if ids:
                logger.info("enqueued foundry_mine: %s", ids)
        except Exception:  # noqa: BLE001
            logger.exception("foundry miner tick failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && pytest test_foundry_miner_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/foundry_miner_scheduler.py dashboard/server/test_foundry_miner_scheduler.py
git commit -m "feat(dashboard): foundry_miner_scheduler enqueues mine jobs on a cadence"
```

---

### Task 8: `discovery_pipeline_scheduler` (dedups against manual pipeline; global pause)

**Files:**
- Create: `dashboard/server/discovery_pipeline_scheduler.py`
- Test: `dashboard/server/test_discovery_pipeline_scheduler.py`

**Interfaces:**
- Produces: `tick(repo_root, symbols, now, interval_sec) -> list[str]` — dedups against active `discovery_pipeline` OR `pipeline` jobs; honours `GLOBAL_PAUSE_FILE` only.

- [ ] **Step 1: Write the failing test**

Create `dashboard/server/test_discovery_pipeline_scheduler.py`:

```python
from datetime import datetime, timezone
import pipeline_jobs as pj
import discovery_pipeline_scheduler as dps


def test_tick_enqueues_and_dedups_against_manual_pipeline(tmp_path, monkeypatch):
    monkeypatch.delenv("GLOBAL_PAUSE_FILE", raising=False)
    ids = dps.tick(tmp_path, ["eth"], datetime.now(timezone.utc), 86400)
    assert len(ids) == 1
    # a manually-queued pipeline for the same symbol blocks the next enqueue
    pj.create_job(tmp_path, kind="pipeline", symbol="btc")
    assert dps.tick(tmp_path, ["btc"], datetime.now(timezone.utc), 86400) == []


def test_tick_skips_on_global_pause(tmp_path, monkeypatch):
    (tmp_path / "g.pause").write_text("x", encoding="utf-8")
    monkeypatch.setenv("GLOBAL_PAUSE_FILE", str(tmp_path / "g.pause"))
    assert dps.tick(tmp_path, ["eth"], datetime.now(timezone.utc), 86400) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && pytest test_discovery_pipeline_scheduler.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery_pipeline_scheduler'`

- [ ] **Step 3: Implement**

Create `dashboard/server/discovery_pipeline_scheduler.py`:

```python
"""Discovery pipeline scheduler: enqueue a discovery_pipeline job per symbol on
its own cadence. Spends nothing, so it ignores FOUNDRY_PAUSE_FILE but honours
GLOBAL_PAUSE_FILE. Dedups against any active pipeline-family job (including a
manual `pipeline`) so the timer never collides with an operator's run."""
from __future__ import annotations

import logging, os, time
from datetime import datetime, timezone
from pathlib import Path

import pipeline_jobs as pj

logger = logging.getLogger("pipeline.discovery")
_PIPELINE_FAMILY = ("discovery_pipeline", "pipeline")


def _global_paused(repo_root) -> bool:
    p = os.environ.get("GLOBAL_PAUSE_FILE") or str(Path(repo_root) / "runs" / "discovery_global.pause")
    return Path(p).exists()


def _active(repo_root, symbol, now, max_age_sec):
    for j in pj.list_jobs(repo_root, limit=10000):
        if j.get("kind") not in _PIPELINE_FAMILY or j.get("symbol") != symbol:
            continue
        if j.get("status") not in ("queued", "running"):
            continue
        created = j.get("created_at")
        if created and (now - datetime.fromisoformat(created)).total_seconds() > max_age_sec:
            continue
        return j
    return None


def tick(repo_root, symbols, now, interval_sec) -> list:
    if _global_paused(repo_root):
        logger.info("discovery pipeline paused (global pause file present)")
        return []
    out = []
    for sym in symbols:
        if _active(repo_root, sym, now, 2 * interval_sec) is None:
            out.append(pj.create_job(repo_root, kind="discovery_pipeline", symbol=sym, config={
                "image": os.environ.get("DISCOVERY_IMAGE", "talos-sandbox:test"),
                "manifests_dir": str(Path(repo_root) / "research" / "manifests"),
            })["job_id"])
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    repo_root = Path(os.environ.get("REPO_ROOT", "/repo"))
    symbols = [s.strip() for s in os.environ.get("DISCOVERY_PIPELINE_SYMBOLS", "eth").split(",") if s.strip()]
    interval = float(os.environ.get("DISCOVERY_PIPELINE_INTERVAL_SEC", "86400"))
    while True:
        try:
            ids = tick(repo_root, symbols, datetime.now(timezone.utc), interval)
            if ids:
                logger.info("enqueued discovery_pipeline: %s", ids)
        except Exception:  # noqa: BLE001
            logger.exception("discovery pipeline tick failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && pytest test_discovery_pipeline_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Run the full dashboard suite**

Run: `cd dashboard/server && pytest -q`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

```bash
git add dashboard/server/discovery_pipeline_scheduler.py dashboard/server/test_discovery_pipeline_scheduler.py
git commit -m "feat(dashboard): discovery_pipeline_scheduler with manual-pipeline dedup + global pause"
```

---

## Final verification

- [ ] **Both scopes green**:

```bash
.venv/Scripts/python.exe -m pytest research/tests/ -q
cd dashboard/server && pytest -q && cd ../..
```

- [ ] **Dry-run the two ticks** (no docker, no spend — just enqueue then inspect):

```bash
cd /c/Users/cool6/Vibe-Trading/dashboard/server
/c/Users/cool6/Vibe-Trading/.venv/Scripts/python.exe -c "import pipeline_jobs as pj, foundry_miner_scheduler as f, discovery_pipeline_scheduler as d; from datetime import datetime,timezone; r='C:/Users/cool6/Vibe-Trading'; print('mine', f.tick(r,['eth'],datetime.now(timezone.utc),86400)); print('pipe', d.tick(r,['eth'],datetime.now(timezone.utc),86400))"
```

Expected: prints one `foundry_mine` id and one `discovery_pipeline` id; the jobs appear in `runs/pipeline_jobs/`.

- [ ] **Confirm no secret in a job payload**:

```bash
grep -riE "sk-|api_key|secret" runs/pipeline_jobs/*/job.json
```

Expected: no matches — the config snapshot holds only image/model/dirs.

- [ ] **Confirm production untouched**:

```bash
git status --short research/manifests/
```

Expected: no `features_*.parquet` / `factor_values_*.parquet` changes.
