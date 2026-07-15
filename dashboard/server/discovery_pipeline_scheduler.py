"""Discovery pipeline scheduler: enqueue a discovery_pipeline job per symbol on
its own cadence. Spends nothing, so it ignores FOUNDRY_PAUSE_FILE but honours
GLOBAL_PAUSE_FILE. Dedups against any active pipeline-family job (including a
manual `pipeline`) so the timer never collides with an operator's run."""
from __future__ import annotations

import logging, os, time
from datetime import datetime, timezone
from pathlib import Path

import pipeline_jobs as pj

logger = logging.getLogger("pipeline.discovery")
_PIPELINE_FAMILY = ("discovery_pipeline", "pipeline")


def _global_paused(repo_root) -> bool:
    p = os.environ.get("GLOBAL_PAUSE_FILE") or str(Path(repo_root) / "runs" / "discovery_global.pause")
    return Path(p).exists()


def _active(repo_root, symbol, now, max_age_sec):
    for j in pj.list_jobs(repo_root, limit=10000):
        if j.get("kind") not in _PIPELINE_FAMILY or j.get("symbol") != symbol:
            continue
        if j.get("status") not in ("queued", "running"):
            continue
        created = j.get("created_at")
        if created and (now - datetime.fromisoformat(created)).total_seconds() > max_age_sec:
            continue
        return j
    return None


def tick(repo_root, symbols, now, interval_sec) -> list:
    if _global_paused(repo_root):
        logger.info("discovery pipeline paused (global pause file present)")
        return []
    out = []
    for sym in symbols:
        if _active(repo_root, sym, now, 2 * interval_sec) is None:
            out.append(pj.create_job(repo_root, kind="discovery_pipeline", symbol=sym, config={
                "image": os.environ.get("DISCOVERY_IMAGE", "talos-sandbox:test"),
                "manifests_dir": str(Path(repo_root) / "research" / "manifests"),
            })["job_id"])
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    repo_root = Path(os.environ.get("REPO_ROOT", "/repo"))
    symbols = [s.strip() for s in os.environ.get("DISCOVERY_PIPELINE_SYMBOLS", "eth").split(",") if s.strip()]
    interval = float(os.environ.get("DISCOVERY_PIPELINE_INTERVAL_SEC", "86400"))
    while True:
        try:
            ids = tick(repo_root, symbols, datetime.now(timezone.utc), interval)
            if ids:
                logger.info("enqueued discovery_pipeline: %s", ids)
        except Exception:  # noqa: BLE001
            logger.exception("discovery pipeline tick failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
