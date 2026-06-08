"""Tests for dashboard/server/pipeline_status.py (pure status builder)."""
from __future__ import annotations

import json
from pathlib import Path

from schemas import PipelineStatus, StageStatus


def test_stage_status_model_round_trips():
    s = StageStatus(
        stage_id="0a", label="Features/Evidence", state="done",
        generated_at="2026-06-04T13:45:41+00:00",
        metric_label="top|IC|", metric_value="0.088",
    )
    assert s.state == "done"
    # invalid state rejected
    import pytest
    with pytest.raises(Exception):
        StageStatus(stage_id="0a", label="x", state="bogus",
                    generated_at=None, metric_label=None, metric_value=None)


def test_pipeline_status_empty():
    ps = PipelineStatus(generated_at="2026-06-08T00:00:00+00:00", symbols=[])
    assert ps.symbols == []
