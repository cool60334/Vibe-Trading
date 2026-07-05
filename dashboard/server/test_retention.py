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
