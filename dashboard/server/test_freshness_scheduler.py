from datetime import datetime, timezone, timedelta

import pipeline_jobs as pj
import freshness_scheduler as fs


def test_tick_enqueues_when_none_active(tmp_path):
    now = datetime.now(timezone.utc)
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)
    assert len(ids) == 1
    jobs = pj.list_jobs(tmp_path, limit=10)
    assert jobs[0]["kind"] == "live_refresh"
    assert jobs[0]["symbol"] == "sol"


def test_tick_skips_when_active_exists(tmp_path):
    now = datetime.now(timezone.utc)
    fs.tick(tmp_path, ["sol"], now, interval_sec=3600)      # enqueue one
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)  # second tick
    assert ids == []  # queued job still active -> no duplicate


def test_tick_reenqueues_when_active_is_zombie(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    # create a stale 'running' job by hand
    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    job["status"] = "running"
    job["created_at"] = old.isoformat()
    pj.write_job(tmp_path, job)
    now = datetime.now(timezone.utc)
    ids = fs.tick(tmp_path, ["sol"], now, interval_sec=3600)  # >2*interval old
    assert len(ids) == 1  # zombie ignored -> fresh job enqueued
