# Talos Foundry Cross-Run Daily Cost Ceiling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A durable UTC-calendar-day LLM-call budget across `foundry run` invocations, so a nightly cron cannot spend without bound.

**Architecture:** A new `foundry_spend.py` holds an append-only spend ledger (`todays_spend`/`record_spend`) and an `os.mkdir` single-instance lock. `reconcile` gains an optional pre-built `shared_budget` so `main` keeps the reference. `main`'s `run` guard pins the UTC date, shrinks the batch to the day's remaining allowance, records spend in a mask-safe `finally`, and returns non-zero on refuse/record-failure.

**Tech Stack:** Python 3.11 stdlib (`os`, `json`, `shutil`, `contextlib`), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-12-foundry-daily-cost-ceiling-design.md`

## Global Constraints

- **No test spends money.** Tests monkeypatch `reconcile_foundry_jobs`/`build_llm` and touch only files.
- **Units are LLM call counts** (reuse `ForgeBudget`); calls is a **floor** (a transport retry can issue >1 real request), so document head-room. No USD.
- **The daily guard is opt-in:** `--daily-max-llm-calls` defaults to `None` → no cross-run guard (per-reconcile budget only, back-compat).
- **`record_spend` raises on write failure** (unlike `research_ledger.append_event`'s fail-soft) — the guard must detect a failed record and return non-zero.
- **`finally` must never mask reconcile's exception:** recording is wrapped in its own `try/except` that only logs.
- **Pin the UTC date once** at run start; `record_spend` bills that exact day (midnight-crossing correctness).
- **Arg validation via `type=positive_int`** (print+SystemExit-2), never `assert`.
- **Three pytest scopes never mix.** Run research tests from repo root: `python -m pytest research/tests/`.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Never push without being asked.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `research/hermes/foundry_spend.py` (create) | `todays_spend`, `record_spend`, `single_instance_lock`, `AlreadyRunning`, `default_ledger_path` | 1 |
| `research/tests/test_hermes_foundry_spend.py` (create) | ledger + lock unit tests | 1 |
| `research/hermes/foundry_runner.py` (modify) | `reconcile` gains `shared_budget=None`; `main` run gains the daily guard | 2, 3 |
| `research/tests/test_hermes_foundry_runner.py` (modify) | reconcile-injected-budget + daily-guard integration tests | 2, 3 |

---

## Task 1: `foundry_spend.py` — ledger + single-instance lock

**Files:**
- Create: `research/hermes/foundry_spend.py`
- Test: `research/tests/test_hermes_foundry_spend.py`

**Interfaces:**
- Produces:
  - `default_ledger_path(runs_dir) -> Path` → `<runs-dir>/foundry_spend.jsonl`.
  - `todays_spend(ledger_path, day: date) -> int`.
  - `record_spend(ledger_path, day: date, calls: int, tokens: int) -> None` (raises `OSError` on write failure).
  - `single_instance_lock(runs_dir, stale_hours: float = 6)` context manager; raises `AlreadyRunning` if a fresh lock is held.
  - `class AlreadyRunning(RuntimeError)`.

**Why:** These are the durable-spend primitives, pure file/OS operations, fully testable with tmp files and no network/LLM.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_spend.py (create)
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from research.hermes.foundry_spend import (
    AlreadyRunning, default_ledger_path, record_spend, single_instance_lock, todays_spend,
)


def test_todays_spend_sums_only_today_utc(tmp_path):
    led = tmp_path / "spend.jsonl"
    led.write_text(
        json.dumps({"date": "2026-07-12", "calls": 4}) + "\n" +
        json.dumps({"date": "2026-07-12", "calls": 2}) + "\n" +
        json.dumps({"date": "2026-07-11", "calls": 9}) + "\n", encoding="utf-8")
    assert todays_spend(led, date(2026, 7, 12)) == 6      # yesterday's 9 excluded
    assert todays_spend(led, date(2026, 7, 13)) == 0      # nothing today


def test_todays_spend_skips_malformed_line(tmp_path):
    led = tmp_path / "spend.jsonl"
    led.write_text('{"date": "2026-07-12", "calls": 3}\nNOT JSON\n', encoding="utf-8")
    assert todays_spend(led, date(2026, 7, 12)) == 3      # corrupt line skipped, no crash


def test_todays_spend_missing_file_is_zero(tmp_path):
    assert todays_spend(tmp_path / "nope.jsonl", date(2026, 7, 12)) == 0


def test_record_spend_appends_with_pinned_day(tmp_path):
    led = tmp_path / "spend.jsonl"
    record_spend(led, day=date(2026, 7, 12), calls=6, tokens=2562)
    rec = json.loads(led.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["date"] == "2026-07-12"                    # the PASSED day, not now
    assert rec["calls"] == 6 and rec["tokens"] == 2562


def test_record_spend_raises_on_write_failure(tmp_path, monkeypatch):
    # a directory where the ledger path is unwritable -> OSError propagates
    led = tmp_path / "spend.jsonl"
    def boom(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(Path, "open", boom)
    with pytest.raises(OSError):
        record_spend(led, day=date(2026, 7, 12), calls=1, tokens=1)


def test_lock_blocks_a_second_instance(tmp_path):
    with single_instance_lock(tmp_path):
        with pytest.raises(AlreadyRunning):
            with single_instance_lock(tmp_path):
                pass


def test_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "foundry.lock"
    lock.mkdir()
    old = time.time() - 7 * 3600            # 7h old, past the 6h default
    os.utime(lock, (old, old))
    with single_instance_lock(tmp_path, stale_hours=6):   # takes over, no raise
        assert lock.exists()


def test_lock_released_on_exit(tmp_path):
    with single_instance_lock(tmp_path):
        pass
    assert not (tmp_path / "foundry.lock").exists()


def test_default_ledger_path(tmp_path):
    assert default_ledger_path(tmp_path) == tmp_path / "foundry_spend.jsonl"
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_spend.py -v`
Expected: FAIL (`research.hermes.foundry_spend` does not exist).

- [ ] **Step 3: Implement**

```python
# research/hermes/foundry_spend.py (create)
"""Durable, cross-run spend accounting + a single-instance lock for the Foundry.

The per-reconcile ForgeBudget caps one `foundry run`; this module adds the
cross-invocation daily ceiling (a nightly cron gets a fresh ForgeBudget each pass
otherwise). Call counts only — a call's dollar cost varies, and a transport retry
can issue more than one real request, so the ledger's `calls` is a floor."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    """Another foundry run holds the single-instance lock for this runs-dir."""


def default_ledger_path(runs_dir) -> Path:
    return Path(runs_dir) / "foundry_spend.jsonl"


def todays_spend(ledger_path, day: date) -> int:
    """Sum `calls` over ledger lines whose `date` == day.isoformat(). A malformed
    line is skipped (best-effort), never crashes the guard; a missing file is 0."""
    path = Path(ledger_path)
    if not path.exists():
        return 0
    key = day.isoformat()
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("date") == key:
            total += int(rec.get("calls", 0) or 0)
    return total


def record_spend(ledger_path, day: date, calls: int, tokens: int) -> None:
    """Append one spend line billed to the PASSED `day` (not now, so a run that
    crosses UTC midnight bills the day it budgeted against). Raises OSError on
    write failure — unlike research_ledger's fail-soft append, the daily guard
    must know when recording failed so it can signal non-zero."""
    rec = {"date": day.isoformat(), "ts": datetime.now(timezone.utc).isoformat(),
           "calls": int(calls), "tokens": int(tokens)}
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


@contextmanager
def single_instance_lock(runs_dir, stale_hours: float = 6):
    """Prevent a cron misfire from double-running (doubled spend + sandbox load).

    `os.mkdir` is the atomic acquire. Staleness is read from the lock DIRECTORY's
    own st_mtime, never from a meta.json inside it: mkdir stamps the dir
    atomically, whereas writing meta.json afterward is a second, non-atomic step a
    peer could read half-written. meta.json is written for human diagnostics only;
    a read error there never grounds a takeover."""
    lock = Path(runs_dir) / "foundry.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    def _acquire():
        os.mkdir(lock)                       # atomic; FileExistsError if held
        try:
            (lock / "meta.json").write_text(json.dumps(
                {"pid": os.getpid(), "ts": datetime.now(timezone.utc).isoformat()}))
        except OSError:
            pass                             # diagnostic only

    try:
        _acquire()
    except FileExistsError:
        age_h = (time.time() - lock.stat().st_mtime) / 3600.0
        if age_h > stale_hours:
            try:
                shutil.rmtree(lock)
            except OSError as exc:
                log.warning("stale foundry.lock could not be removed (%s); future "
                            "runs stay blocked until it clears", exc)
                raise AlreadyRunning(f"stale lock {lock} could not be removed") from exc
            _acquire()                       # retry once
        else:
            raise AlreadyRunning(f"another foundry run holds {lock} (age {age_h:.1f}h)")
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest research/tests/test_hermes_foundry_spend.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_spend.py research/tests/test_hermes_foundry_spend.py
git commit -m "feat(hermes): foundry spend ledger + single-instance lock

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `reconcile` accepts a pre-built `shared_budget`

**Files:**
- Modify: `research/hermes/foundry_runner.py` (`reconcile_foundry_jobs` signature ~line 71-72; shared construction ~line 104)
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `ForgeBudget` (already imported in `foundry_runner.py`).
- Produces: `reconcile_foundry_jobs(..., batch_max_llm_calls=None, shared_budget=None)` — if `shared_budget` is given, use it (so `main` keeps the reference for `finally` recording); else build from `batch_max_llm_calls` as today.

**Why:** `main` must hold the shared budget object to read `.used` after reconcile (even on abort). Letting `main` build it and pass it in is the cleanest seam; the projected-cost pre-check reads `shared.used` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_foundry_runner.py (append; _queue_job helper already exists)
def test_reconcile_uses_a_passed_shared_budget(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.forge import ForgeBudget
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    def fake_run_job(job_path, *, forge_budget=None, **k):
        forge_budget.charge_call(); forge_budget.charge_call()   # spend 2 on the shared obj
        return {"candidate": 0, "llm_calls_used": forge_budget.used}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    shared = ForgeBudget(max_llm_calls=5)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                              zoo_dir=tmp_path, shared_budget=shared)
    assert shared.used == 2          # main can read the same object afterwards
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py::test_reconcile_uses_a_passed_shared_budget -v`
Expected: FAIL (`reconcile_foundry_jobs` has no `shared_budget` parameter).

- [ ] **Step 3: Implement**

In `reconcile_foundry_jobs`'s signature (foundry_runner.py:71-72), add `shared_budget=None`:

```python
def reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir,
                           budget=None, batch_max_llm_calls=None, shared_budget=None) -> list:
```

Replace the shared construction (foundry_runner.py:104):

```python
    # was: shared = ForgeBudget(max_llm_calls=batch_max_llm_calls) if batch_max_llm_calls is not None else None
    if shared_budget is not None:
        shared = shared_budget                       # caller owns it (reads .used after)
    elif batch_max_llm_calls is not None:
        shared = ForgeBudget(max_llm_calls=batch_max_llm_calls)
    else:
        shared = None
```

- [ ] **Step 4: Run — verify pass, no regression**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -q`
Expected: PASS (the new test + all existing reconcile tests; `shared_budget=None` preserves current behaviour).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): reconcile accepts a caller-owned shared ForgeBudget

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `main` run daily guard

**Files:**
- Modify: `research/hermes/foundry_runner.py` (imports ~line 17; `run` subparser ~line 167-178; run branch ~line 187-198)
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `single_instance_lock`, `AlreadyRunning`, `todays_spend`, `record_spend`, `default_ledger_path` (Task 1); `reconcile(..., shared_budget=)` (Task 2); `ForgeBudget`.
- Produces: `main`'s `run` gains `--daily-max-llm-calls` (type=`_positive_int`, default None), `--spend-ledger` (default None → `<runs-dir>/foundry_spend.jsonl`), `--batch-max-llm-calls` becomes `type=_positive_int`; the guard shrinks/refuses, records in a mask-safe `finally`, returns 2 on refuse/record-failure/`AlreadyRunning`.

**Why:** This is the durable ceiling itself — the seam that makes a nightly cron safe.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_runner.py (append)
def _run_args(tmp_path, **extra):
    a = ["run", "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
         "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
         "--model", "gpt-4o-mini", "--i-will-spend-real-money"]
    for k, v in extra.items():
        a += [f"--{k}", str(v)]
    return a

def _stub_run_deps(monkeypatch, fr, reconcile):
    monkeypatch.setattr(fr, "resolve_image_id", lambda tag: "sha256:" + "e" * 64)
    monkeypatch.setattr(fr, "DockerSandbox", lambda **k: object())
    monkeypatch.setattr(fr, "build_llm", lambda *a, **k: type("C", (), {"total_tokens": 0})())
    monkeypatch.setattr(fr, "reconcile_foundry_jobs", reconcile)


def test_remaining_zero_refuses_and_skips_reconcile(tmp_path, monkeypatch):
    import json
    from research.hermes import foundry_runner as fr
    (tmp_path / "foundry_spend.jsonl").write_text(
        json.dumps({"date": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat(), "calls": 40}) + "\n")
    called = {"reconcile": False}
    _stub_run_deps(monkeypatch, fr, lambda *a, **k: called.__setitem__("reconcile", True))
    rc = fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert rc == 2 and called["reconcile"] is False


def test_effective_shrinks_to_remaining(tmp_path, monkeypatch):
    import json
    from research.hermes import foundry_runner as fr
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    (tmp_path / "foundry_spend.jsonl").write_text(json.dumps({"date": today, "calls": 37}) + "\n")
    seen = {}
    def reconcile(*a, shared_budget=None, **k): seen["cap"] = shared_budget.max_llm_calls
    _stub_run_deps(monkeypatch, fr, reconcile)
    fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert seen["cap"] == 3          # min(6, 40-37)


def test_spend_recorded_even_when_reconcile_aborts(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    from research.hermes.llm_client import LLMUnavailable
    from research.hermes.foundry_spend import todays_spend
    from datetime import datetime, timezone
    def reconcile(*a, shared_budget=None, **k):
        shared_budget.charge_call(); shared_budget.charge_call()
        raise LLMUnavailable("bad key")
    _stub_run_deps(monkeypatch, fr, reconcile)
    with pytest.raises(LLMUnavailable):           # reconcile's exception is NOT masked
        fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert todays_spend(tmp_path / "foundry_spend.jsonl",
                        datetime.now(timezone.utc).date()) == 2   # spend still recorded


def test_argparse_rejects_nonpositive_budgets(tmp_path):
    from research.hermes import foundry_runner as fr
    with pytest.raises(SystemExit):
        fr.main(_run_args(tmp_path, **{"batch-max-llm-calls": 0}))
    with pytest.raises(SystemExit):
        fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 0}))


def test_daily_omitted_skips_the_guard(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    seen = {"reconciled": False}
    _stub_run_deps(monkeypatch, fr,
                   lambda *a, **k: seen.__setitem__("reconciled", True))
    rc = fr.main(_run_args(tmp_path, **{"batch-max-llm-calls": 6}))   # no --daily
    assert rc == 0 and seen["reconciled"] is True
    assert not (tmp_path / "foundry_spend.jsonl").exists()   # no ledger touched


def test_record_failure_returns_nonzero_when_reconcile_succeeded(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    _stub_run_deps(monkeypatch, fr, lambda *a, **k: None)     # reconcile succeeds
    monkeypatch.setattr(fr, "record_spend",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rc = fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert rc == 2
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "remaining_zero or shrinks or aborts or nonpositive or daily_omitted or record_failure" -v`
Expected: FAIL (`--daily-max-llm-calls` unknown; no guard).

- [ ] **Step 3: Implement**

Add imports at the top of `foundry_runner.py` (near line 17, alongside the existing `from research.hermes.forge import ForgeBudget` and the llm_client import):

```python
from datetime import datetime, timezone

from research.hermes.foundry_spend import (
    AlreadyRunning, default_ledger_path, record_spend, single_instance_lock, todays_spend,
)
```

Add a positive-int arg type near the top of the module (after imports):

```python
def _positive_int(s: str) -> int:
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {v}")
    return v
```

Change the `run` subparser args (foundry_runner.py:177) and add two (after line 178):

```python
    r.add_argument("--batch-max-llm-calls", type=_positive_int, default=6)
    r.add_argument("--max-tokens", type=int, default=2048)
    r.add_argument("--daily-max-llm-calls", type=_positive_int, default=None,
                   help="cross-run UTC-day call ceiling; omit for no daily guard")
    r.add_argument("--spend-ledger", default=None,
                   help="spend ledger path (default <runs-dir>/foundry_spend.jsonl)")
```

Replace the run branch (foundry_runner.py:187-198):

```python
    if not args.i_will_spend_real_money:
        print("refusing: a real run spends money. Re-run with "
              "--i-will-spend-real-money once your provider key is set.")
        return 2

    ledger = args.spend_ledger or str(default_ledger_path(args.runs_dir))
    daily = args.daily_max_llm_calls
    try:
        with single_instance_lock(args.runs_dir):
            today = None
            effective = args.batch_max_llm_calls
            if daily is not None:
                today = datetime.now(timezone.utc).date()          # pinned once
                remaining = daily - todays_spend(ledger, today)
                if remaining <= 0:
                    print(f"refusing: daily LLM-call budget {daily} already spent "
                          f"today ({today.isoformat()}).")
                    return 2
                effective = min(args.batch_max_llm_calls, remaining)

            image_id = resolve_image_id(args.image)                # infra pre-check; raises to abort
            sandbox = DockerSandbox(image=image_id, timeout_s=args.timeout_s, allow_unpinned=False)
            llm = build_llm(args.llm, model=args.model, max_tokens=args.max_tokens)
            shared = ForgeBudget(max_llm_calls=effective)
            record_error = None
            try:
                reconcile_foundry_jobs(args.runs_dir, args.manifests_dir, llm, sandbox,
                                       args.zoo_dir, shared_budget=shared)
            finally:
                if daily is not None:
                    try:
                        record_spend(ledger, day=today, calls=shared.used,
                                     tokens=getattr(llm, "total_tokens", 0))
                    except Exception as e:      # noqa: BLE001 - MUST NOT return/raise here:
                        log.error("daily guard compromised: spend not recorded: %s", e)
                        record_error = e         # that would mask reconcile's own traceback
            if record_error is not None:
                return 2
            print(f"foundry run complete. tokens used (reported): {getattr(llm, 'total_tokens', 0)}")
            return 0
    except AlreadyRunning as exc:
        print(f"refusing: {exc}")
        return 2
```

- [ ] **Step 4: Run — verify pass, full runner + spend suites**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py research/tests/test_hermes_foundry_spend.py -v`
Expected: PASS.

- [ ] **Step 5: Full research suite + commit**

Run: `python -m pytest research/tests/ -q`
Expected: all pass/skip.

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): cross-run daily LLM-call ceiling in the foundry run guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §N1 ledger (`todays_spend`/`record_spend`, pinned day, raises on write failure) → Task 1. ✅
- §N2 lock (`os.mkdir`, dir-`st_mtime` staleness, rmtree-fail logs, meta diagnostic-only) → Task 1. ✅
- §N3 reconcile `shared_budget` → Task 2. ✅
- §1 main guard (opt-in `--daily`, pinned date, shrink/refuse, mask-safe `finally`, return 2) → Task 3. ✅
- §2 `--spend-ledger` override + `type=positive_int` → Task 3. ✅
- §3 error table: refuse (Task 3 `remaining_zero`), abort-not-masked (Task 3 `aborts`), record-failure (Task 3 `record_failure`), lock held/stale (Task 1 `lock_blocks`/`stale_lock`), non-positive args (Task 3 `nonpositive`), daily omitted (Task 3 `daily_omitted`). ✅
- Non-goals respected: no USD, no rotation, single-host. ✅

**Placeholder scan:** none. `stale_hours=6` and default ledger name are concrete. The `_positive_int` / `default_ledger_path` helpers are fully specified.

**Type consistency:** `single_instance_lock(runs_dir, stale_hours=6)` / `AlreadyRunning` / `record_spend(ledger, day=, calls=, tokens=)` / `todays_spend(path, day)` / `default_ledger_path(runs_dir)` identical across Task 1 defs and Task 3 calls. `reconcile_foundry_jobs(..., shared_budget=)` matches Task 2 def and Task 3 call. `_positive_int` used in Task 3 argparse. `ForgeBudget(max_llm_calls=)` consistent with existing forge.py.

**Known deferred (documented in spec):** the real endpoint is exercised only by the manual paid run; concurrency beyond the `os.mkdir` window and USD accounting are out of scope.
