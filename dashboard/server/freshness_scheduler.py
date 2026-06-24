"""Hourly L/S factor-refresh scheduler (Phase-1).

Enqueues a ``live_refresh`` pipeline job for each configured symbol when none is
active; the decoupled pipeline_manager executes it serially. Writes only — never
runs stages itself.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pipeline_jobs as pj

logger = logging.getLogger("pipeline.freshness")


def _active_live_refresh(repo_root, symbol: str, now: datetime,
                         max_age_sec: float) -> Optional[dict]:
    """Return an active (queued/running) live_refresh job for *symbol*, ignoring
    zombies older than *max_age_sec* so a stuck job cannot block enqueues."""
    for j in pj.list_jobs(repo_root, limit=10000):
        if j.get("kind") != "live_refresh" or j.get("symbol") != symbol:
            continue
        if j.get("status") not in ("queued", "running"):
            continue
        created = j.get("created_at")
        if created:
            age = (now - datetime.fromisoformat(created)).total_seconds()
            if age > max_age_sec:
                continue  # zombie — treat as dead
        return j
    return None


def tick(repo_root, symbols: list[str], now: datetime,
         interval_sec: float) -> list[str]:
    """One scheduling pass. Returns the job_ids enqueued this pass."""
    enqueued: list[str] = []
    for sym in symbols:
        if _active_live_refresh(repo_root, sym, now, 2 * interval_sec) is None:
            job = pj.create_job(repo_root, kind="live_refresh", symbol=sym,
                                interval="1H")
            enqueued.append(job["job_id"])
    return enqueued


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    repo_root = Path(os.environ.get("REPO_ROOT", "/repo"))
    symbols = [s.strip() for s in os.environ.get("FRESHNESS_SYMBOLS", "sol").split(",") if s.strip()]
    interval_sec = float(os.environ.get("FRESHNESS_INTERVAL_SEC", "3600"))
    logger.info("freshness_scheduler start: symbols=%s interval=%ss", symbols, interval_sec)
    while True:
        now = datetime.now(timezone.utc)
        try:
            ids = tick(repo_root, symbols, now, interval_sec)
            if ids:
                logger.info("enqueued live_refresh jobs: %s", ids)
        except Exception:  # noqa: BLE001
            logger.exception("freshness tick failed")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
