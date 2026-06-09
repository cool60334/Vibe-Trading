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


def jobs_dir(repo_root) -> Path:
    return Path(repo_root) / "runs" / "pipeline_jobs"


def _job_path(repo_root, job_id: str) -> Path:
    return jobs_dir(repo_root) / job_id / "job.json"


def _new_job_id() -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}-{rnd}"


def write_job(repo_root, job: dict) -> None:
    p = _job_path(repo_root, job["job_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(job, indent=2), encoding="utf-8")


def read_job(repo_root, job_id: str) -> Optional[dict]:
    try:
        return json.loads(_job_path(repo_root, job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs(repo_root, limit: int = 50) -> list[dict]:
    base = jobs_dir(repo_root)
    out: list[dict] = []
    if base.is_dir():
        for p in base.glob("*/job.json"):
            j = read_job(repo_root, p.parent.name)
            if j is not None:
                out.append(j)
    out.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return out[:limit]


def create_job(repo_root, kind: str, stage: Optional[str] = None) -> dict:
    if kind == "stage":
        if stage not in allowed_stage_ids():
            raise ValueError(f"invalid stage {stage!r}")
        steps = [{"stage": stage, "status": "pending", "exit_code": None}]
    elif kind == "pipeline":
        stage = None
        steps = [{"stage": s, "status": "pending", "exit_code": None}
                 for s in pipeline_sequence()]
    else:
        raise ValueError(f"invalid kind {kind!r}")

    job = {
        "job_id": _new_job_id(),
        "kind": kind,
        "stage": stage,
        "status": "queued",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "steps": steps,
        "exit_code": None,
        "error": None,
        "cancel": False,
    }
    write_job(repo_root, job)
    return job
