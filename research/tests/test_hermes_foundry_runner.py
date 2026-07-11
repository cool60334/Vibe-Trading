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
