"""Foundry miner scheduler: enqueue a foundry_mine job per symbol on a cadence.
Write-only; the serial pipeline_manager executes. Paused by FOUNDRY_PAUSE_FILE
(money) or GLOBAL_PAUSE_FILE (ops)."""
from __future__ import annotations

import logging, os, time
from datetime import datetime, timezone
from pathlib import Path

import pipeline_jobs as pj

logger = logging.getLogger("pipeline.foundry_miner")


def _paused(repo_root) -> bool:
    for env, default in (("FOUNDRY_PAUSE_FILE", "runs/foundry_auto.pause"),
                         ("GLOBAL_PAUSE_FILE", "runs/discovery_global.pause")):
        p = os.environ.get(env) or str(Path(repo_root) / default)
        if Path(p).exists():
            return True
    return False


def _active(repo_root, symbol, now, max_age_sec):
    for j in pj.list_jobs(repo_root, limit=10000):
        if j.get("kind") != "foundry_mine" or j.get("symbol") != symbol:
            continue
        status = j.get("status")
        if status not in ("queued", "running"):
            continue
        # A queued job is never "dead" -- foundry_mine is deliberately
        # low-priority (pipeline_manager sorts it last), so it can legitimately
        # sit queued far past max_age_sec while just waiting its turn. Only a
        # `running` job past the window plausibly reflects a runner that died
        # mid-step, so staleness only applies there.
        if status == "running":
            created = j.get("created_at")
            if created and (now - datetime.fromisoformat(created)).total_seconds() > max_age_sec:
                continue
        return j
    return None


def _config(repo_root) -> dict:
    return {
        "image": os.environ.get("DISCOVERY_IMAGE", "talos-sandbox:test"),
        "llm": os.environ.get("DISCOVERY_LLM", "openai"),
        "model": os.environ.get("DISCOVERY_MODEL", "gpt-4o-mini"),
        "daily_max": int(os.environ.get("DISCOVERY_DAILY_MAX_LLM_CALLS", "6")),
        "runs_dir": os.environ.get("FOUNDRY_RUNS_DIR", str(Path(repo_root) / "runs" / "foundry_auto")),
        "zoo_dir": os.environ.get("DISCOVERY_ZOO_DIR", str(Path(repo_root) / "agent" / "src" / "factors" / "zoo")),
        "manifests_dir": str(Path(repo_root) / "research" / "manifests"),
        "pause_file": os.environ.get("FOUNDRY_PAUSE_FILE", str(Path(repo_root) / "runs" / "foundry_auto.pause")),
    }


def tick(repo_root, symbols, now, interval_sec) -> list:
    if _paused(repo_root):
        logger.info("foundry miner paused (pause file present)")
        return []
    out = []
    cfg = _config(repo_root)
    for sym in symbols:
        if _active(repo_root, sym, now, 2 * interval_sec) is None:
            out.append(pj.create_job(repo_root, kind="foundry_mine", symbol=sym, config=cfg)["job_id"])
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    repo_root = Path(os.environ.get("REPO_ROOT", "/repo"))
    symbols = [s.strip() for s in os.environ.get("FOUNDRY_MINE_SYMBOLS", "eth").split(",") if s.strip()]
    interval = float(os.environ.get("FOUNDRY_MINE_INTERVAL_SEC", "86400"))
    while True:
        try:
            ids = tick(repo_root, symbols, datetime.now(timezone.utc), interval)
            if ids:
                logger.info("enqueued foundry_mine: %s", ids)
        except Exception:  # noqa: BLE001
            logger.exception("foundry miner tick failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
