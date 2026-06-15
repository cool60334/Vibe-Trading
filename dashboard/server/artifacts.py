from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from schemas import (
    FactorManifest,
    SelectionManifest,
    StrategyManifest,
    TestnetStatus,
)


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _manifests_base(repo_root: Path, interval: str = "1H") -> Path:
    base = repo_root / "research" / "manifests"
    return base if interval.strip().upper() == "1H" else base / interval.strip()


_INTERVAL_DIR_RE = re.compile(r"^\d+[mH]$")  # 15m, 30m, 4H, ...


def _dir_has_manifests(d: Path) -> bool:
    return any(d.glob("*/manifest.json")) or any(d.glob("factor_*.json"))


def discover_intervals(repo_root: Path) -> list[str]:
    """Intervals with data: '1H' if the manifests root has manifests, plus any
    sub-hour subdir (name like 15m/30m) that has manifests. Sorted, 1H first.
    Always returns at least ['1H'] so the selector is never empty.
    """
    base = repo_root / "research" / "manifests"
    out: list[str] = []
    if base.is_dir():
        if _dir_has_manifests(base):
            out.append("1H")
        subs = sorted(
            d.name for d in base.iterdir()
            if d.is_dir() and _INTERVAL_DIR_RE.match(d.name) and _dir_has_manifests(d)
        )
        out.extend(subs)
    return out or ["1H"]


# ---------------------------------------------------------------------------
# Strategy manifests — research/manifests/<strategy_id>/manifest.json
# ---------------------------------------------------------------------------

def list_strategy_manifests(repo_root: Path, interval: str = "1H") -> list[StrategyManifest]:
    base = _manifests_base(repo_root, interval)
    manifests: list[StrategyManifest] = []
    if not base.is_dir():
        return manifests
    for path in sorted(base.glob("*/manifest.json")):
        raw = _load_json(path)
        if raw is not None:
            try:
                manifests.append(StrategyManifest.model_validate(raw))
            except Exception:
                pass
    return manifests


def get_strategy_manifest(repo_root: Path, strategy_id: str, interval: str = "1H") -> Optional[StrategyManifest]:
    path = _manifests_base(repo_root, interval) / strategy_id / "manifest.json"
    raw = _load_json(path)
    if raw is None:
        return None
    try:
        return StrategyManifest.model_validate(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Factor manifests — research/manifests/factor_<symbol>.json
# ---------------------------------------------------------------------------

def list_factor_manifests(repo_root: Path, interval: str = "1H") -> list[FactorManifest]:
    base = _manifests_base(repo_root, interval)
    manifests: list[FactorManifest] = []
    if not base.is_dir():
        return manifests
    for path in sorted(base.glob("factor_*.json")):
        raw = _load_json(path)
        if raw is not None:
            try:
                manifests.append(FactorManifest.model_validate(raw))
            except Exception:
                pass
    return manifests


def get_factor_manifest(repo_root: Path, symbol: str, interval: str = "1H") -> Optional[FactorManifest]:
    path = _manifests_base(repo_root, interval) / f"factor_{symbol}.json"
    raw = _load_json(path)
    if raw is None:
        return None
    try:
        return FactorManifest.model_validate(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Selection manifest — research/manifests/selection.json
# ---------------------------------------------------------------------------

def get_selection_manifest(repo_root: Path, interval: str = "1H") -> Optional[SelectionManifest]:
    path = _manifests_base(repo_root, interval) / "selection.json"
    raw = _load_json(path)
    if raw is None:
        return None
    try:
        return SelectionManifest.model_validate(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Regime manifest — research/manifests/regime_<symbol>.json (raw dict)
# ---------------------------------------------------------------------------

def get_regime_manifest(repo_root: Path, symbol: str, interval: str = "1H") -> Optional[dict]:
    path = _manifests_base(repo_root, interval) / f"regime_{symbol}.json"
    return _load_json(path)


# ---------------------------------------------------------------------------
# Testnet status — runs/testnet/<id>/testnet_status.json
# ---------------------------------------------------------------------------

def list_testnet_statuses(repo_root: Path) -> list[TestnetStatus]:
    base = repo_root / "runs" / "testnet"
    statuses: list[TestnetStatus] = []
    if not base.is_dir():
        return statuses
    for path in sorted(base.glob("*/testnet_status.json")):
        raw = _load_json(path)
        if raw is not None:
            try:
                statuses.append(TestnetStatus.model_validate(raw))
            except Exception:
                pass
    return statuses


def get_testnet_status(repo_root: Path, testnet_id: str) -> Optional[TestnetStatus]:
    path = repo_root / "runs" / "testnet" / testnet_id / "testnet_status.json"
    raw = _load_json(path)
    if raw is None:
        return None
    try:
        return TestnetStatus.model_validate(raw)
    except Exception:
        return None
