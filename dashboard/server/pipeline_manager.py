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

# A stage/command step exits 201 to mean "the killswitch/pause-file tripped
# mid-run, not a failure" -- execute_job marks the job "paused" (resumable)
# instead of "failed" for this code. See research/hermes/foundry_runner.py.
EXIT_PAUSED = 201


def stage_env(overrides: dict) -> dict:
    """Environment for a stage subprocess. MUST inherit the parent env: passing
    only overrides would drop PATH and the stage would fail to start."""
    return {**os.environ, **{k: str(v) for k, v in overrides.items()}}


def cleanup_overlay(job_dir) -> None:
    """Delete the per-run Foundry overlay cache.

    NOTE: the automated job runner does not currently enable or materialize
    the overlay (RESEARCH_INCLUDE_FOUNDRY/RESEARCH_FOUNDRY_OVERLAY_DIR are
    never set here) -- that wiring is deferred to a future spec. This
    cleanup is a no-op until that wiring exists; it only matters for the
    manual CLI path (research.hermes.foundry_bridge).
    """
    d = Path(job_dir)
    if not d.exists():
        return
    for p in list(d.glob("foundry_overlay_*.parquet")) + list(d.glob("foundry_manifest_*.json")):
        try:
            p.unlink()
        except OSError as exc:                       # noqa: BLE001 - teardown must not crash
            logger.warning("could not remove overlay cache %s: %s", p, exc)


def _default_runner(repo_root: Path, stage_id: str, symbol: Optional[str], log_fp,
                    stress: bool = False, interval: str = "1H",
                    live_refresh: bool = False, config: Optional[dict] = None) -> int:
    """Run one stage as ``python -m research.pipeline.<module>``; stdout+stderr
    stream into the job's log file. If ``symbol`` is set, scope it via
    RESEARCH_ONLY_SYMBOL. If ``stress`` is set AND this is stage 3, append
    ``--stress`` so stage 3 also runs the 2x/3x cost-stress sweep. A sub-hour
    ``interval`` is applied globally via RESEARCH_INTERVAL (the config loader's
    interval override); "1H" leaves it unset for zero regression. If ``config``
    is set (a ``discovery_pipeline`` job's non-secret config, carrying
    ``overlay_dir``), the stage sees the Foundry overlay via
    RESEARCH_INCLUDE_FOUNDRY/RESEARCH_FOUNDRY_OVERLAY_DIR. Returns the process
    exit code."""
    argv = [sys.executable, *pj.stage_command(stage_id)]
    if stress and stage_id == "3":
        argv.append("--stress")

    overrides = {}
    overrides["PYTHONPATH"] = str(repo_root) + os.pathsep + os.environ.get("PYTHONPATH", "")
    if symbol:
        overrides["RESEARCH_ONLY_SYMBOL"] = symbol
    if interval and interval != "1H":
        overrides["RESEARCH_INTERVAL"] = interval
    if live_refresh and stage_id == "0a":
        overrides["LIVE_OI_REFRESH"] = "1"
    if config is not None:
        overrides["RESEARCH_INCLUDE_FOUNDRY"] = "1"
        overrides["RESEARCH_FOUNDRY_OVERLAY_DIR"] = config.get("overlay_dir", "")

    env = stage_env(overrides)
    # Explicitly remove keys that should not be set based on conditions
    if not symbol:
        env.pop("RESEARCH_ONLY_SYMBOL", None)
    if not (interval and interval != "1H"):
        env.pop("RESEARCH_INTERVAL", None)
    if not (live_refresh and stage_id == "0a"):
        env.pop("LIVE_OI_REFRESH", None)
    if config is None:
        env.pop("RESEARCH_INCLUDE_FOUNDRY", None)
        env.pop("RESEARCH_FOUNDRY_OVERLAY_DIR", None)

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
        # Tests inject `runner` to fake out every step (stage AND command) via
        # a single seam; production leaves this None so _command_runner takes
        # the real subprocess path below.

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
        # live_refresh jobs jump ahead of research jobs so the hourly factor
        # refresh never starves behind a manually-queued full pipeline.
        queued.sort(key=lambda j: (0 if j.get("kind") == "live_refresh" else 1,
                                   j.get("created_at", "")))
        return queued[0] if queued else None

    def execute_job(self, job: dict) -> None:
        job["status"] = "running"
        job["started_at"] = pj._now()
        pj.write_job(self.repo_root, job)

        lp = pj.log_path(self.repo_root, job["job_id"])
        lp.parent.mkdir(parents=True, exist_ok=True)
        job_dir = lp.parent

        try:
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
                    if pj.is_command_step(step["stage"]):
                        rc = self._command_runner(job, step["stage"], fp)
                    else:
                        step_symbol = (
                            job.get("symbol")
                            if job.get("symbol") and pj.stage_uses_symbol(step["stage"])
                            else None
                        )
                        # A discovery_pipeline job's stage steps run after the
                        # "bridge" step materialized the Foundry overlay
                        # parquet -- carry overlay_dir (+ any other non-secret
                        # config) so the stage subprocess can pick it up.
                        step_config = (
                            {**job.get("config", {}), "overlay_dir": job["overlay_dir"]}
                            if job.get("kind") == "discovery_pipeline" else None
                        )
                        rc = self._runner(
                            self.repo_root, step["stage"], step_symbol, fp,
                            job.get("stress", False), job.get("interval", "1H"),
                            job.get("kind") == "live_refresh" and step["stage"] == "0a",
                            config=step_config,
                        )

                step["exit_code"] = rc
                step["status"] = "succeeded" if rc == 0 else "failed"
                pj.write_job(self.repo_root, job)

                if rc == EXIT_PAUSED:
                    step["exit_code"] = rc
                    step["status"] = "paused"
                    job["status"] = "paused"
                    job["exit_code"] = rc
                    job["finished_at"] = pj._now()
                    pj.write_job(self.repo_root, job)
                    logger.info("job %s paused by killswitch at %s", job["job_id"], step["stage"])
                    return

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
        finally:
            cleanup_overlay(job_dir)

    def _command_runner(self, job: dict, step_id: str, log_fp) -> int:
        """Run a command step ("foundry" / "bridge") -- a direct
        ``research.hermes.*`` subprocess rather than a ``research.pipeline``
        stage module. Builds argv via ``pipeline_jobs.step_command`` from the
        job's non-secret ``config`` (LLM API key is never read here -- the
        subprocess reads it from ``agent/.env`` itself). Returns the process
        exit code.

        If a fake ``runner`` was injected (tests), delegate to it instead of
        shelling out for real -- this is the same seam ``_runner`` uses for
        stage steps, so a single injected callable can fake out an entire
        job's steps regardless of whether a step is a command step or a
        research.pipeline stage.
        """
        if self._runner is not _default_runner:
            return self._runner(
                self.repo_root, step_id, job.get("symbol"), log_fp,
                config=job.get("config"),
            )

        c = job.get("config", {})
        argv = [sys.executable, *pj.step_command(
            step_id, job.get("symbol"), overlay_dir=job["overlay_dir"],
            runs_dir=c.get("runs_dir"), manifests_dir=c.get("manifests_dir"),
            zoo_dir=c.get("zoo_dir"), image=c.get("image"), llm=c.get("llm"),
            model=c.get("model"), daily_max=c.get("daily_max"),
            pause_file=c.get("pause_file"),
        )]
        env = stage_env({"PYTHONPATH": str(self.repo_root) + os.pathsep + os.environ.get("PYTHONPATH", "")})
        proc = subprocess.run(
            argv, cwd=str(self.repo_root), env=env,
            stdout=log_fp, stderr=subprocess.STDOUT, text=True,
        )
        return proc.returncode

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
