"""Tests for dashboard/server/pipeline_status.py (pure status builder)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schemas import PipelineStatus, StageStatus
import pipeline_status as ps_mod


def test_stage_status_model_round_trips():
    s = StageStatus(
        stage_id="0a", label="Features/Evidence", state="done",
        generated_at="2026-06-04T13:45:41+00:00",
        metric_label="top|IC|", metric_value="0.088",
    )
    assert s.state == "done"
    # invalid state rejected
    with pytest.raises(Exception):
        StageStatus(stage_id="0a", label="x", state="bogus",
                    generated_at=None, metric_label=None, metric_value=None)


def test_pipeline_status_empty():
    ps = PipelineStatus(generated_at="2026-06-08T00:00:00+00:00", symbols=[])
    assert ps.symbols == []


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_artifact_time_prefers_generated_at(tmp_path):
    p = tmp_path / "a.json"
    _write(p, {"generated_at": "2026-01-02T03:04:05+00:00"})
    raw = json.loads(p.read_text())
    assert ps_mod._artifact_time(p, raw) == "2026-01-02T03:04:05+00:00"


def test_artifact_time_falls_back_to_mtime(tmp_path):
    p = tmp_path / "b.json"
    _write(p, {"no_time": 1})
    raw = json.loads(p.read_text())
    out = ps_mod._artifact_time(p, raw)
    assert out is not None and "T" in out  # an ISO timestamp from mtime


def test_discover_symbols_union_config_and_filesystem(tmp_path):
    md = tmp_path / "research" / "manifests"
    md.mkdir(parents=True)
    (md / "factor_btc.json").write_text("{}", encoding="utf-8")
    (md / "evidence_eth.json").write_text("{}", encoding="utf-8")
    (md / "candidates_sol.json").write_text("{}", encoding="utf-8")
    (md / "factor_values_btc.meta.json").write_text("{}", encoding="utf-8")  # must be ignored
    syms = ps_mod.discover_symbols(md, config_symbols=["btc", "eth"])
    assert syms[:2] == ["btc", "eth"]       # config first, in config order
    assert "sol" in syms                      # discovered extra
    assert "values_btc" not in syms           # meta file not mistaken for a symbol


def test_apply_staleness_marks_downstream_stale():
    # (stage_id, label, generated_at, metric_label, metric_value, present)
    raws = [
        ps_mod._Raw("0a", "A", "2026-01-03T00:00:00+00:00", None, None, True),
        ps_mod._Raw("0",  "B", "2026-01-02T00:00:00+00:00", None, None, True),  # older than upstream -> stale
        ps_mod._Raw("1",  "C", None, None, None, False),                         # missing
        ps_mod._Raw("2",  "D", "2026-01-04T00:00:00+00:00", None, None, True),   # newer -> done
    ]
    out = ps_mod._apply_staleness(raws, seed_time=None)
    assert [s.state for s in out] == ["done", "stale", "missing", "done"]


def test_apply_staleness_seed_time_from_upstream_chain():
    # A strategy stage older than its symbol's stage-2 seed is stale.
    seed = "2026-02-01T00:00:00+00:00"
    raws = [ps_mod._Raw("3", "Diag", "2026-01-15T00:00:00+00:00", None, None, True)]
    out = ps_mod._apply_staleness(raws, seed_time=seed)
    assert out[0].state == "stale"


def _seed_symbol_manifests(md: Path):
    md.mkdir(parents=True, exist_ok=True)
    _write(md / "features_btc.meta.json",
           {"generated_at": "2026-06-01T00:00:00+00:00",
            "feature_names": ["rsi_14", "funding_z"]})
    _write(md / "evidence_btc.json",
           {"evidence": [{"ic_by_horizon": {"8": 0.088, "72": -0.02}}]})
    _write(md / "candidates_btc.json",
           {"generated_at": "2026-06-02T00:00:00+00:00",
            "candidates": [{"feature_key": "funding_z"}, {"feature_key": "stablecoin_supply_z"}]})
    _write(md / "factor_btc.json",
           {"generated_at": "2026-06-03T00:00:00+00:00",
            "factors": [{"verdict": "single_use"}, {"verdict": "ensemble_only"},
                        {"verdict": "reject"}]})
    _write(md / "regime_btc.json",
           {"generated_at": "2026-06-08T00:00:00+00:00",
            "breakdown": [{"regime": "bull"}, {"regime": "bull"}, {"regime": "bear"}]})


def test_symbol_stages_metrics_and_states(tmp_path):
    md = tmp_path / "research" / "manifests"
    _seed_symbol_manifests(md)
    # stage 2 generation.json for one btc strategy
    _write(md / "btc_s1_single_factor" / "generation.json", {"method": "deterministic", "generated_at": "2026-06-07T00:00:00+00:00"})

    stages = ps_mod.build_symbol_stages(md, "btc")
    by_id = {s.stage_id: s for s in stages}
    assert by_id["0a"].metric_label == "top|IC|" and by_id["0a"].metric_value == "0.088"
    assert by_id["0"].metric_value == "2"                # 2 candidates
    assert by_id["1"].metric_value == "S1 E1 R1"         # verdict counts
    assert by_id["2"].metric_value == "1"                # 1 strategy emitted
    assert by_id["2.5"].metric_value == "bull"           # dominant regime
    assert all(s.state == "done" for s in stages)        # timestamps ascending


def test_symbol_stages_missing(tmp_path):
    md = tmp_path / "research" / "manifests"
    md.mkdir(parents=True)
    stages = ps_mod.build_symbol_stages(md, "btc")
    assert all(s.state == "missing" for s in stages)


def test_strategy_stages_and_full_build(tmp_path):
    md = tmp_path / "research" / "manifests"
    _seed_symbol_manifests(md)
    sid = "btc_s1_single_factor"
    # Give strategy artifacts timestamps newer than symbol stage-2 (which will have mtime ~now)
    _write(md / sid / "generation.json",
           {"method": "deterministic", "generated_at": "2026-06-09T10:00:00+00:00"})
    _write(md / sid / "diagnosis.json",
           {"generated_at": "2026-06-09T11:00:00+00:00",
            "recommended_action": "proceed"})
    _write(md / sid / "optimization.json",
           {"generated_at": "2026-06-09T12:00:00+00:00",
            "best_params": {}, "best_metrics": {"sharpe": 1.23}})
    _write(md / "selection.json",
           {"generated_at": "2026-06-09T13:00:00+00:00",
            "ranking": [{"strategy_id": sid, "score": 0.72, "selected": True}]})

    full = ps_mod.build_pipeline_status(tmp_path, config_symbols=["btc"])
    assert full.symbols[0].symbol == "btc"
    strat = full.symbols[0].strategies[0]
    assert strat.strategy_id == sid
    by_id = {s.stage_id: s for s in strat.stages}
    assert by_id["3"].metric_value == "proceed"
    assert by_id["4"].metric_value == "1.23"          # best sharpe
    assert by_id["5"].metric_value == "✓ 0.72"        # selected + score
    assert all(s.state == "done" for s in strat.stages)


def test_strategy_stage_missing_when_no_artifacts(tmp_path):
    md = tmp_path / "research" / "manifests"
    _seed_symbol_manifests(md)
    sid = "btc_s1_single_factor"
    _write(md / sid / "generation.json", {"method": "deterministic"})
    full = ps_mod.build_pipeline_status(tmp_path, config_symbols=["btc"])
    strat = full.symbols[0].strategies[0]
    assert [s.state for s in strat.stages] == ["missing", "missing", "missing"]


def test_strategy_stage_stale_when_older_than_seed(tmp_path):
    """Strategy stage older than symbol's stage-2 must be stale."""
    md = tmp_path / "research" / "manifests"
    _seed_symbol_manifests(md)
    sid = "btc_s1_single_factor"
    _write(md / sid / "generation.json", {"generated_at": "2026-06-03T12:00:00+00:00"})
    # Diagnosis older than generation (stage-2 seed) -> stale
    _write(md / sid / "diagnosis.json",
           {"generated_at": "2026-06-02T00:00:00+00:00",
            "recommended_action": "proceed"})

    full = ps_mod.build_pipeline_status(tmp_path, config_symbols=["btc"])
    strat = full.symbols[0].strategies[0]
    by_id = {s.stage_id: s for s in strat.stages}
    assert by_id["3"].state == "stale"
