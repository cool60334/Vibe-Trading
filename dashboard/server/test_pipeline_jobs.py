"""Tests for dashboard/server/pipeline_jobs.py (job-file contract + stage table)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline_jobs as pj


def test_allowed_stage_ids_are_ui_stages():
    assert pj.allowed_stage_ids() == ["0a", "0", "1", "2", "2.5", "3", "4", "5"]


def test_pipeline_sequence_full_chain():
    assert pj.pipeline_sequence() == [
        "0a", "0", "1", "2", "2b", "2.5", "3", "3diag", "4", "5"
    ]


def test_stage_command_maps_to_module():
    assert pj.stage_command("1") == ["-m", "research.pipeline.stage1_factors"]
    assert pj.stage_command("2b") == ["-m", "research.pipeline.stage2b_compile_signal"]
    assert pj.stage_command("3diag") == ["-m", "research.pipeline.stage3_diagnose"]


def test_stage_command_unknown_raises():
    with pytest.raises(KeyError):
        pj.stage_command("99")


def test_create_stage_job(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    assert job["kind"] == "stage"
    assert job["stage"] == "1"
    assert job["status"] == "queued"
    assert [s["stage"] for s in job["steps"]] == ["1"]
    # persisted to runs/pipeline_jobs/<id>/job.json
    p = tmp_path / "runs" / "pipeline_jobs" / job["job_id"] / "job.json"
    assert json.loads(p.read_text())["job_id"] == job["job_id"]


def test_create_pipeline_job_has_full_sequence(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    assert job["stage"] is None
    assert [s["stage"] for s in job["steps"]] == pj.pipeline_sequence()


def test_create_stage_job_rejects_bad_stage(tmp_path):
    with pytest.raises(ValueError):
        pj.create_job(tmp_path, kind="stage", stage="2b")   # chain-only, not a UI stage
    with pytest.raises(ValueError):
        pj.create_job(tmp_path, kind="bogus")


def test_read_and_list_jobs_newest_first(tmp_path):
    a = pj.create_job(tmp_path, kind="stage", stage="0a")
    b = pj.create_job(tmp_path, kind="stage", stage="1")
    # force distinct created_at ordering
    a["created_at"] = "2026-01-01T00:00:00+00:00"
    b["created_at"] = "2026-01-02T00:00:00+00:00"
    pj.write_job(tmp_path, a)
    pj.write_job(tmp_path, b)
    ids = [j["job_id"] for j in pj.list_jobs(tmp_path)]
    assert ids[0] == b["job_id"]            # newest first
    assert pj.read_job(tmp_path, a["job_id"])["stage"] == "0a"
    assert pj.read_job(tmp_path, "nope") is None
