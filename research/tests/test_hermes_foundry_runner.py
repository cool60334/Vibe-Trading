from pathlib import Path

import pytest


def test_scripted_llm_wraps_bodies_in_dirty_markdown_and_records_prompts():
    from research.tests.hermes_support import ScriptedLLM
    from research.hermes.forge import extract_code
    llm = ScriptedLLM(["def compute(df):\n    return df['close']"])
    out = llm.complete("PROMPT-A")
    assert "```" in out and "Certainly" in out            # dirty: fence + preamble
    assert extract_code(out) == "def compute(df):\n    return df['close']"
    assert llm.prompts == ["PROMPT-A"]


def test_resolve_image_id_returns_immutable_id(monkeypatch):
    import subprocess
    from research.hermes.foundry_runner import resolve_image_id
    def fake_run(cmd, **k):
        assert cmd[:4] == ["docker", "image", "inspect", "talos-sandbox:test"]
        return subprocess.CompletedProcess(cmd, 0, stdout="sha256:" + "e" * 64 + "\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_image_id("talos-sandbox:test") == "sha256:" + "e" * 64

def test_resolve_image_id_raises_on_unresolvable(monkeypatch):
    import subprocess
    from research.hermes.foundry_runner import resolve_image_id
    from research.hermes.sandbox import SandboxError
    monkeypatch.setattr(subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such image"))
    with pytest.raises(SandboxError, match="does not resolve|No such image"):
        resolve_image_id("nope:zzz")

def test_load_ohlcv_reads_close_and_utc_index(tmp_path):
    import pandas as pd
    from research.hermes.foundry_runner import load_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    p = tmp_path / "o.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(p)
    got = load_ohlcv(p)
    assert "close" in got.columns and got.index.tz is not None

def test_load_ohlcv_rejects_missing_close(tmp_path):
    import pandas as pd
    from research.hermes.foundry_runner import load_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    p = tmp_path / "o.parquet"
    pd.DataFrame({"volume": [1.0, 2, 3]}, index=idx).to_parquet(p)
    with pytest.raises(ValueError, match="close"):
        load_ohlcv(p)


def _queue_job(tmp_path, symbol, ohlcv_path, created_at):
    from research.hermes.orchestrator import enqueue_foundry_job
    p = enqueue_foundry_job(symbol, tmp_path, {
        "oos_start": "2025-01-01", "interval": "1H", "horizon_h": 24,
        "ohlcv_path": str(ohlcv_path)})
    import json
    job = json.loads(Path(p).read_text()); job["created_at"] = created_at
    Path(p).write_text(json.dumps(job)); return p

def test_jobs_run_in_created_at_order(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-02T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-01T00:00:00+00:00")
    seen = []
    def fake_run_job(job_path, **k):
        import json; seen.append(json.loads(Path(job_path).read_text())["symbol"])
        return {"candidate": 0}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert seen == ["btc", "eth"]                      # earlier created_at first

def test_infra_error_aborts_whole_queue(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.sandbox import SandboxError
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        if sym == "eth":
            raise SandboxError("docker image does not resolve")     # infra
        return {"candidate": 0}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    with pytest.raises(SandboxError):
        fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["eth"]                              # btc never ran

def test_job_level_failure_continues_queue(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        if sym == "eth":
            raise ValueError("bad factor")             # NOT infra
        return {"candidate": 1}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["eth", "btc"]                       # btc still ran despite eth failing
