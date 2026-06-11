"""Pipeline job runner — global-serial executor (A2).

Runs as the entrypoint of the decoupled ``pipeline-runner`` container. Polls
``runs/pipeline_jobs/`` for the oldest queued job and runs its stage steps
sequentially, appending to ``log.txt`` and updating ``job.json``. One job at a
time (no shared-file races, bounded resources). The dashboard server only
*writes* queued jobs, so deploying/restarting the dashboard never kills a run.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal as _signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import pipeline_jobs as pj

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("pipeline.manager")


def _default_runner(repo_root: Path, stage_id: str, symbol: Optional[str], log_fp,
                    stress: bool = False) -> int:
    """Run one stage as ``python -m research.pipeline.<module>``; stdout+stderr
    stream into the job's log file. If ``symbol`` is set, scope it via
    RESEARCH_ONLY_SYMBOL. If ``stress`` is set AND this is stage 3, append
    ``--stress`` so stage 3 also runs the 2x/3x cost-stress sweep. Returns the
    process exit code."""
    argv = [sys.executable, *pj.stage_command(stage_id)]
    if stress and stage_id == "3":
        argv.append("--stress")
    env = {**os.environ}
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    if symbol:
        env["RESEARCH_ONLY_SYMBOL"] = symbol
    else:
        env.pop("RESEARCH_ONLY_SYMBOL", None)
    proc = subprocess.run(
        argv, cwd=str(repo_root), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True,
    )
    return proc.returncode


class Manager:
    """Global-serial executor. ``runner`` is injectable for tests."""

    def __init__(self, repo_root, *, runner: Optional[Callable] = None) -> None:
        self.repo_root = Path(repo_root)
        self._runner = runner or _default_runner

    def reconcile_startup(self) -> None:
        """A job left ``running`` means the runner died mid-step; mark it failed
        (stages aren't resumable). Queued jobs are untouched."""
        for job in pj.list_jobs(self.repo_root, limit=10000):
            if job.get("status") == "running":
                job["status"] = "failed"
                job["error"] = "interrupted by runner restart"
                job["finished_at"] = pj._now()
                pj.write_job(self.repo_root, job)

    def _oldest_queued(self) -> Optional[dict]:
        queued = [j for j in pj.list_jobs(self.repo_root, limit=10000)
                  if j.get("status") == "queued"]
        queued.sort(key=lambda j: j.get("created_at", ""))
        return queued[0] if queued else None

    def execute_job(self, job: dict) -> None:
        job["status"] = "running"
        job["started_at"] = pj._now()
        pj.write_job(self.repo_root, job)

        lp = pj.log_path(self.repo_root, job["job_id"])
        lp.parent.mkdir(parents=True, exist_ok=True)

        for step in job["steps"]:
            fresh = pj.read_job(self.repo_root, job["job_id"]) or job
            if fresh.get("cancel"):
                job["status"] = "canceled"
                job["finished_at"] = pj._now()
                pj.write_job(self.repo_root, job)
                logger.info("job %s canceled before stage %s", job["job_id"], step["stage"])
                return

            step["status"] = "running"
            pj.write_job(self.repo_root, job)
            logger.info("job %s running stage %s", job["job_id"], step["stage"])

            with open(lp, "a", encoding="utf-8") as fp:
                fp.write(f"\n===== stage {step['stage']} @ {pj._now()} =====\n")
                fp.flush()
                step_symbol = (
                    job.get("symbol")
                    if job.get("symbol") and pj.stage_uses_symbol(step["stage"])
                    else None
                )
                rc = self._runner(
                    self.repo_root, step["stage"], step_symbol, fp,
                    job.get("stress", False),
                )

            step["exit_code"] = rc
            step["status"] = "succeeded" if rc == 0 else "failed"
            pj.write_job(self.repo_root, job)

            if rc != 0:
                job["status"] = "failed"
                job["exit_code"] = rc
                job["error"] = f"stage {step['stage']} exited {rc}"
                job["finished_at"] = pj._now()
                pj.write_job(self.repo_root, job)
                logger.warning("job %s failed at stage %s (rc=%s)", job["job_id"], step["stage"], rc)
                return

        job["status"] = "succeeded"
        job["finished_at"] = pj._now()
        pj.write_job(self.repo_root, job)
        logger.info("job %s succeeded", job["job_id"])

    def scan_once(self) -> bool:
        """Run the oldest queued job to completion. Returns True if one ran."""
        job = self._oldest_queued()
        if job is None:
            return False
        self.execute_job(job)
        return True

    def run(self, scan_interval: float = 3.0) -> None:
        shutdown = {"flag": False}

        def _sig(signum, frame):
            logger.info("signal %s — stopping after current job", signum)
            shutdown["flag"] = True

        _signal.signal(_signal.SIGTERM, _sig)
        _signal.signal(_signal.SIGINT, _sig)

        self.reconcile_startup()
        logger.info("pipeline manager started — watching %s", pj.jobs_dir(self.repo_root))
        while not shutdown["flag"]:
            try:
                ran = self.scan_once()
            except Exception as e:
                logger.error("scan_once error: %s", e, exc_info=True)
                ran = False
            if not ran:
                time.sleep(scan_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Vibe-Trading pipeline job runner")
    parser.add_argument("--repo-root", default="/repo")
    parser.add_argument("--scan-interval", type=float, default=3.0)
    args = parser.parse_args()
    Manager(args.repo_root).run(scan_interval=args.scan_interval)


if __name__ == "__main__":
    main()
