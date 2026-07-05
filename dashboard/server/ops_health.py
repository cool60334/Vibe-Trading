"""One all-UTC operational snapshot: deploy version, factor freshness,
job-queue counts, trader heartbeats.

Every timestamp passes through verbatim from artifacts that are already
UTC ISO; nothing here reads server-local time except generated_at (UTC).
Read-only — safe to call from a request handler.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_META_RE = re.compile(r"^factor_values_([a-z0-9]+)\.meta\.json$")
_JOB_STATUSES = ("queued", "running", "succeeded", "failed", "canceled")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def deployed_version(repo_root: "str | Path") -> dict:
    """{branch, commit} from .git files directly (no git binary needed).
    Deploys are `git pull` on the server, so HEAD == deployed version."""
    repo_root = Path(repo_root)
    try:
        head = (repo_root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {"branch": None, "commit": None}
    if not head.startswith("ref: "):
        return {"branch": None, "commit": head or None}  # detached HEAD
    ref = head[5:].strip()
    branch = ref.rsplit("/", 1)[-1]
    loose = repo_root / ".git" / ref
    if loose.exists():
        try:
            return {"branch": branch, "commit": loose.read_text(encoding="utf-8").strip()}
        except OSError:
            pass
    packed = repo_root / ".git" / "packed-refs"
    if packed.exists():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1] == ref:
                    return {"branch": branch, "commit": parts[0]}
        except OSError:
            pass
    return {"branch": branch, "commit": None}


def build_ops_health(repo_root: "str | Path") -> dict:
    repo_root = Path(repo_root)

    freshness = []
    manifests = repo_root / "research" / "manifests"
    if manifests.is_dir():
        for p in sorted(manifests.iterdir()):
            m = _META_RE.match(p.name)
            if not m:
                continue
            meta = _read_json(p) or {}
            freshness.append({
                "symbol": m.group(1),
                "generated_at": meta.get("generated_at"),
                "index_end": meta.get("index_end"),
            })

    jobs = {s: 0 for s in _JOB_STATUSES}
    jobs_dir = repo_root / "runs" / "pipeline_jobs"
    if jobs_dir.is_dir():
        for jp in jobs_dir.glob("*/job.json"):
            status = (_read_json(jp) or {}).get("status")
            if status in jobs:
                jobs[status] += 1

    traders = []
    testnet = repo_root / "runs" / "testnet"
    if testnet.is_dir():
        for d in sorted(p for p in testnet.iterdir() if p.is_dir()):
            ctrl = _read_json(d / "control.json") or {}
            live = (_read_json(d / "testnet_status.json") or {}).get("live") or {}
            traders.append({
                "testnet_id": d.name,
                "desired_state": ctrl.get("desired_state"),
                "status": live.get("status"),
                "updated_at": live.get("updated_at"),
                "equity": live.get("equity"),
                "trades": live.get("trades"),
            })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "version": deployed_version(repo_root),
        "factor_freshness": freshness,
        "jobs": jobs,
        "traders": traders,
    }
