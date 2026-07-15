from datetime import datetime, timezone
import pipeline_jobs as pj
import discovery_pipeline_scheduler as dps


def test_tick_enqueues_and_dedups_against_manual_pipeline(tmp_path, monkeypatch):
    monkeypatch.delenv("GLOBAL_PAUSE_FILE", raising=False)
    ids = dps.tick(tmp_path, ["eth"], datetime.now(timezone.utc), 86400)
    assert len(ids) == 1
    # a manually-queued pipeline for the same symbol blocks the next enqueue
    pj.create_job(tmp_path, kind="pipeline", symbol="btc")
    assert dps.tick(tmp_path, ["btc"], datetime.now(timezone.utc), 86400) == []


def test_tick_skips_on_global_pause(tmp_path, monkeypatch):
    (tmp_path / "g.pause").write_text("x", encoding="utf-8")
    monkeypatch.setenv("GLOBAL_PAUSE_FILE", str(tmp_path / "g.pause"))
    assert dps.tick(tmp_path, ["eth"], datetime.now(timezone.utc), 86400) == []
