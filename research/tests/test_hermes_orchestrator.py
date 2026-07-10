import numpy as np
import pandas as pd
import pytest
from research.hermes.orchestrator import make_run_sandbox


class _FakeSandbox:
    """Stands in for DockerSandbox: 'runs' by writing a candidate parquet."""
    def __init__(self, fn): self.fn = fn
    def run(self, source, input_parquet, output_dir):
        panel = pd.read_parquet(input_parquet)
        out = pd.DataFrame({"candidate": self.fn(panel)})
        from pathlib import Path
        p = Path(output_dir) / "candidate.parquet"; out.to_parquet(p)
        return str(p)


def test_run_sandbox_roundtrips_panel_to_series(tmp_path):
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    sb = _FakeSandbox(lambda p: p["close"].pct_change(3))
    run = make_run_sandbox(sb, scratch_dir=tmp_path)
    s = run("def compute(df): ...", panel)
    assert isinstance(s, pd.Series)
    assert s.index.equals(panel.index)               # index restored from panel
    pd.testing.assert_series_equal(s, panel["close"].pct_change(3), check_names=False)


def test_run_sandbox_raises_on_length_mismatch(tmp_path):
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    # sandboxed compute() returns a shorter series than the input panel
    sb = _FakeSandbox(lambda p: p["close"].iloc[:-5].reset_index(drop=True))
    run = make_run_sandbox(sb, scratch_dir=tmp_path)
    with pytest.raises(ValueError, match="sandbox output length"):
        run("def compute(df): ...", panel)


def test_process_hypothesis_pass_writes_candidate_and_card(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult
    from research.hermes.evidence_store import load_cards

    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    series = pd.Series(np.arange(300.0), index=idx)

    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(True, 1, code="c", series=series))
    metrics = {"gross_ic": 0.05, "ic_nonoverlap": 0.04, "ir": 0.3, "dsr": 0.9, "pbo": None,
               "turnover": 0.1, "n_samples": 300, "regime_ic": {}, "yearly_ic": {},
               "nearest_factor": None, "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(True, metrics, ""))
    events = []
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: events.append(kw["kind"]))

    hyp = Hypothesis("h1", "mom", SOURCE_LLM)
    outcome = process_hypothesis(hyp, panel, panel, pd.Series("bull", index=idx),
                                 pd.DataFrame(index=idx), "eth", tmp_path,
                                 GateConfig(interval="1D", horizon_h=24),
                                 llm=object(), run_sandbox=object())
    assert outcome == "candidate"
    assert "factor_trial" in events                  # ledger written for future DSR
    cards = {c.factor_id: c for c in load_cards("eth", tmp_path)}
    assert cards["h1"].verdict == "candidate" and cards["h1"].gross_ic == 0.05


def test_process_hypothesis_forge_fail_writes_graveyard(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.forge import ForgeResult
    from research.hermes.evidence_store import load_cards
    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(False, 3, code="bad", death_reason="UnsafeCodeError: x"))
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)
    outcome = process_hypothesis(Hypothesis("h2", "x", SOURCE_LLM), panel, panel,
                                 pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                                 "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                                 llm=object(), run_sandbox=object())
    assert outcome == "forge_failed"
    assert load_cards("eth", tmp_path)[0].verdict == "graveyard"


def test_two_passing_factors_both_survive_in_candidate_parquet(tmp_path, monkeypatch):
    # agy 3b (fatal): write_candidate replaces the whole parquet — the loop must merge
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.candidate_store import _candidate_path
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult
    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    metrics = {"gross_ic": 0.05, "ic_nonoverlap": 0.04, "ir": 0.3, "dsr": 0.9, "pbo": None,
               "turnover": 0.1, "n_samples": 300, "regime_ic": {}, "yearly_ic": {},
               "nearest_factor": None, "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(True, metrics, ""))
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)
    for fid in ("f1", "f2"):
        s = pd.Series(np.arange(300.0), index=idx, name=fid)
        monkeypatch.setattr(orch, "forge", lambda *a, s=s, **k: ForgeResult(True, 1, code="c", series=s))
        process_hypothesis(Hypothesis(fid, fid, SOURCE_LLM), panel, panel,
                           pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                           "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                           llm=object(), run_sandbox=object())
    cand = pd.read_parquet(_candidate_path("eth", tmp_path))
    assert set(cand.columns) == {"f1", "f2"}          # f1 NOT clobbered by f2


def test_rejected_factor_series_lands_in_graveyard_parquet(tmp_path, monkeypatch):
    # agy 3c: dead factor VALUES must persist so 1A can numerically dedup (C-6)
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis, _graveyard_path
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult
    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    series = pd.Series(np.arange(300.0), index=idx)
    metrics = {"gross_ic": 0.001, "ic_nonoverlap": 0.0, "ir": 0.0, "dsr": 0.1, "pbo": None,
               "turnover": 0.1, "n_samples": 300, "regime_ic": {}, "yearly_ic": {},
               "nearest_factor": None, "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(True, 1, code="c", series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(False, metrics, "weak gross_ic"))
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)
    out = process_hypothesis(Hypothesis("dead1", "x", SOURCE_LLM), panel, panel,
                             pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                             "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                             llm=object(), run_sandbox=object())
    assert out == "rejected"
    grave = pd.read_parquet(_graveyard_path("eth", tmp_path))
    assert "dead1" in grave.columns                   # values persisted for C-6 dedup
