"""ops_health — one all-UTC snapshot: version, freshness, jobs, traders."""
import json
from pathlib import Path

from ops_health import build_ops_health, deployed_version


def _repo(tmp_path):
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/quant-trading-dashboard\n")
    (tmp_path / ".git" / "refs" / "heads" / "quant-trading-dashboard").write_text("abc123\n")
    m = tmp_path / "research" / "manifests"
    m.mkdir(parents=True)
    (m / "factor_values_eth.meta.json").write_text(json.dumps(
        {"generated_at": "2026-07-04T00:00:00+00:00", "index_end": "2026-07-03T23:00:00+00:00"}))
    j = tmp_path / "runs" / "pipeline_jobs" / "20260704T000000Z-aaaaaa"
    j.mkdir(parents=True)
    (j / "job.json").write_text(json.dumps(
        {"status": "succeeded", "created_at": "2026-07-04T00:00:00+00:00"}))
    t = tmp_path / "runs" / "testnet" / "eth_s5_paper"
    t.mkdir(parents=True)
    (t / "control.json").write_text(json.dumps({"desired_state": "running"}))
    (t / "testnet_status.json").write_text(json.dumps(
        {"live": {"status": "running", "updated_at": "2026-07-04T09:00:00+00:00",
                  "equity": 10000.0, "trades": 0}}))
    return tmp_path


def test_deployed_version_reads_head_and_loose_ref(tmp_path):
    v = deployed_version(_repo(tmp_path))
    assert v == {"branch": "quant-trading-dashboard", "commit": "abc123"}


def test_deployed_version_falls_back_to_packed_refs(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "refs" / "heads" / "quant-trading-dashboard").unlink()
    (repo / ".git" / "packed-refs").write_text(
        "def456 refs/heads/quant-trading-dashboard\n")
    assert deployed_version(repo)["commit"] == "def456"


def test_deployed_version_missing_git_is_none_fields(tmp_path):
    assert deployed_version(tmp_path) == {"branch": None, "commit": None}


def test_build_ops_health_aggregates_all_sections(tmp_path):
    h = build_ops_health(_repo(tmp_path))
    assert h["schema_version"] == 1
    assert h["version"]["commit"] == "abc123"
    assert h["factor_freshness"] == [{
        "symbol": "eth",
        "generated_at": "2026-07-04T00:00:00+00:00",
        "index_end": "2026-07-03T23:00:00+00:00",
    }]
    assert h["jobs"] == {"queued": 0, "running": 0, "succeeded": 1,
                         "failed": 0, "canceled": 0}
    assert h["traders"] == [{
        "testnet_id": "eth_s5_paper", "desired_state": "running",
        "status": "running", "updated_at": "2026-07-04T09:00:00+00:00",
        "equity": 10000.0, "trades": 0,
    }]


def test_build_ops_health_empty_repo_does_not_crash(tmp_path):
    h = build_ops_health(tmp_path)
    assert h["factor_freshness"] == [] and h["traders"] == []
