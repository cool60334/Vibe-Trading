import os
from pathlib import Path
import pipeline_manager as pm


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
