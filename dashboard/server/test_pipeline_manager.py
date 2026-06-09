"""Tests for dashboard/server/pipeline_manager.py (global-serial executor)."""
from __future__ import annotations

import pipeline_jobs as pj
from pipeline_manager import Manager


def _runner(codes):
    """Injectable runner: writes a marker, returns codes.get(stage, 0)."""
    calls = []

    def run(repo_root, stage_id, fp):
        calls.append(stage_id)
        fp.write(f"ran {stage_id}\n")
        return codes.get(stage_id, 0)

    run.calls = calls
    return run


def test_stage_job_runs_one_step_succeeds(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "succeeded"
    assert out["steps"][0]["status"] == "succeeded"
    assert r.calls == ["1"]
    assert "ran 1" in pj.tail_log(tmp_path, job["job_id"])


def test_pipeline_job_runs_steps_in_order(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "succeeded"
    assert r.calls == pj.pipeline_sequence()


def test_failing_step_stops_chain(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    r = _runner({"1": 2})   # stage 1 exits non-zero
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "failed"
    assert out["exit_code"] == 2
    assert r.calls == ["0a", "0", "1"]   # stopped after the failure
    assert out["steps"][2]["status"] == "failed"


def test_cancel_flag_stops_before_next_step(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    job["cancel"] = True
    pj.write_job(tmp_path, job)
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "canceled"
    assert r.calls == []   # never started any step


def test_oldest_queued_picked_first(tmp_path):
    a = pj.create_job(tmp_path, kind="stage", stage="0a")
    b = pj.create_job(tmp_path, kind="stage", stage="1")
    a["created_at"] = "2026-01-01T00:00:00+00:00"
    b["created_at"] = "2026-01-02T00:00:00+00:00"
    pj.write_job(tmp_path, a)
    pj.write_job(tmp_path, b)
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert pj.read_job(tmp_path, a["job_id"])["status"] == "succeeded"
    assert pj.read_job(tmp_path, b["job_id"])["status"] == "queued"  # still waiting


def test_reconcile_startup_marks_stale_running_failed(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    job["status"] = "running"
    pj.write_job(tmp_path, job)
    Manager(tmp_path, runner=_runner({})).reconcile_startup()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "failed"
    assert "interrupted" in out["error"]
