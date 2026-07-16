import numpy as np
import pandas as pd
from research.hermes import orchestrator as orch
from research.hermes.orchestrator import run_foundry, Budget
from research.hermes.gatekeeper import GateConfig
from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

_OOS = "2030-01-01"


def _ohlcv(idx):
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": 1.0}, index=idx)


def test_run_foundry_stops_when_pause_file_appears(tmp_path, monkeypatch):
    idx = pd.date_range("2022-01-01", periods=72, freq="1h", tz="UTC")
    panel = pd.DataFrame({"funding_z": np.arange(72.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    # three hypotheses queued; the pause file exists from the start → stop before any
    monkeypatch.setattr(orch, "build_queue", lambda **k: [
        Hypothesis(f"h{i}", "x", SOURCE_ZOO) for i in range(3)])
    calls = {"n": 0}
    def fake_process(*a, **k):
        calls["n"] += 1
        return "rejected"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    pause = tmp_path / "p.pause"
    pause.write_text("stop", encoding="utf-8")

    summary = run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=5, early_stop_after=99),
                          zoo_dir=tmp_path, oos_start=_OOS, ohlcv=_ohlcv(idx),
                          pause_file=str(pause), sources=())

    assert summary.get("killswitch_paused") is True
    assert calls["n"] == 0            # stopped before running any hypothesis
