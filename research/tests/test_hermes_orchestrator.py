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


@pytest.mark.parametrize("passed,reason", [
    (True, ""),
    (False, "weak gross_ic 0.001 < 0.03"),
    (False, "net_ir -0.01 <= 0"),
    (False, "DSR 0.01 < 0.5"),
])
def test_every_evaluated_factor_lands_in_the_ledger(tmp_path, monkeypatch, passed, reason):
    """N is the multiple-testing debt. A factor that reached evaluate() WAS
    tested -- whatever the verdict -- so it must be counted. Skipping the losers
    undercounts N and lets only profitable factors shape the variance."""
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult

    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    series = pd.Series(np.arange(300.0), index=idx)
    metrics = {"gross_ic": 0.05, "ic_nonoverlap": 0.04, "ir": 0.3, "gross_ir": 0.31,
               "dsr": 0.9, "pbo": None, "turnover": 0.1, "n_samples": 300,
               "regime_ic": {}, "yearly_ic": {}, "nearest_factor": None,
               "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(True, 1, code="c", series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(passed, metrics, reason))
    events = []
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: events.append(kw))

    process_hypothesis(Hypothesis("h1", "x", SOURCE_LLM), panel, panel,
                       pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                       "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                       llm=object(), run_sandbox=object())

    trials = [e for e in events if e["kind"] == "factor_trial"]
    assert len(trials) == 1, "an evaluated factor must be counted whatever the verdict"
    assert trials[0]["detail"]["gross_sr_per_bar"] == 0.31
    assert trials[0]["detail"]["sr_per_bar"] == 0.3          # kept for existing readers


def test_a_forge_failure_is_not_a_trial(tmp_path, monkeypatch):
    """forge_failed code never ran clean, so it was never statistically tested --
    it is not a multiple-testing debt."""
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.forge import ForgeResult

    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    monkeypatch.setattr(orch, "forge",
                        lambda *a, **k: ForgeResult(False, 3, code="bad", death_reason="UnsafeCodeError: x"))
    events = []
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: events.append(kw))
    process_hypothesis(Hypothesis("h2", "x", SOURCE_LLM), panel, panel,
                       pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                       "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                       llm=object(), run_sandbox=object())
    assert [e for e in events if e["kind"] == "factor_trial"] == []


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
    feats = pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx)   # no close: real schema
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis(f"h{i}", f"x{i}", SOURCE_ZOO) for i in range(20)])
    # every hypothesis fails -> early stop should fire before all 20 processed
    seen = []
    def fake_process(hyp, *a, **k): seen.append(hyp.id); return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)
    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=20, early_stop_after=3),
                          zoo_dir=tmp_path, run_sandbox=object(), oos_start=_TEST_OOS,
                          ohlcv=_ohlcv_for(idx), sources=())
    assert len(seen) == 3                             # stopped after 3 consecutive fails
    assert summary["forge_failed"] == 3 and summary["candidate"] == 0


def test_run_foundry_uses_a_passed_shared_forge_budget(tmp_path, monkeypatch):
    """A shared ForgeBudget passed in is used verbatim; run_foundry does not build
    a fresh one. Pre-exhaust it and confirm the cap bites (not budget's 99)."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.forge import ForgeBudget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    feats = pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h0", "x0", SOURCE_ZOO)])

    shared = ForgeBudget(max_llm_calls=2)
    shared.used = 2                                  # already exhausted
    seen = {}
    def fake_process(hyp, *a, forge_budget=None, **k):
        seen["is_shared"] = forge_budget is shared
        forge_budget.charge_call()                   # raises BudgetExhausted (used>=max)
        return "candidate"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=5, max_llm_calls=99),
                          zoo_dir=tmp_path, run_sandbox=object(), oos_start=_TEST_OOS,
                          ohlcv=_ohlcv_for(idx), forge_budget=shared, sources=())
    assert seen["is_shared"] is True
    assert summary.get("budget_exhausted") is True   # shared cap bit, not budget's 99


def test_run_foundry_summary_reports_queue_composition_and_features_range(tmp_path, monkeypatch):
    """N4: the foundry e2e asserts these keys so a caller (dashboard / cron log)
    can see what the sweep actually tried and over what window, without having
    to re-derive it from the queue/panel after the fact."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO, SOURCE_DERIVED

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    feats = pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [
        Hypothesis("h0", "x0", SOURCE_ZOO), Hypothesis("h1", "x1", SOURCE_ZOO),
        Hypothesis("h2", "x2", SOURCE_DERIVED),
    ])
    monkeypatch.setattr(orch, "process_hypothesis", lambda hyp, *a, **k: "candidate")
    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=20, early_stop_after=99),
                          zoo_dir=tmp_path, run_sandbox=object(), oos_start=_TEST_OOS,
                          ohlcv=_ohlcv_for(idx), sources=())
    assert summary["queue_composition"] == {SOURCE_ZOO: 2, SOURCE_DERIVED: 1}
    assert summary["features_range"] == [str(idx.min()), str(idx.max())]


def test_run_foundry_raises_when_injected_ohlcv_has_no_close(tmp_path, monkeypatch):
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    monkeypatch.setattr(orch, "load_features",
                        lambda s, manifests_dir=None: pd.DataFrame({"funding_z": np.arange(50.0)}, index=idx))
    priceless = pd.DataFrame({"volume": np.ones(50)}, index=idx)     # no close column
    with pytest.raises(ValueError, match="close"):
        run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                    llm=object(), sandbox=object(), budget=Budget(), zoo_dir=tmp_path,
                    oos_start=_TEST_OOS, ohlcv=priceless)


def test_run_foundry_wires_graveyard_values_into_existing_and_dead(tmp_path, monkeypatch):
    """agy 3c: existing.join(pd.read_parquet(gpath), ...) must actually surface
    buried-factor VALUES to process_hypothesis, not merely avoid crashing."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget, _graveyard_path, _merge_column
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=60, freq="1D")
    # features-only: the real features_<sym>.parquet carries no OHLCV
    feats = pd.DataFrame({"funding_z": np.arange(60.0), "some_factor": np.arange(60.0) * 2}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
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
               oos_start=_TEST_OOS, ohlcv=_ohlcv_for(idx), sources=())

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
    panel = pd.DataFrame({"funding_z": np.arange(72.0)}, index=idx)   # features-only: real parquet has no close
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
               oos_start=_TEST_OOS, ohlcv=_ohlcv_for(idx), sources=())

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
    panel = pd.DataFrame({"funding_z": np.arange(72.0)}, index=idx)   # features-only: real parquet has no close
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
               oos_start=_TEST_OOS, ohlcv=_ohlcv_for(idx), sources=())

    dr = captured["daily_regime"]
    assert len(dr) == 3
    assert list(dr.values) == ["bull", "bull", "neutral"]     # real labels, not all-neutral
    assert (dr.index == dr.index.normalize()).all()           # daily-normalized index


def test_load_daily_regime_returns_tz_aware_utc_index(tmp_path):
    # The foundry panel/ohlcv are tz-aware UTC; a naive regime index makes
    # regime_ic's alignment raise "Cannot compare dtypes datetime64 and
    # datetime64[..., UTC]". The loader must return UTC-aware labels.
    import json
    from research.hermes.orchestrator import _load_daily_regime

    manifest = {"breakdown": [
        {"date": "2022-01-01", "regime": "bull"},
        {"date": "2022-01-02", "regime": "neutral"},
    ]}
    (tmp_path / "regime_eth.json").write_text(json.dumps(manifest), encoding="utf-8")

    dr = _load_daily_regime("eth", tmp_path)
    assert dr is not None
    assert dr.index.tz is not None                            # tz-aware, not naive
    assert str(dr.index.tz) == "UTC"


def test_run_foundry_falls_back_to_neutral_when_regime_manifest_malformed(tmp_path, monkeypatch):
    """A present-but-broken regime_<sym>.json must degrade to the neutral
    fallback, not crash the foundry run (regime_ic is informational only)."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=72, freq="1h")
    panel = pd.DataFrame({"funding_z": np.arange(72.0)}, index=idx)   # features-only: real parquet has no close
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
               oos_start=_TEST_OOS, ohlcv=_ohlcv_for(idx), sources=())

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
                              sandbox=object(), zoo_dir=tmp_path, ohlcv=None)
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
                        sandbox=object(), zoo_dir=tmp_path, ohlcv=None)

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
    feats = pd.DataFrame({"funding_z": np.arange(float(len(idx)))}, index=idx)
    assert (idx >= pd.Timestamp(oos_start, tz="UTC")).any(), "fixture must span the OOS boundary"

    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
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
                zoo_dir=tmp_path, run_sandbox=object(), oos_start=oos_start,
                ohlcv=_ohlcv_for(idx), sources=())

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


# ── OHLCV must be injected, never sliced out of the feature panel ──────────
#
# agy: features_<sym>.parquet deliberately holds only features -- OHLCV lives in
# the candle source (stage0a fetches it via lib.okx_data.fetch_candles). The old
# `ohlcv = panel[[c for c in _OHLCV_COLS if c in panel.columns]]` sliced columns
# off a table that never had them, so run_foundry raised on every real manifest.
# Every unit test hid this by hand-building a DataFrame that happened to carry
# "close" -- "Fixture Schema Divergence". These tests build the fixture through
# the REAL dump_features/load_features round-trip so the schema cannot diverge.

def _write_real_features(tmp_path, n=400):
    """Schema-enforced fixture: goes through production dump_features()."""
    import numpy as np, pandas as pd
    from research.lib.factor_io import dump_features
    idx = pd.date_range("2022-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    dump_features("eth", {"rsi_14": pd.Series(rng.normal(size=n), index=idx),
                          "funding_z": pd.Series(rng.normal(size=n), index=idx)},
                  manifests_dir=tmp_path)
    return idx


def _ohlcv_for(idx):
    import numpy as np, pandas as pd
    close = 100 + np.cumsum(np.random.default_rng(1).normal(size=len(idx)))
    return pd.DataFrame({"open": close, "high": close, "low": close,
                         "close": close, "volume": np.ones(len(idx))}, index=idx)


def test_real_features_parquet_carries_no_ohlcv(tmp_path):
    # documents WHY ohlcv must be injected: the production schema has no close
    from research.lib.factor_io import load_features
    idx = _write_real_features(tmp_path)
    feats = load_features("eth", manifests_dir=tmp_path)
    assert "close" not in feats.columns


def test_run_foundry_smoke_on_real_feature_schema(tmp_path, monkeypatch):
    """E2E artifact smoke test (agy #4): real features parquet + injected ohlcv.
    Any regression in the data seam blows up here instead of at 3am in prod."""
    import pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = _write_real_features(tmp_path)
    ohlcv = _ohlcv_for(idx)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h1", "x", SOURCE_ZOO)])

    seen = {}
    def spy(hyp, panel_arg, ohlcv_arg, *a, **k):
        seen["panel_cols"] = set(panel_arg.columns)
        seen["ohlcv_cols"] = set(ohlcv_arg.columns)
        return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", spy)

    run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
                llm=object(), sandbox=object(), budget=Budget(), zoo_dir=tmp_path,
                oos_start=_TEST_OOS, ohlcv=ohlcv, run_sandbox=object(), sources=())

    # forge's panel carries BOTH: 258/301 zoo alphas require close, 171 require volume
    assert {"rsi_14", "funding_z", "close", "volume"} <= seen["panel_cols"]
    # the gate gets its own ohlcv object, not a slice of the feature table
    assert "close" in seen["ohlcv_cols"]


def test_run_foundry_rejects_ohlcv_that_does_not_cover_features(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    idx = _write_real_features(tmp_path)
    ohlcv = _ohlcv_for(idx).iloc[:50]           # agy #3: short/misaligned price table
    with pytest.raises(ValueError, match="coverage|align"):
        run_foundry("eth", tmp_path, GateConfig(interval="1H", horizon_h=24),
                    llm=object(), sandbox=object(), budget=Budget(), zoo_dir=tmp_path,
                    oos_start=_TEST_OOS, ohlcv=ohlcv, run_sandbox=object())


# ── the budget breaker must actually be CALLED from the production path ─────
#
# Five bugs this session were all "guard exists, guard is tested, guard is never
# invoked". A circuit breaker that run_foundry forgets to pass to forge() is the
# same bug wearing a new hat.

def test_run_foundry_passes_a_budget_into_forge(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.forge import ForgeBudget
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    feats = pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h1", "x", SOURCE_ZOO)])

    seen = {}
    def fake_forge(hyp, llm, run_sandbox, panel, max_retries=3, budget=None):
        seen["budget"] = budget
        from research.hermes.forge import ForgeResult
        return ForgeResult(False, 1, code="c", death_reason="x")
    monkeypatch.setattr(orch, "forge", fake_forge)
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)

    run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                llm=object(), sandbox=object(), budget=Budget(max_llm_calls=7),
                zoo_dir=tmp_path, oos_start=_TEST_OOS, ohlcv=_ohlcv_for(idx),
                run_sandbox=object(), sources=())
    assert isinstance(seen["budget"], ForgeBudget)
    assert seen["budget"].max_llm_calls == 7      # run-wide cap reached forge()


def test_run_foundry_stops_the_sweep_when_budget_trips(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    feats = pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
    monkeypatch.setattr(orch, "build_queue",
                        lambda **k: [Hypothesis(f"h{i}", f"x{i}", SOURCE_ZOO) for i in range(10)])

    tried = []
    def spy(hyp, *a, **k):
        tried.append(hyp.id)
        return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", spy)
    # trip on the 3rd hypothesis
    real_charge = None
    def budget_tripping_process(hyp, *a, **k):
        tried.append(hyp.id)
        if len(tried) > 2:
            from research.hermes.forge import BudgetExhausted
            raise BudgetExhausted("LLM call budget exhausted after 2 calls")
        return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", budget_tripping_process)

    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(), budget=Budget(max_factors=10),
                          zoo_dir=tmp_path, oos_start=_TEST_OOS, ohlcv=_ohlcv_for(idx),
                          run_sandbox=object(), sources=())
    assert len(tried) == 3                       # stopped, did not grind through all 10
    assert summary.get("budget_exhausted") is True


def test_merge_into_candidates_outer_joins_across_calls(tmp_path):
    import pandas as pd, numpy as np
    from research.hermes.orchestrator import _merge_into_candidates
    from research.hermes.candidate_store import _candidate_path
    idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    _merge_into_candidates("eth", tmp_path, "f_a", pd.Series(np.arange(5.0), index=idx))
    _merge_into_candidates("eth", tmp_path, "f_b", pd.Series(np.arange(5.0) * 2, index=idx))
    got = pd.read_parquet(_candidate_path("eth", tmp_path))
    assert list(got.columns) == ["f_a", "f_b"]              # both kept, not clobbered
    assert got["f_b"].tolist() == [0.0, 2, 4, 6, 8]

def test_merge_column_replaces_same_name_and_unions_index():
    import pandas as pd, numpy as np
    from research.hermes.orchestrator import _merge_column
    idx1 = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    idx2 = pd.date_range("2024-01-01 02:00", periods=3, freq="1h", tz="UTC")
    base = _merge_column(None, "f_a", pd.Series([1.0, 2, 3], index=idx1))
    out = _merge_column(base, "f_a", pd.Series([9.0, 9, 9], index=idx2))   # same name
    assert list(out.columns) == ["f_a"]                     # replaced, not duplicated
    assert len(out) == 5                                    # union of indexes (1 overlap)

def test_merge_into_graveyard_persists_dead_values(tmp_path):
    import pandas as pd, numpy as np
    from research.hermes.orchestrator import _merge_into_graveyard, _graveyard_path
    idx = pd.date_range("2024-01-01", periods=4, freq="1h", tz="UTC")
    _merge_into_graveyard("eth", tmp_path, "dead_1", pd.Series(np.arange(4.0), index=idx))
    got = pd.read_parquet(_graveyard_path("eth", tmp_path))
    assert "dead_1" in got.columns and len(got) == 4

def test_process_hypothesis_persists_code_for_candidate_only(tmp_path, monkeypatch):
    # A candidate's forged source must be retrievable afterwards (the bridge
    # re-runs it over the full span); a graveyard factor's need not be.
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.candidate_store import load_candidate_code
    from research.hermes.forge import ForgeResult
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=48, freq="1h", tz="UTC")
    panel = pd.DataFrame({"close": np.arange(48.0)}, index=idx)
    series = pd.Series(np.arange(48.0), index=idx)
    code = "def compute(panel):\n    return panel['close']\n"

    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(
        success=True, attempts=1, code=code, series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(
        True, {"gross_ic": 0.1, "ic_nonoverlap": 0.1, "ir": 0.5, "dsr": 1.0,
               "pbo": None, "turnover": 0.1, "n_samples": 48,
               "regime_ic": {}, "yearly_ic": {}, "nearest_factor": None,
               "nearest_abs_spearman": 0.0}, ""))
    monkeypatch.setattr(orch, "upsert_card", lambda *a, **k: None)
    monkeypatch.setattr(orch, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_merge_into_candidates", lambda *a, **k: None)

    hyp = Hypothesis("zoo_mom", "momentum", SOURCE_ZOO)
    cfg = GateConfig(interval="1H", horizon_h=24)
    out = orch.process_hypothesis(hyp, panel, panel, None, panel, "eth",
                                  tmp_path, cfg, object(), lambda c, p: series)

    assert out == orch.OUTCOME_CANDIDATE
    got_code, meta = load_candidate_code("zoo_mom", "eth", tmp_path)
    assert got_code == code
    assert meta["code_sha256"] == __import__("hashlib").sha256(code.encode()).hexdigest()


def test_process_hypothesis_does_not_persist_code_for_rejected(tmp_path, monkeypatch):
    # Negative path: a rejected/graveyard factor must NOT have its code persisted.
    # Only candidates get code storage; rejects are buried and their source is discarded.
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.candidate_store import load_candidate_code
    from research.hermes.forge import ForgeResult
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=48, freq="1h", tz="UTC")
    panel = pd.DataFrame({"close": np.arange(48.0)}, index=idx)
    series = pd.Series(np.arange(48.0), index=idx)
    code = "def compute(panel):\n    return panel['close']\n"

    # Forge succeeds, but gatekeeper rejects
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(
        success=True, attempts=1, code=code, series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(
        False, {"gross_ic": 0.001, "ic_nonoverlap": 0.0, "ir": 0.0, "dsr": 0.1,
                "pbo": None, "turnover": 0.1, "n_samples": 48,
                "regime_ic": {}, "yearly_ic": {}, "nearest_factor": None,
                "nearest_abs_spearman": 0.0}, "weak ic"))
    monkeypatch.setattr(orch, "upsert_card", lambda *a, **k: None)
    monkeypatch.setattr(orch, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_merge_into_graveyard", lambda *a, **k: None)

    hyp = Hypothesis("dead_zoo", "weak_factor", SOURCE_ZOO)
    cfg = GateConfig(interval="1H", horizon_h=24)
    out = orch.process_hypothesis(hyp, panel, panel, None, panel, "eth",
                                  tmp_path, cfg, object(), lambda c, p: series)

    assert out == orch.OUTCOME_REJECTED
    # Code must NOT be persisted for rejected factors
    with pytest.raises(FileNotFoundError):
        load_candidate_code("dead_zoo", "eth", tmp_path)


# ── LLM ideation wiring: llm_raw was hardcoded to [] until this task ───────
#
# Everything downstream of the queue (build_queue, process_hypothesis, forge,
# evaluate) is already covered above -- the one missing wire was run_foundry
# actually calling generate_ideas() and feeding its output into build_queue's
# llm_raw. These tests fake generate_ideas only and let the rest of the real
# path (build_queue -> process_hypothesis -> forge -> sandbox -> gatekeeper)
# run for real, so a regression anywhere in the wire shows up here rather than
# only in a mock assertion.

from research.hermes.orchestrator import Budget, run_foundry


class _FakeIdeationLLM:
    """.complete() is only ever reached via forge()'s codegen call in these
    tests (generate_ideas itself is monkeypatched per-test), so this can
    ignore the prompt and return one fixed, safe compute() that reads a real
    panel column -- enough for forge() to succeed on the first attempt."""
    def complete(self, prompt: str) -> str:
        return "```python\ndef compute(df):\n    return df['funding_z'].fillna(0.0)\n```"


def _fake_run_sandbox(code, panel):
    """In-process stand-in for make_run_sandbox(sandbox, ...): exec the code
    and call compute() directly -- no Docker/parquet round-trip needed here."""
    ns: dict = {}
    exec(code, {"pd": pd, "np": np}, ns)
    return ns["compute"](panel)


@pytest.fixture
def foundry_env(tmp_path):
    """kwargs for run_foundry(**foundry_env): a real features parquet carrying
    two field_schema.yaml columns (funding_z, oi_z), a matching injected ohlcv,
    and a fake llm/run_sandbox that let forge() succeed on the first attempt --
    so a test only has to fake generate_ideas and still exercises the real
    build_queue/process_hypothesis/forge/evaluate path end to end."""
    from research.lib.factor_io import dump_features
    from research.hermes.gatekeeper import GateConfig

    idx = pd.date_range("2022-01-01", periods=400, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    dump_features("eth", {
        "funding_z": pd.Series(rng.normal(size=len(idx)), index=idx),
        "oi_z": pd.Series(rng.normal(size=len(idx)), index=idx),
    }, manifests_dir=tmp_path)
    close = 100 + np.cumsum(rng.normal(size=len(idx)))
    ohlcv = pd.DataFrame({"open": close, "high": close, "low": close,
                         "close": close, "volume": np.ones(len(idx))}, index=idx)
    return dict(
        symbol="eth", manifests_dir=tmp_path,
        cfg=GateConfig(interval="1H", horizon_h=24),
        llm=_FakeIdeationLLM(), sandbox=object(), budget=Budget(),
        oos_start=_TEST_OOS, ohlcv=ohlcv, run_sandbox=_fake_run_sandbox,
    )


def test_budget_defaults_match_the_single_source_reality():
    b = Budget()
    assert b.max_factors == 20
    # only one source is left, no next source to skip to -> early stop would
    # only truncate the quality-measuring sample (spec Q5)
    assert b.early_stop_after >= b.max_factors
    assert b.max_llm_calls == 60          # hard cap does not move


def test_run_foundry_wires_llm_ideas_into_the_queue(foundry_env, monkeypatch):
    """The single most important test in this file: llm_raw was hardcoded to []."""
    import research.hermes.orchestrator as orch

    seen = {}

    def fake_generate(llm, schema, deaths, n_ideas, budget=None, **kw):
        seen["schema_cols"] = sorted(schema)
        seen["n_ideas"] = n_ideas
        return [{"id": "fz_oi", "description": "funding_z conditional on oi_z",
                 "fields": ["funding_z", "oi_z"]}], [], None

    monkeypatch.setattr(orch, "generate_ideas", fake_generate)
    summary = run_foundry(**foundry_env)

    assert summary["queue_composition"] == {"llm": 1}
    assert summary["ideas_accepted"] == 1
    assert "zoo" not in summary["queue_composition"]
    assert seen["n_ideas"] == 25          # ask for 25, take the first 20 (spec §5.3)


def test_run_foundry_records_ideation_failure_without_crashing(foundry_env, monkeypatch):
    """A nightly cron must not die just because the LLM returned garbage."""
    import research.hermes.orchestrator as orch
    monkeypatch.setattr(orch, "generate_ideas",
                        lambda *a, **k: ([], [], "IdeationParseError: no JSON"))
    summary = run_foundry(**foundry_env)

    assert summary["ideation_failed"] == "IdeationParseError: no JSON"
    assert summary["candidate"] == 0
    assert summary["queue_composition"] == {}


def test_run_foundry_surfaces_rejected_ideas_in_the_summary(foundry_env, monkeypatch):
    """The hallucinated-field rejection rate must be visible -- it's the signal
    for whether field_schema is doing its job."""
    import research.hermes.orchestrator as orch
    monkeypatch.setattr(orch, "generate_ideas", lambda *a, **k: (
        [{"id": "ok", "description": "d", "fields": ["funding_z", "oi_z"]}],
        [{"id": "bad", "reason": "unknown panel columns: ['liquidation_z']"}], None))
    summary = run_foundry(**foundry_env)
    assert summary["ideas_accepted"] == 1
    assert summary["ideas_rejected"] == [
        {"id": "bad", "reason": "unknown panel columns: ['liquidation_z']"}]


def test_run_foundry_only_offers_documented_panel_columns(foundry_env, monkeypatch):
    """When schema and panel disagree, the runtime takes the intersection +
    warns instead of crashing (spec §6)."""
    import research.hermes.orchestrator as orch
    monkeypatch.setattr(orch, "load_field_schema", lambda: {
        "funding_z": {"what": "w", "positive": "p", "notes": "n"},
        "long_gone_column": {"what": "w", "positive": "p", "notes": "n"}})
    seen = {}

    def fake_generate(llm, schema, deaths, n_ideas, budget=None, **kw):
        seen["cols"] = sorted(schema)
        return [{"id": "a", "description": "d", "fields": ["funding_z"]}], [], None

    monkeypatch.setattr(orch, "generate_ideas", fake_generate)
    run_foundry(**foundry_env)
    assert seen["cols"] == ["funding_z"]          # the stale column was never offered to the LLM


def test_run_foundry_degrades_gracefully_when_ideation_budget_is_already_exhausted(foundry_env):
    """Final whole-branch review finding: generate_ideas() raises BudgetExhausted
    when the shared forge_budget is already spent, and that call sits BEFORE
    build_queue with no exception handling -- it used to propagate out of
    run_foundry, which makes run_foundry_job mark the job failed and re-raise,
    which in turn aborts the entire reconcile_foundry_jobs batch (that function
    only catches LLMUnavailable/SandboxError, not BudgetExhausted). Running out
    of budget during ideation must degrade the same way the sweep loop's own
    BudgetExhausted handling already does -- not crash depending on WHEN in the
    run it happens."""
    from research.hermes.forge import ForgeBudget

    exhausted = ForgeBudget(max_llm_calls=0)          # already exhausted before any call
    summary = run_foundry(**foundry_env, forge_budget=exhausted)

    assert summary.get("budget_exhausted") is True
    assert summary["ideas_accepted"] == 0
    assert summary["candidate"] == 0                  # no ideas -> nothing to sweep, not a crash


# ── job params -> sources passthrough ───────────────────────────────────────
#
# run_foundry_job hardcoded no `sources` kwarg to run_foundry, so every queued
# job silently fell back to run_foundry's own default (llm-only). Re-running
# zoo (or any other combination) as a control required editing code instead of
# enqueueing a job with params["sources"] set -- this wires that override
# through, the same dependency-injection path oos_start/ohlcv already use.

@pytest.fixture
def job_env(tmp_path):
    """kwargs for run_foundry_job(**job_env): a queued job.json (oos_start set,
    per the OOS-lock contract) plus the object()/None stand-ins the existing
    reconcile tests (test_enqueue_writes_job_and_runner_reconciles et al.)
    already use for llm/sandbox/ohlcv."""
    from research.hermes.orchestrator import enqueue_foundry_job
    job_path = enqueue_foundry_job("eth", runs_dir=tmp_path,
                                   params={"interval": "1D", "horizon_h": 24,
                                           "oos_start": _TEST_OOS})
    return dict(job_path=job_path, manifests_dir=tmp_path, llm=object(),
               sandbox=object(), zoo_dir=tmp_path, ohlcv=None)


def test_run_foundry_job_defaults_to_llm_only(tmp_path, monkeypatch, job_env):
    import research.hermes.orchestrator as orch
    seen = {}
    monkeypatch.setattr(orch, "run_foundry",
                        lambda *a, **kw: seen.update(kw) or {"candidate": 0})
    orch.run_foundry_job(**job_env)
    assert seen["sources"] == frozenset({"llm"})


def test_run_foundry_job_honours_an_explicit_sources_list(tmp_path, monkeypatch, job_env):
    """重跑 zoo 當對照組：enqueue params.sources=["llm","zoo"]，不必改碼。"""
    import json
    from pathlib import Path
    import research.hermes.orchestrator as orch
    job = json.loads(Path(job_env["job_path"]).read_text(encoding="utf-8"))
    job["params"]["sources"] = ["llm", "zoo"]
    Path(job_env["job_path"]).write_text(json.dumps(job), encoding="utf-8")

    seen = {}
    monkeypatch.setattr(orch, "run_foundry",
                        lambda *a, **kw: seen.update(kw) or {"candidate": 0})
    orch.run_foundry_job(**job_env)
    assert seen["sources"] == frozenset({"llm", "zoo"})


def test_job_failure_records_the_traceback_not_just_the_message(tmp_path, monkeypatch):
    """A real run buried two jobs under the bare string 'float division by zero'.
    str(e) alone is unfalsifiable: it names no file, line or frame, and the bug
    lives on a paid code path that a fake-LLM repro cannot reach. Without the
    traceback there is no way to ever find it."""
    import json
    from pathlib import Path
    import research.hermes.orchestrator as orch

    job_path = orch.enqueue_foundry_job("eth", tmp_path, {"oos_start": "2025-01-01"})

    def boom(*a, **kw):
        one, zero = 1.0, 0.0
        return one / zero                      # the real failure's shape

    monkeypatch.setattr(orch, "run_foundry", boom)
    with pytest.raises(ZeroDivisionError):
        orch.run_foundry_job(job_path, manifests_dir=tmp_path, llm=None, sandbox=None,
                             zoo_dir=None, ohlcv=None)

    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["error"] == "float division by zero"
    assert "traceback" in job, "job.json must carry the traceback, not just str(e)"
    assert "ZeroDivisionError" in job["traceback"]
    assert "boom" in job["traceback"]           # names the frame that raised
    assert "orchestrator.py" in job["traceback"]


def _iso_env(monkeypatch, exc):
    """run_foundry wired so hypothesis #2 of 3 raises `exc` inside process_hypothesis."""
    import research.hermes.orchestrator as orch
    calls = []

    def fake_process(hyp, *a, **kw):
        calls.append(hyp.id)
        if hyp.id == "llm_b":
            raise exc
        return orch.OUTCOME_REJECTED

    monkeypatch.setattr(orch, "process_hypothesis", fake_process)
    # distinct descriptions: dedupe() collapses on the description fingerprint, so
    # three ideas all saying "d" would arrive at the sweep as a single hypothesis.
    monkeypatch.setattr(orch, "generate_ideas", lambda *a, **k: (
        [{"id": i, "description": f"hypothesis {i}", "fields": ["funding_z", "oi_z"]}
         for i in "abc"],
        [], None))
    return calls


def test_one_hypothesis_exception_does_not_kill_the_whole_run(foundry_env, monkeypatch):
    """A single bad factor must not take the night's other 19 down with it.
    The real run lost 20 hypotheses to one uncaught ZeroDivisionError."""
    calls = _iso_env(monkeypatch, ZeroDivisionError("float division by zero"))
    summary = run_foundry(**foundry_env)

    assert calls == ["llm_a", "llm_b", "llm_c"], "the sweep stopped at the bad hypothesis"
    assert summary["errored"] == 1
    assert summary["rejected"] == 2


def test_an_isolated_exception_keeps_its_traceback_visible(foundry_env, monkeypatch):
    """Isolation must not become concealment: catching the error is only
    legitimate if the evidence survives in full."""
    _iso_env(monkeypatch, ZeroDivisionError("float division by zero"))
    summary = run_foundry(**foundry_env)

    errors = summary["errors"]
    assert [e["factor_id"] for e in errors] == ["llm_b"]
    assert "ZeroDivisionError" in errors[0]["traceback"]
    assert "float division by zero" in errors[0]["traceback"]


def test_isolated_exception_writes_a_graveyard_card_with_the_traceback(foundry_env, monkeypatch):
    from research.hermes.evidence_store import load_cards
    from research.hermes.evidence_card import VERDICT_GRAVEYARD

    _iso_env(monkeypatch, ZeroDivisionError("float division by zero"))
    run_foundry(**foundry_env)

    card = [c for c in load_cards(foundry_env["symbol"], foundry_env["manifests_dir"])
            if c.factor_id == "llm_b"][0]
    assert card.verdict == VERDICT_GRAVEYARD
    assert "ZeroDivisionError" in card.death_reason
    assert "Traceback" in card.death_reason


def test_infra_sandbox_error_still_aborts_the_run(foundry_env, monkeypatch):
    """SandboxError (docker daemon down) is NOT a factor verdict. reconcile_foundry_jobs
    relies on it propagating to abort the whole batch -- every later job hits the
    same wall. Isolation must not swallow it."""
    from research.hermes.sandbox import SandboxError
    _iso_env(monkeypatch, SandboxError("docker daemon is down"))
    with pytest.raises(SandboxError):
        run_foundry(**foundry_env)
