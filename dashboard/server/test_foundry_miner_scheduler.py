from datetime import datetime, timedelta, timezone
import pipeline_jobs as pj
import foundry_miner_scheduler as fms


def test_tick_enqueues_one_mine_per_symbol(tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDRY_PAUSE_FILE", raising=False)
    monkeypatch.delenv("GLOBAL_PAUSE_FILE", raising=False)
    ids = fms.tick(tmp_path, ["eth", "btc"], datetime.now(timezone.utc), 3600)
    assert len(ids) == 2
    # a second tick dedups against the active jobs
    assert fms.tick(tmp_path, ["eth", "btc"], datetime.now(timezone.utc), 3600) == []


def test_tick_skips_when_pause_file_present(tmp_path, monkeypatch):
    (tmp_path / "p.pause").write_text("x", encoding="utf-8")
    monkeypatch.setenv("FOUNDRY_PAUSE_FILE", str(tmp_path / "p.pause"))
    assert fms.tick(tmp_path, ["eth"], datetime.now(timezone.utc), 3600) == []


def test_old_but_still_queued_job_blocks_reenqueue(tmp_path, monkeypatch):
    # A foundry_mine job is deliberately low-priority (pipeline_manager sorts it
    # last), so it can legitimately sit `queued` far longer than 2*interval_sec
    # while still waiting its turn -- that is not "dead", and must not cause the
    # scheduler to pile up a duplicate every tick.
    monkeypatch.delenv("FOUNDRY_PAUSE_FILE", raising=False)
    monkeypatch.delenv("GLOBAL_PAUSE_FILE", raising=False)
    now = datetime.now(timezone.utc)
    job = pj.create_job(tmp_path, kind="foundry_mine", symbol="eth", config={})
    job["created_at"] = (now - timedelta(seconds=100_000)).isoformat()  # >> 2*3600
    pj.write_job(tmp_path, job)

    assert fms.tick(tmp_path, ["eth"], now, 3600) == []


def test_old_running_job_is_treated_as_dead_and_reenqueued(tmp_path, monkeypatch):
    # A `running` job past the age window really might reflect a dead runner
    # (the process died mid-step) -- unlike `queued`, staleness still applies.
    monkeypatch.delenv("FOUNDRY_PAUSE_FILE", raising=False)
    monkeypatch.delenv("GLOBAL_PAUSE_FILE", raising=False)
    now = datetime.now(timezone.utc)
    job = pj.create_job(tmp_path, kind="foundry_mine", symbol="eth", config={})
    job["status"] = "running"
    job["created_at"] = (now - timedelta(seconds=100_000)).isoformat()  # >> 2*3600
    pj.write_job(tmp_path, job)

    ids = fms.tick(tmp_path, ["eth"], now, 3600)
    assert len(ids) == 1
    assert ids[0] != job["job_id"]
