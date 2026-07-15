from datetime import datetime, timezone
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
