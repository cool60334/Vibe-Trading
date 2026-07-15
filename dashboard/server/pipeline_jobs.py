"""Pipeline job files + stage command table (A2).

The dashboard server writes a queued ``job.json`` under
``runs/pipeline_jobs/<job_id>/``; the decoupled pipeline-runner reconciles it.
Jobs are plain dicts (no Pydantic) — the frontend declares the matching TS types.
"""
from __future__ import annotations

import json
import os
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
# NOTE: "3diag" runs TWICE on purpose. stage4_optimize gates on diagnosis.json
# existing, so a diag pass must PRECEDE stage 4. But stage 4 is also what writes
# walk_forward_runs (the held-out OOS holdout), and stage3_diagnose only becomes
# OOS-aware once it can read that run — so a second diag pass AFTER stage 4
# produces the authoritative OOS verdict that stage 5 selection consumes. Without
# it, a clean first run diagnoses on in-sample base metrics and wrongly passes
# strategies with too few OOS trades. See research/PIPELINE.md "Stage 3-diag".
_PIPELINE_SEQUENCE = ["0a", "0", "1", "2", "2b", "2.5", "3", "3diag", "4", "3diag", "5"]

# Stages exposed as individual UI "Run" buttons (2b / 3diag are chain-only).
_UI_STAGE_IDS = ["0a", "0", "1", "2", "2.5", "3", "4", "5"]

# Stages whose work can be filtered to a single symbol via RESEARCH_ONLY_SYMBOL.
# (5 = global selection; 2b = compiles all — both stay all-scope.)
SYMBOL_AWARE_STAGES = {"0a", "0", "1", "2", "2.5", "3", "3diag", "4"}

# Mirror of research/lib/timeframe.SUPPORTED_INTERVALS — kept local so the
# dashboard server need not import the research package. Update both together.
SUPPORTED_INTERVALS = frozenset({"15m", "30m", "1H"})


def allowed_stage_ids() -> list[str]:
    return list(_UI_STAGE_IDS)


def pipeline_sequence() -> list[str]:
    return list(_PIPELINE_SEQUENCE)


def stage_uses_symbol(stage_id: str) -> bool:
    return stage_id in SYMBOL_AWARE_STAGES


def stage_command(stage_id: str) -> list[str]:
    """argv tail for ``python <...> -m research.pipeline.<module>``.

    Raises KeyError for an unknown stage id.
    """
    module = _STAGE_MODULE[stage_id]
    return ["-m", f"research.pipeline.{module}"]


# Step ids that run as a direct subprocess command (research.hermes.* modules)
# rather than a research.pipeline stage module — see step_command().
_STEP_COMMAND = {"foundry", "bridge"}


def is_command_step(step_id: str) -> bool:
    return step_id in _STEP_COMMAND


def step_command(step_id, symbol, *, overlay_dir, runs_dir, manifests_dir, zoo_dir,
                 image, llm, model, daily_max, pause_file) -> list[str]:
    """argv tail for a command step (foundry_runner / foundry_bridge).

    Raises KeyError for an unknown step id.
    """
    if step_id == "foundry":
        return ["-m", "research.hermes.foundry_runner", "run",
                "--runs-dir", str(runs_dir), "--manifests-dir", str(manifests_dir),
                "--zoo-dir", str(zoo_dir), "--image", str(image),
                "--llm", str(llm), "--model", str(model),
                "--i-will-spend-real-money", "--daily-max-llm-calls", str(daily_max),
                "--pause-file", str(pause_file)]
    if step_id == "bridge":
        return ["-m", "research.hermes.foundry_bridge", "--symbol", str(symbol),
                "--overlay-dir", str(overlay_dir), "--image", str(image),
                "--manifests-dir", str(manifests_dir)]
    raise KeyError(f"not a command step: {step_id!r}")


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
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    os.replace(tmp, p)


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


def create_job(repo_root, kind: str, stage: Optional[str] = None,
               symbol: Optional[str] = None, stress: bool = False,
               interval: str = "1H", config: Optional[dict] = None) -> dict:
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"invalid interval {interval!r}; valid: {sorted(SUPPORTED_INTERVALS)}"
        )
    if kind == "stage":
        if stage not in allowed_stage_ids():
            raise ValueError(f"invalid stage {stage!r}")
        steps = [{"stage": stage, "status": "pending", "exit_code": None}]
    elif kind == "pipeline":
        stage = None
        steps = [{"stage": s, "status": "pending", "exit_code": None}
                 for s in pipeline_sequence()]
    elif kind == "live_refresh":
        stage = None
        steps = [{"stage": s, "status": "pending", "exit_code": None}
                 for s in ("0a", "1")]
    elif kind == "foundry_mine":
        stage = None
        steps = [{"stage": "foundry", "status": "pending", "exit_code": None}]
    elif kind == "discovery_pipeline":
        stage = None
        steps = [{"stage": s, "status": "pending", "exit_code": None}
                 for s in ["bridge", *pipeline_sequence()]]
    else:
        raise ValueError(f"invalid kind {kind!r}")

    jid = _new_job_id()
    job = {
        "job_id": jid,
        "kind": kind,
        "stage": stage,
        "symbol": symbol,
        "interval": interval,
        "stress": bool(stress) and kind == "stage" and stage == "3",
        "status": "queued",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "steps": steps,
        "exit_code": None,
        "error": None,
        "cancel": False,
        "config": dict(config or {}),
        "overlay_dir": str(log_path(repo_root, jid).parent),
    }
    write_job(repo_root, job)
    return job


def log_path(repo_root, job_id: str) -> Path:
    return jobs_dir(repo_root) / job_id / "log.txt"


def tail_log(repo_root, job_id: str, lines: int = 200) -> str:
    try:
        text = log_path(repo_root, job_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def request_cancel(repo_root, job_id: str) -> Optional[dict]:
    """Flag a job for cancellation. A queued job is canceled immediately; a
    running job only gets the flag (honored between steps by the manager)."""
    job = read_job(repo_root, job_id)
    if job is None:
        return None
    if job["status"] == "queued":
        job["status"] = "canceled"
        job["finished_at"] = _now()
    job["cancel"] = True
    write_job(repo_root, job)
    return job
