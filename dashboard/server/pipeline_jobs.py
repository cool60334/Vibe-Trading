"""Pipeline job files + stage command table (A2).

The dashboard server writes a queued ``job.json`` under
``runs/pipeline_jobs/<job_id>/``; the decoupled pipeline-runner reconciles it.
Jobs are plain dicts (no Pydantic) — the frontend declares the matching TS types.
"""
from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# stage id -> research.pipeline module name (single command each).
_STAGE_MODULE = {
    "0a": "stage0a_features",
    "0": "stage0_discovery",
    "1": "stage1_factors",
    "2": "stage2_strategies",
    "2b": "stage2b_compile_signal",   # no --strategy => compiles all
    "2.5": "stage2_5_regime",
    "3": "stage3_backtest",
    "3diag": "stage3_diagnose",
    "4": "stage4_optimize",
    "5": "stage5_select",
}

# Full ordered chain for a kind="pipeline" job.
_PIPELINE_SEQUENCE = ["0a", "0", "1", "2", "2b", "2.5", "3", "3diag", "4", "5"]

# Stages exposed as individual UI "Run" buttons (2b / 3diag are chain-only).
_UI_STAGE_IDS = ["0a", "0", "1", "2", "2.5", "3", "4", "5"]


def allowed_stage_ids() -> list[str]:
    return list(_UI_STAGE_IDS)


def pipeline_sequence() -> list[str]:
    return list(_PIPELINE_SEQUENCE)


def stage_command(stage_id: str) -> list[str]:
    """argv tail for ``python <...> -m research.pipeline.<module>``.

    Raises KeyError for an unknown stage id.
    """
    module = _STAGE_MODULE[stage_id]
    return ["-m", f"research.pipeline.{module}"]


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
