"""Tests for dashboard/server/pipeline_jobs.py (job-file contract + stage table)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline_jobs as pj


def test_allowed_stage_ids_are_ui_stages():
    assert pj.allowed_stage_ids() == ["0a", "0", "1", "2", "2.5", "3", "4", "5"]


def test_pipeline_sequence_full_chain():
    assert pj.pipeline_sequence() == [
        "0a", "0", "1", "2", "2b", "2.5", "3", "3diag", "4", "5"
    ]


def test_stage_command_maps_to_module():
    assert pj.stage_command("1") == ["-m", "research.pipeline.stage1_factors"]
    assert pj.stage_command("2b") == ["-m", "research.pipeline.stage2b_compile_signal"]
    assert pj.stage_command("3diag") == ["-m", "research.pipeline.stage3_diagnose"]


def test_stage_command_unknown_raises():
    with pytest.raises(KeyError):
        pj.stage_command("99")
