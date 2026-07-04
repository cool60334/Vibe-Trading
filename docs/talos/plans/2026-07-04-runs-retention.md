# runs/ 與 pipeline_jobs/ 保留政策工具（Part A B3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一個**預設 dry-run** 的清理工具：列出（並在 `--apply` 時刪除）不再被引用且過期的 `runs/<run>/` 目錄與已終結且過期的 `runs/pipeline_jobs/<job>/` 目錄。伺服器實測 517 個 job 目錄、sweep/CPCV 一輪可寫數 GB，只增不減。

**Architecture:** 純函式規劃（`plan_retention` 回傳待刪清單）與執行（`apply_retention`）分離；CLI 預設只印不刪。安全鐵律內建：`runs/testnet/`（實盤狀態）與 `runs/pipeline_jobs/` 裡非終結態的 job **永不**入清單；被 `strategy_runs.json` 任何欄位引用的 run 永久保留。

**Tech Stack:** Python 標準庫、pytest。

**背景（給零 context 的執行者）：**
- `research/strategy_runs.json` 是策略→run 目錄的 registry。引用欄位：`base_run`（str）、`sweep_run`（str|null）、`regime_runs`/`stress_runs`/`lag_stress_runs`/`intrabar_audit_runs`（dict 值）、`walk_forward_runs`/`oos_runs`（list）。這些名字對應 `runs/<名字>/` 目錄。
- `runs/pipeline_jobs/<job_id>/job.json` 欄位：`status`（queued/running/succeeded/failed/canceled）、`finished_at`（UTC ISO 或 null）。
- 測試 scope：**`cd dashboard/server && python -m pytest -q`**。
- 部署後由使用者手動跑或掛 cron：`python dashboard/server/retention.py --repo-root /repo`（dry-run）→ 檢視 → 加 `--apply`。本計畫**不**掛任何排程。
- **檔案所有權**：`dashboard/server/retention.py`（新）、`dashboard/server/test_retention.py`（新）。不准動其他檔。

---

### Task 1: `referenced_runs()` + `plan_retention()`

**Files:**
- Create: `dashboard/server/retention.py`
- Create: `dashboard/server/test_retention.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""retention — dry-run-first cleanup of runs/ and runs/pipeline_jobs/."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from retention import apply_retention, plan_retention, referenced_runs

_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


def test_referenced_runs_collects_every_field_shape():
    runs = {
        "s1": {
            "base_run": "s1_base",
            "sweep_run": "s1_sweep_007",
            "regime_runs": {"bull": "s1_bull", "bear": None},
            "stress_runs": {"2x": "s1_stress_2x"},
            "intrabar_audit_runs": {"train": "s1_base"},
            "walk_forward_runs": ["s1_oos"],
        },
        "s2": {"base_run": None, "sweep_run": None},
    }
    assert referenced_runs(runs) == {
        "s1_base", "s1_sweep_007", "s1_bull", "s1_stress_2x", "s1_oos",
    }


def _make_repo(tmp_path, *, old_days=60):
    old = (_NOW - timedelta(days=old_days)).timestamp()
    runs = tmp_path / "runs"
    for name in ("kept_referenced", "old_orphan", "fresh_orphan", "testnet", "pipeline_jobs"):
        (runs / name).mkdir(parents=True)
    os.utime(runs / "kept_referenced", (old, old))
    os.utime(runs / "old_orphan", (old, old))
    os.utime(runs / "testnet", (old, old))

    research = tmp_path / "research"
    research.mkdir()
    (research / "strategy_runs.json").write_text(json.dumps(
        {"s1": {"base_run": "kept_referenced"}}), encoding="utf-8")

    def job(job_id, status, finished_at):
        d = runs / "pipeline_jobs" / job_id
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps(
            {"status": status, "finished_at": finished_at}), encoding="utf-8")
        return d

    job("old_done", "succeeded", (_NOW - timedelta(days=120)).isoformat())
    job("fresh_done", "succeeded", (_NOW - timedelta(days=5)).isoformat())
    job("old_running", "running", None)
    return tmp_path


def test_plan_keeps_referenced_and_fresh_and_protected(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    run_names = {p.name for p in plan.run_dirs}
    assert run_names == {"old_orphan"}          # referenced/fresh/protected all kept
    job_names = {p.name for p in plan.job_dirs}
    assert job_names == {"old_done"}            # fresh + non-terminal kept


def test_plan_never_touches_testnet_or_pipeline_jobs_as_runs(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=0, keep_job_days=0)
    names = {p.name for p in plan.run_dirs}
    assert "testnet" not in names and "pipeline_jobs" not in names


def test_apply_deletes_only_planned_paths(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    apply_retention(repo, plan)
    assert not (repo / "runs" / "old_orphan").exists()
    assert not (repo / "runs" / "pipeline_jobs" / "old_done").exists()
    assert (repo / "runs" / "kept_referenced").exists()
    assert (repo / "runs" / "testnet").exists()
    assert (repo / "runs" / "pipeline_jobs" / "old_running").exists()


def test_apply_refuses_paths_outside_runs(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    plan.run_dirs.append(tmp_path / "research")  # malicious/buggy entry
    import pytest
    with pytest.raises(ValueError):
        apply_retention(repo, plan)
    assert (tmp_path / "research").exists()
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `cd dashboard/server && python -m pytest test_retention.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'retention'`。

- [ ] **Step 3: 實作 `dashboard/server/retention.py`**

```python
"""Dry-run-first retention for runs/ and runs/pipeline_jobs/.

Sweep/CPCV runs accumulate GBs and job dirs grow forever (517 observed on
the server). This tool PLANS deletions as data, prints them by default and
only deletes under --apply. Hard safety rails:

  - runs/testnet/ (live trader state) and runs/pipeline_jobs/ are never
    treated as run dirs;
  - any run referenced anywhere in research/strategy_runs.json is kept
    forever;
  - only terminal jobs (succeeded/failed/canceled) older than the cutoff
    are deletable; queued/running never are;
  - apply refuses any path that is not strictly inside <repo>/runs/.
"""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROTECTED_TOPLEVEL = {"testnet", "pipeline_jobs"}
_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "canceled"}


@dataclass
class RetentionPlan:
    run_dirs: list = field(default_factory=list)
    job_dirs: list = field(default_factory=list)


def referenced_runs(strategy_runs: dict) -> set:
    out: set = set()
    for entry in (strategy_runs or {}).values():
        if not isinstance(entry, dict):
            continue
        for key in ("base_run", "sweep_run"):
            v = entry.get(key)
            if v:
                out.add(v)
        for key in ("regime_runs", "stress_runs", "lag_stress_runs",
                    "intrabar_audit_runs"):
            out.update(v for v in (entry.get(key) or {}).values() if v)
        for key in ("walk_forward_runs", "oos_runs"):
            out.update(v for v in (entry.get(key) or []) if v)
    return out


def _load_strategy_runs(repo_root: Path) -> dict:
    path = repo_root / "research" / "strategy_runs.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Unreadable registry → we cannot know what is referenced → keep all.
        return None  # type: ignore[return-value]


def plan_retention(repo_root: "str | Path", *, now: datetime,
                   keep_run_days: int = 30, keep_job_days: int = 90) -> RetentionPlan:
    repo_root = Path(repo_root)
    plan = RetentionPlan()
    runs_root = repo_root / "runs"
    if not runs_root.is_dir():
        return plan

    registry = _load_strategy_runs(repo_root)
    keep = referenced_runs(registry) if registry is not None else None
    run_cutoff = (now - timedelta(days=keep_run_days)).timestamp()
    if keep is not None:  # registry readable → orphan runs are fair game
        for d in sorted(p for p in runs_root.iterdir() if p.is_dir()):
            if d.name in _PROTECTED_TOPLEVEL or d.name in keep:
                continue
            if d.stat().st_mtime < run_cutoff:
                plan.run_dirs.append(d)

    job_cutoff = now - timedelta(days=keep_job_days)
    jobs_root = runs_root / "pipeline_jobs"
    if jobs_root.is_dir():
        for d in sorted(p for p in jobs_root.iterdir() if p.is_dir()):
            try:
                job = json.loads((d / "job.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # unreadable job → keep (safe)
            if job.get("status") not in _TERMINAL_JOB_STATUSES:
                continue
            finished = job.get("finished_at")
            try:
                finished_dt = datetime.fromisoformat(finished)
            except (TypeError, ValueError):
                continue
            if finished_dt.tzinfo is None:
                finished_dt = finished_dt.replace(tzinfo=timezone.utc)
            if finished_dt < job_cutoff:
                plan.job_dirs.append(d)
    return plan


def apply_retention(repo_root: "str | Path", plan: RetentionPlan) -> None:
    runs_root = Path(repo_root).resolve() / "runs"
    for d in list(plan.run_dirs) + list(plan.job_dirs):
        resolved = Path(d).resolve()
        if runs_root not in resolved.parents:
            raise ValueError(f"refusing to delete outside runs/: {resolved}")
        if resolved.name in _PROTECTED_TOPLEVEL and resolved.parent == runs_root:
            raise ValueError(f"refusing to delete protected dir: {resolved}")
        shutil.rmtree(resolved, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="runs/ retention (dry-run by default)")
    ap.add_argument("--repo-root", default="/repo")
    ap.add_argument("--keep-run-days", type=int, default=30)
    ap.add_argument("--keep-job-days", type=int, default=90)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; default only prints the plan")
    args = ap.parse_args()

    plan = plan_retention(args.repo_root, now=datetime.now(tz=timezone.utc),
                          keep_run_days=args.keep_run_days,
                          keep_job_days=args.keep_job_days)
    print(f"run dirs to delete: {len(plan.run_dirs)}")
    for d in plan.run_dirs:
        print(f"  {d}")
    print(f"job dirs to delete: {len(plan.job_dirs)}")
    for d in plan.job_dirs:
        print(f"  {d}")
    if args.apply:
        apply_retention(args.repo_root, plan)
        print("applied.")
    else:
        print("dry-run only — re-run with --apply to delete.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑測試確認 GREEN + 全套 server scope**

Run: `cd dashboard/server && python -m pytest -q`
Expected: 全 passed。

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/retention.py dashboard/server/test_retention.py
git commit -m "feat(server): dry-run-first retention tool for runs/ and pipeline_jobs/"
```

---

## Self-check

- [ ] 預設 dry-run；`--apply` 才刪。
- [ ] `runs/testnet/`、非終結 job、被 registry 引用的 run 在任何參數下都不會入清單（有測試釘住）。
- [ ] registry 讀不到 → 一個 run 都不刪（fail-safe）。
- [ ] `apply_retention` 拒絕 runs/ 以外路徑（有測試釘住）。
- [ ] **沒有**掛排程 —— 上線排程是使用者的部署決定。
