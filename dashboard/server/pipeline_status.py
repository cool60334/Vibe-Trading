"""Pure builder: scan research/manifests/ -> PipelineStatus (A1 status page).

No I/O beyond reading files. Defensive: any unreadable/invalid artifact is
treated as missing rather than raising, so the endpoint never 500s.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schemas import (
    PipelineStatus,
    StageStatus,
    StrategyPipeline,
    SymbolPipeline,
)


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _artifact_time(path: Path, raw: Optional[dict]) -> Optional[str]:
    """ISO time for an artifact: JSON ``generated_at`` if present, else mtime."""
    if raw and isinstance(raw.get("generated_at"), str):
        return raw["generated_at"]
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def discover_symbols(manifests_dir: Path, config_symbols: list[str]) -> list[str]:
    """Union of config symbols (first, in order) and symbols found on disk."""
    found: set[str] = set()
    for pattern in ("factor_*.json", "evidence_*.json", "candidates_*.json"):
        for p in manifests_dir.glob(pattern):
            name = p.stem.split(".")[0]  # candidates_eth.failed -> candidates_eth
            for prefix in ("factor_", "evidence_", "candidates_"):
                if name.startswith(prefix):
                    sym = name[len(prefix):]
                    if sym and not sym.startswith("values_"):
                        found.add(sym)
                    break
    extra = sorted(found - set(config_symbols))
    return list(config_symbols) + extra
