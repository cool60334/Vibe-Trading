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
    run_dirs: list[Path] = field(default_factory=list)
    job_dirs: list[Path] = field(default_factory=list)


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


def _refuses_as_run_dir(resolved: Path, runs_root: Path) -> bool:
    """True if resolved is (or is nested inside) a protected top-level dir.

    Job dirs legitimately live *inside* runs/pipeline_jobs/<job_id>/, so this
    guard is only meant for entries in plan.run_dirs — pipeline_jobs itself
    (and testnet/ entirely) must never be treated as a deletable run.
    """
    rel_parts = resolved.relative_to(runs_root).parts
    return bool(rel_parts) and rel_parts[0] in _PROTECTED_TOPLEVEL


def _rmtree_if_exists(resolved: Path) -> None:
    if not resolved.exists():
        return  # already gone (e.g. race) — nothing to do
    shutil.rmtree(resolved)


def apply_retention(repo_root: "str | Path", plan: RetentionPlan) -> None:
    runs_root = Path(repo_root).resolve() / "runs"

    for d in plan.run_dirs:
        resolved = Path(d).resolve()
        if not resolved.is_relative_to(runs_root) or resolved == runs_root:
            raise ValueError(f"refusing to delete outside runs/: {resolved}")
        if _refuses_as_run_dir(resolved, runs_root):
            raise ValueError(f"refusing to delete protected dir: {resolved}")
        _rmtree_if_exists(resolved)

    jobs_root = runs_root / "pipeline_jobs"
    for d in plan.job_dirs:
        resolved = Path(d).resolve()
        if not resolved.is_relative_to(jobs_root) or resolved == jobs_root:
            raise ValueError(f"refusing to delete outside pipeline_jobs/: {resolved}")
        _rmtree_if_exists(resolved)


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
