import os
from pathlib import Path
import pipeline_jobs as pj
import pipeline_manager as pm
from pipeline_manager import Manager


def test_cleanup_overlay_removes_the_per_run_cache(tmp_path):
    # the overlay is a per-run cache; leaving it behind accumulates orphan
    # parquet files that fill the disk.
    (tmp_path / "foundry_overlay_eth.parquet").write_bytes(b"x")
    (tmp_path / "foundry_manifest_eth.json").write_text("{}", encoding="utf-8")

    pm.cleanup_overlay(tmp_path)

    assert not (tmp_path / "foundry_overlay_eth.parquet").exists()
    assert not (tmp_path / "foundry_manifest_eth.json").exists()


def test_stage_env_inherits_the_parent_environment(monkeypatch):
    # passing only overrides to subprocess would wipe PATH and break the stage.
    monkeypatch.setenv("PATH", "/usr/bin")
    env = pm.stage_env({"RESEARCH_INCLUDE_FOUNDRY": "1"})
    assert env["PATH"] == "/usr/bin"
    assert env["RESEARCH_INCLUDE_FOUNDRY"] == "1"


def test_cleanup_overlay_runs_on_job_failure(tmp_path):
    # the finally block must run even on failure to prevent accumulating orphans.
    job = pj.create_job(tmp_path, kind="stage", stage="1")

    # Set up overlay files in the log directory before running
    log_dir = pj.log_path(tmp_path, job["job_id"]).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "foundry_overlay_eth.parquet").write_bytes(b"x")
    (log_dir / "foundry_manifest_eth.json").write_text("{}", encoding="utf-8")

    # Inject a runner that fails with exit code 1
    def failing_runner(repo_root, stage_id, symbol, fp, stress=False, interval="1H", live_refresh=False):
        return 1

    mgr = Manager(tmp_path, runner=failing_runner)
    mgr.execute_job(job)

    # Job should be marked failed, but overlay files must be cleaned up
    result = pj.read_job(tmp_path, job["job_id"])
    assert result["status"] == "failed"
    assert not (log_dir / "foundry_overlay_eth.parquet").exists()
    assert not (log_dir / "foundry_manifest_eth.json").exists()
