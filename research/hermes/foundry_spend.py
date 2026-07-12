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
