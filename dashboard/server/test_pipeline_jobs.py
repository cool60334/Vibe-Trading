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


def test_tail_log_returns_last_lines(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    lp = pj.log_path(tmp_path, job["job_id"])
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")
    assert pj.tail_log(tmp_path, job["job_id"], lines=3) == "line7\nline8\nline9"
    assert pj.tail_log(tmp_path, "missing") == ""


def test_request_cancel_queued_marks_canceled(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    out = pj.request_cancel(tmp_path, job["job_id"])
    assert out["status"] == "canceled"
    assert out["cancel"] is True


def test_request_cancel_running_sets_flag_only(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    job["status"] = "running"
    pj.write_job(tmp_path, job)
    out = pj.request_cancel(tmp_path, job["job_id"])
    assert out["status"] == "running"   # not killed mid-run
    assert out["cancel"] is True


def test_request_cancel_missing_returns_none(tmp_path):
    assert pj.request_cancel(tmp_path, "missing") is None


import importlib
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    import main as main_module
    importlib.reload(main_module)
    return TestClient(main_module.app), main_module


def test_endpoint_run_creates_queued_job(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/pipeline/run", json={"kind": "stage", "stage": "1"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued" and body["stage"] == "1"
    # listed
    jobs = c.get("/api/pipeline/jobs").json()
    assert any(j["job_id"] == body["job_id"] for j in jobs)


def test_endpoint_run_rejects_bad_stage(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    r = c.post("/api/pipeline/run", json={"kind": "stage", "stage": "99"})
    assert r.status_code == 400


def test_endpoint_get_job_includes_log_tail(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    jid = c.post("/api/pipeline/run", json={"kind": "pipeline"}).json()["job_id"]
    pj.log_path(tmp_path, jid).write_text("hello-log", encoding="utf-8")
    body = c.get(f"/api/pipeline/jobs/{jid}").json()
    assert body["log_tail"] == "hello-log"
    assert c.get("/api/pipeline/jobs/nope").status_code == 404


def test_endpoint_cancel(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    jid = c.post("/api/pipeline/run", json={"kind": "stage", "stage": "0a"}).json()["job_id"]
    body = c.post(f"/api/pipeline/jobs/{jid}/cancel").json()
    assert body["status"] == "canceled"


def test_symbol_aware_stages():
    for s in ["0a", "0", "1", "2", "2.5", "3", "3diag", "4"]:
        assert pj.stage_uses_symbol(s) is True
    for s in ["2b", "5"]:
        assert pj.stage_uses_symbol(s) is False


def test_create_job_stores_symbol(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="3", symbol="btc")
    assert job["symbol"] == "btc"
    assert pj.read_job(tmp_path, job["job_id"])["symbol"] == "btc"


def test_create_job_symbol_defaults_none(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    assert job["symbol"] is None


def test_endpoint_run_with_symbol(tmp_path, monkeypatch):
    # seed a config so _config_symbols() returns btc/eth
    research = tmp_path / "research"
    research.mkdir(parents=True)
    (research / "research_config.yaml").write_text(
        """
symbols:
  - {name: btc, okx_swap: BTC-USDT-SWAP, ccxt_bybit: "BTC/USDT:USDT"}
  - {name: eth, okx_swap: ETH-USDT-SWAP, ccxt_bybit: "ETH/USDT:USDT"}
period: 365
interval: "1H"
data_source: okx
engine: daily
fees: {maker_rate: 0.0002, taker_rate: 0.00055, slippage: 0.0005}
horizons_h: [8, 24, 72, 168]
""".strip(),
        encoding="utf-8",
    )
    c, _ = _client(tmp_path, monkeypatch)
    ok = c.post("/api/pipeline/run", json={"kind": "stage", "stage": "3", "symbol": "btc"})
    assert ok.status_code == 201 and ok.json()["symbol"] == "btc"
    bad = c.post("/api/pipeline/run", json={"kind": "stage", "stage": "3", "symbol": "zzz"})
    assert bad.status_code == 400
