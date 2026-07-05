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


def test_apply_refuses_runs_root_itself(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    plan.run_dirs.append(tmp_path / "runs")  # runs/ itself, not a child of it
    import pytest
    with pytest.raises(ValueError):
        apply_retention(repo, plan)
    assert (tmp_path / "runs").exists()
    assert (tmp_path / "runs" / "kept_referenced").exists()


def test_apply_refuses_paths_nested_under_protected_dirs(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    nested = tmp_path / "runs" / "testnet" / "some_state_subdir"
    nested.mkdir(parents=True)
    plan.run_dirs.append(nested)
    import pytest
    with pytest.raises(ValueError):
        apply_retention(repo, plan)
    assert nested.exists()


def test_apply_surfaces_permission_errors_instead_of_swallowing(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)

    def fake_rmtree(path, *a, **kw):
        raise PermissionError(f"cannot remove {path}")

    import retention as retention_module
    monkeypatch.setattr(retention_module.shutil, "rmtree", fake_rmtree)

    import pytest
    with pytest.raises(PermissionError):
        apply_retention(repo, plan)


def test_apply_tolerates_already_deleted_dir(tmp_path):
    repo = _make_repo(tmp_path)
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    # Simulate a race: the planned dir is gone by the time apply runs.
    import shutil as _shutil
    for d in plan.run_dirs:
        _shutil.rmtree(d)
    # Should not raise even though the dirs no longer exist.
    apply_retention(repo, plan)


def test_plan_retention_missing_runs_dir_returns_empty_plan(tmp_path):
    # No runs/ dir created at all under the repo root.
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "strategy_runs.json").write_text("{}", encoding="utf-8")
    plan = plan_retention(tmp_path, now=_NOW, keep_run_days=30, keep_job_days=90)
    assert plan.run_dirs == []
    assert plan.job_dirs == []


def test_plan_retention_missing_strategy_runs_json_keeps_everything(tmp_path):
    old = (_NOW - timedelta(days=60)).timestamp()
    runs = tmp_path / "runs"
    (runs / "old_orphan").mkdir(parents=True)
    os.utime(runs / "old_orphan", (old, old))
    # No research/ dir, no strategy_runs.json at all.
    plan = plan_retention(tmp_path, now=_NOW, keep_run_days=30, keep_job_days=90)
    assert plan.run_dirs == []  # unreadable registry -> fail-safe, keep everything


def test_plan_retention_unreadable_strategy_runs_json_keeps_everything(tmp_path):
    old = (_NOW - timedelta(days=60)).timestamp()
    runs = tmp_path / "runs"
    (runs / "old_orphan").mkdir(parents=True)
    os.utime(runs / "old_orphan", (old, old))
    research = tmp_path / "research"
    research.mkdir()
    (research / "strategy_runs.json").write_text("{not valid json", encoding="utf-8")
    plan = plan_retention(tmp_path, now=_NOW, keep_run_days=30, keep_job_days=90)
    assert plan.run_dirs == []  # malformed JSON -> fail-safe, keep everything


def test_plan_retention_malformed_job_json_is_skipped_not_crashed(tmp_path):
    repo = _make_repo(tmp_path)
    jobs_root = repo / "runs" / "pipeline_jobs"

    bad_json_dir = jobs_root / "bad_json"
    bad_json_dir.mkdir(parents=True)
    (bad_json_dir / "job.json").write_text("{not valid json", encoding="utf-8")

    missing_keys_dir = jobs_root / "missing_keys"
    missing_keys_dir.mkdir(parents=True)
    (missing_keys_dir / "job.json").write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    # A valid, deletable job dir alongside the broken ones must still be found.
    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    job_names = {p.name for p in plan.job_dirs}
    assert job_names == {"old_done"}
    assert "bad_json" not in job_names
    assert "missing_keys" not in job_names


def test_plan_retention_naive_finished_at_compares_against_utc_cutoff(tmp_path):
    repo = _make_repo(tmp_path)
    jobs_root = repo / "runs" / "pipeline_jobs"

    naive_old_dir = jobs_root / "naive_old"
    naive_old_dir.mkdir(parents=True)
    naive_old_iso = (_NOW - timedelta(days=120)).replace(tzinfo=None).isoformat()
    (naive_old_dir / "job.json").write_text(json.dumps(
        {"status": "succeeded", "finished_at": naive_old_iso}), encoding="utf-8")

    naive_fresh_dir = jobs_root / "naive_fresh"
    naive_fresh_dir.mkdir(parents=True)
    naive_fresh_iso = (_NOW - timedelta(days=5)).replace(tzinfo=None).isoformat()
    (naive_fresh_dir / "job.json").write_text(json.dumps(
        {"status": "succeeded", "finished_at": naive_fresh_iso}), encoding="utf-8")

    plan = plan_retention(repo, now=_NOW, keep_run_days=30, keep_job_days=90)
    job_names = {p.name for p in plan.job_dirs}
    assert "naive_old" in job_names
    assert "naive_fresh" not in job_names
