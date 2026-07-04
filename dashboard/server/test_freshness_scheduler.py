from datetime import datetime, timezone, timedelta

import pipeline_jobs as pj
import freshness_scheduler as fs


import json as _json

from freshness_scheduler import running_symbols, tick


def _write_control(repo_root, testnet_id, desired_state, symbol):
    d = repo_root / "runs" / "testnet" / testnet_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "control.json").write_text(
        _json.dumps({"desired_state": desired_state, "symbol": symbol}),
        encoding="utf-8",
    )


def test_tick_enqueues_when_none_active(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    now = datetime.now(timezone.utc)
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)
    assert len(ids) == 1
    jobs = pj.list_jobs(tmp_path, limit=10)
    assert jobs[0]["kind"] == "live_refresh"
    assert jobs[0]["symbol"] == "sol"


def test_tick_skips_when_active_exists(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    now = datetime.now(timezone.utc)
    fs.tick(tmp_path, ["sol"], now, interval_sec=3600)      # enqueue one
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)  # second tick
    assert ids == []  # queued job still active -> no duplicate


def test_tick_reenqueues_when_active_is_zombie(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    # create a stale 'running' job by hand
    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    job["status"] = "running"
    job["created_at"] = old.isoformat()
    pj.write_job(tmp_path, job)
    now = datetime.now(timezone.utc)
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)  # >2*interval old
    assert len(ids) == 1  # zombie ignored -> fresh job enqueued


def test_running_symbols_collects_only_running(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    _write_control(tmp_path, "eth_z_paper", "running", "ETH-USDT-SWAP")
    assert running_symbols(tmp_path) == {"sol", "eth"}


def test_running_symbols_empty_tree(tmp_path):
    assert running_symbols(tmp_path) == set()


def test_running_symbols_ignores_corrupt_control(tmp_path):
    d = tmp_path / "runs" / "testnet" / "bad"
    d.mkdir(parents=True)
    (d / "control.json").write_text("{not json", encoding="utf-8")
    assert running_symbols(tmp_path) == set()


def test_tick_skips_symbols_without_running_trader(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    ids = tick(tmp_path, ["sol", "xrp"], now, 3600.0)
    assert len(ids) == 1  # only sol enqueued


def test_tick_ignore_controls_env_refreshes_all(tmp_path, monkeypatch):
    monkeypatch.setenv("FRESHNESS_IGNORE_CONTROLS", "1")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    ids = tick(tmp_path, ["sol", "xrp"], now, 3600.0)
    assert len(ids) == 2


def test_tick_no_controls_at_all_enqueues_nothing(tmp_path):
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    assert tick(tmp_path, ["sol"], now, 3600.0) == []


def test_tick_ignore_controls_env_false_keeps_filtering(tmp_path, monkeypatch):
    monkeypatch.setenv("FRESHNESS_IGNORE_CONTROLS", "false")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    ids = tick(tmp_path, ["sol", "xrp"], now, 3600.0)
    assert ids == []  # "false" must NOT be treated as ignore-controls
