"""Talos Foundry runner CLI (design law 2: Talos writes decision files, a
separate runner picks them up; it never inline-runs a stage itself).

`enqueue` writes a job.json; `run` reconciles the queued jobs. The runner never
touches the network — it reads an OHLCV parquet, it does not fetch candles."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

import pandas as pd

from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed

log = logging.getLogger(__name__)


def resolve_image_id(tag: str) -> str:
    """Resolve a human-readable tag to its immutable content-addressed image id.
    A tag can be re-pointed under you between the run that vets a factor and the
    run that reproduces it; the image id cannot. Raises SandboxError (infra) if
    the tag does not resolve on this host, so a config typo aborts before the
    first container starts rather than burning every hypothesis's retries."""
    try:
        proc = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                              capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SandboxError(f"could not inspect image {tag!r}: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError(
            f"sandbox image {tag!r} does not resolve on this host: "
            f"{(proc.stderr or '').strip()[:200]}")
    return proc.stdout.strip()


def load_ohlcv(path) -> pd.DataFrame:
    """Read an OHLCV parquet. Never networks. Validates a `close` column and a
    tz-aware index so a misaligned/naive table fails loudly here rather than
    producing NaN forward returns deep inside the gate."""
    df = pd.read_parquet(path)
    if "close" not in df.columns:
        raise ValueError(f"ohlcv parquet {path} has no 'close' column")
    if getattr(df.index, "tz", None) is None:
        raise ValueError(f"ohlcv parquet {path} index is not tz-aware (need UTC)")
    return df


from research.hermes.orchestrator import run_foundry_job     # top-of-file import


def _queued_jobs(runs_dir) -> list:
    jobs_root = Path(runs_dir) / "foundry_jobs"
    out = []
    if not jobs_root.exists():
        return out
    for job_path in jobs_root.rglob("job.json"):
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue                                   # a half-written peer; next pass gets it
        if job.get("status") == "queued":
            out.append((job.get("created_at", ""), job_path, job))
    out.sort(key=lambda t: t[0])                       # deterministic: created_at order
    return out


def reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir, budget=None) -> list:
    """Run every queued foundry job in created_at order (write-file->reconcile).

    A bare SandboxError (docker/image infra, NOT a SandboxRunFailed subclass)
    aborts the whole batch — the next job would hit the same wall. Any other
    failure leaves that job status=failed (already written by run_foundry_job)
    and the batch continues, so one bad-code job does not poison its neighbours."""
    summaries = []
    for _created, job_path, job in _queued_jobs(runs_dir):
        try:
            ohlcv = load_ohlcv(job["params"]["ohlcv_path"])
            summaries.append(run_foundry_job(
                job_path, manifests_dir=manifests_dir, llm=llm, sandbox=sandbox,
                zoo_dir=zoo_dir, ohlcv=ohlcv, budget=budget))
        except SandboxError as exc:
            if not isinstance(exc, SandboxRunFailed):
                log.error("infra fault on %s; aborting the batch: %s", job_path, exc)
                raise                                  # Layer A: stop the whole queue
            log.warning("job %s failed (repairable, buried): %s", job_path, exc)
        except Exception as exc:                       # noqa: BLE001 job-level, keep going
            log.warning("job %s failed: %s; continuing", job_path, exc)
    return summaries
