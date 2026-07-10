import numpy as np
import pandas as pd
import pytest
from research.hermes.orchestrator import make_run_sandbox


# Foundry's OOS lock is mandatory. Fixtures predate this cutoff, so the whole
# fixture panel survives the split while the guard itself still runs.
_TEST_OOS = "2030-01-01"


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


def test_early_stop_after_consecutive_failures():
    from research.hermes.orchestrator import Budget, should_early_stop
    b = Budget(max_factors=100, early_stop_after=3)
    assert should_early_stop(["forge_failed", "rejected", "forge_failed"], b) is True
    assert should_early_stop(["forge_failed", "candidate", "forge_failed"], b) is False  # a win resets
    assert should_early_stop(["forge_failed", "rejected"], b) is False                    # under threshold


def test_budget_caps_factor_count():
    from research.hermes.orchestrator import Budget
    assert Budget(max_factors=2, early_stop_after=99).max_factors == 2


def test_run_foundry_respects_budget_and_early_stop(tmp_path, monkeypatch):
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    panel = pd.DataFrame({"close": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis(f"h{i}", f"x{i}", SOURCE_ZOO) for i in range(20)])
    # every hypothesis fails -> early stop should fire before all 20 processed
    seen = []
    def fake_process(hyp, *a, **k): seen.append(hyp.id); return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)
    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=20, early_stop_after=3),
                          zoo_dir=tmp_path, run_sandbox=object(), oos_start=_TEST_OOS)
    assert len(seen) == 3                             # stopped after 3 consecutive fails
    assert summary["forge_failed"] == 3 and summary["candidate"] == 0


def test_run_foundry_raises_when_panel_has_no_close(tmp_path, monkeypatch):
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    monkeypatch.setattr(orch, "load_features",
                        lambda s, manifests_dir=None: pd.DataFrame({"funding_z": np.arange(50.0)}, index=idx))
    with pytest.raises(ValueError, match="close"):      # agy 4a
        run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                    llm=object(), sandbox=object(), budget=Budget(), zoo_dir=tmp_path,
                    oos_start=_TEST_OOS)


def test_run_foundry_wires_graveyard_values_into_existing_and_dead(tmp_path, monkeypatch):
    """agy 3c: existing.join(pd.read_parquet(gpath), ...) must actually surface
    buried-factor VALUES to process_hypothesis, not merely avoid crashing."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget, _graveyard_path, _merge_column
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=60, freq="1D")
    panel = pd.DataFrame({"close": np.arange(60.0), "some_factor": np.arange(60.0) * 2}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h0", "x0", SOURCE_ZOO)])

    # pre-seed a graveyard parquet with a dead factor's values, same helper the
    # orchestrator itself uses to write it (_merge_into_graveyard's building block)
    gpath = _graveyard_path("eth", tmp_path)
    gpath.parent.mkdir(parents=True, exist_ok=True)
    dead_series = pd.Series(np.arange(60.0) * 3, index=idx)
    _merge_column(None, "dead_factor_x", dead_series).to_parquet(gpath)

    captured = {}
    def fake_process(hyp, panel_, ohlcv_, daily_regime_, existing_and_dead, *rest, **kw):
        captured["existing_and_dead"] = existing_and_dead
        return "candidate"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
               llm=object(), sandbox=object(),
               budget=Budget(max_factors=5, early_stop_after=99), zoo_dir=tmp_path,
               oos_start=_TEST_OOS)

    cols = captured["existing_and_dead"].columns
    assert "dead_factor_x" in cols            # came from the graveyard parquet
    assert "some_factor" in cols              # came from the live feature panel


def test_run_foundry_daily_regime_fallback_is_neutral_and_daily_indexed(tmp_path, monkeypatch):
    """agy 4b: the fallback must ffill from a DAILY-normalized index, not the
    panel's own (finer) frequency."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    # hourly panel: finer than daily, so a panel-frequency fallback would be
    # distinguishable from a correctly daily-normalized one by row count/index.
    idx = pd.date_range("2022-01-01", periods=72, freq="1h")
    panel = pd.DataFrame({"close": np.arange(72.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h0", "x0", SOURCE_ZOO)])

    captured = {}
    def fake_process(hyp, panel_, ohlcv_, daily_regime_, *rest, **kw):
        captured["daily_regime"] = daily_regime_
        return "candidate"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
               llm=object(), sandbox=object(),
               budget=Budget(max_factors=5, early_stop_after=99), zoo_dir=tmp_path,
               oos_start=_TEST_OOS)

    dr = captured["daily_regime"]
    assert (dr == "neutral").all()
    assert len(dr) < len(panel)                        # daily, not hourly, granularity
    assert (dr.index == dr.index.normalize()).all()     # every stamp is midnight


def test_run_foundry_loads_daily_regime_from_regime_manifest_when_present(tmp_path, monkeypatch):
    """regime_<sym>.json (stage 2.5 output) should be used in preference to the
    all-neutral fallback when it exists and is well-formed."""
    import json
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=72, freq="1h")
    panel = pd.DataFrame({"close": np.arange(72.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h0", "x0", SOURCE_ZOO)])

    manifest = {
        "schema_version": 1, "symbol": "ETH", "generated_at": "2022-01-01T00:00:00+00:00",
        "detector_params": {}, "current_regime": "bull",
        "distribution": {"bull": 0.67, "bear": 0.0, "neutral": 0.33},
        "period_days": 3, "total_daily_bars": 3,
        "breakdown": [
            {"date": "2022-01-01", "regime": "bull"},
            {"date": "2022-01-02", "regime": "bull"},
            {"date": "2022-01-03", "regime": "neutral"},
        ],
    }
    (tmp_path / "regime_eth.json").write_text(json.dumps(manifest), encoding="utf-8")

    captured = {}
    def fake_process(hyp, panel_, ohlcv_, daily_regime_, *rest, **kw):
        captured["daily_regime"] = daily_regime_
        return "candidate"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
               llm=object(), sandbox=object(),
               budget=Budget(max_factors=5, early_stop_after=99), zoo_dir=tmp_path,
               oos_start=_TEST_OOS)

    dr = captured["daily_regime"]
    assert len(dr) == 3
    assert list(dr.values) == ["bull", "bull", "neutral"]     # real labels, not all-neutral
    assert (dr.index == dr.index.normalize()).all()           # daily-normalized index


def test_run_foundry_falls_back_to_neutral_when_regime_manifest_malformed(tmp_path, monkeypatch):
    """A present-but-broken regime_<sym>.json must degrade to the neutral
    fallback, not crash the foundry run (regime_ic is informational only)."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=72, freq="1h")
    panel = pd.DataFrame({"close": np.arange(72.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h0", "x0", SOURCE_ZOO)])

    (tmp_path / "regime_eth.json").write_text("{not valid json", encoding="utf-8")

    captured = {}
    def fake_process(hyp, panel_, ohlcv_, daily_regime_, *rest, **kw):
        captured["daily_regime"] = daily_regime_
        return "candidate"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
               llm=object(), sandbox=object(),
               budget=Budget(max_factors=5, early_stop_after=99), zoo_dir=tmp_path,
               oos_start=_TEST_OOS)

    dr = captured["daily_regime"]
    assert (dr == "neutral").all()
    assert (dr.index == dr.index.normalize()).all()


def test_enqueue_writes_job_and_runner_reconciles(tmp_path, monkeypatch):
    import json
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import enqueue_foundry_job, run_foundry_job

    job_path = enqueue_foundry_job("eth", runs_dir=tmp_path,
                                   params={"interval": "1D", "horizon_h": 24,
                                           "oos_start": _TEST_OOS})
    assert job_path.exists()
    job = json.loads(job_path.read_text())
    assert job["symbol"] == "eth" and job["status"] == "queued"

    called = {}

    def _fake_run_foundry(symbol, *a, **k):
        called["symbol"] = symbol
        return {"candidate": 1}
    monkeypatch.setattr(orch, "run_foundry", _fake_run_foundry)
    summary = run_foundry_job(job_path, manifests_dir=tmp_path, llm=object(),
                              sandbox=object(), zoo_dir=tmp_path)
    assert called["symbol"] == "eth" and summary["candidate"] == 1
    assert json.loads(job_path.read_text())["status"] == "done"     # reconciled


def test_run_foundry_job_marks_failed_on_exception(tmp_path, monkeypatch):
    """A crashed run_foundry must not leave the job stuck at status="queued"
    forever -- rewrite status="failed"+error+finished_at, then re-raise."""
    import json
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import enqueue_foundry_job, run_foundry_job

    job_path = enqueue_foundry_job("eth", runs_dir=tmp_path,
                                   params={"interval": "1D", "horizon_h": 24,
                                           "oos_start": _TEST_OOS})

    def _boom(symbol, *a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(orch, "run_foundry", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        run_foundry_job(job_path, manifests_dir=tmp_path, llm=object(),
                        sandbox=object(), zoo_dir=tmp_path)

    job = json.loads(job_path.read_text())
    assert job["status"] == "failed"
    assert "boom" in job["error"]
    assert "finished_at" in job


# ── OOS lock: foundry_split must actually gate what reaches forge/evaluate ──
#
# Phase 0 built foundry_split ("pipeline walk-forward OOS is reserved; Foundry
# must never see index >= oos_start") but nothing in the production path ever
# called it: run_foundry fed load_features()'s FULL history straight to forge()
# and evaluate(). 37% of the ETH panel sits at/after oos_start, 5.8% at/after
# final_holdout_start. No real run had happened yet (LLMCoder was never wired),
# so nothing was contaminated -- this is a pre-flight guard.

def test_run_foundry_never_feeds_oos_rows_to_forge_or_evaluate(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    oos_start = "2025-01-01"
    idx = pd.date_range("2024-06-01", "2026-06-01", freq="1D", tz="UTC")
    panel = pd.DataFrame({"close": np.arange(float(len(idx)))}, index=idx)
    assert (idx >= pd.Timestamp(oos_start, tz="UTC")).any(), "fixture must span the OOS boundary"

    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue",
                        lambda **k: [Hypothesis("h1", "x", SOURCE_ZOO)])

    seen = {}
    def spy(hyp, panel_arg, ohlcv_arg, *a, **k):
        seen["panel_max"] = panel_arg.index.max()
        seen["ohlcv_max"] = ohlcv_arg.index.max()
        return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", spy)

    run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                llm=object(), sandbox=object(), budget=Budget(),
                zoo_dir=tmp_path, run_sandbox=object(), oos_start=oos_start)

    cutoff = pd.Timestamp(oos_start, tz="UTC")
    assert seen["panel_max"] < cutoff, f"forge saw OOS data up to {seen['panel_max']}"
    assert seen["ohlcv_max"] < cutoff, f"evaluate saw OOS data up to {seen['ohlcv_max']}"


def test_run_foundry_requires_oos_start(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    idx = pd.date_range("2024-06-01", periods=50, freq="1D", tz="UTC")
    monkeypatch.setattr(orch, "load_features",
                        lambda s, manifests_dir=None: pd.DataFrame({"close": np.arange(50.0)}, index=idx))
    with pytest.raises(TypeError):        # oos_start is a required keyword-only arg
        run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                    llm=object(), sandbox=object(), budget=Budget(), zoo_dir=tmp_path)
