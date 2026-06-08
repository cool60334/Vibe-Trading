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
