"""Inducted factors: vetted Foundry factor code that has been promoted into the
production factor library (committed, importable). stage0a computes these daily.

Discovery is a directory scan (no central registry -> no merge conflicts):
a factor is inducted iff research/lib/inducted/<sym>/<id>.py AND <id>.meta.json
both exist.
"""
from __future__ import annotations

from pathlib import Path

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[2]                      # research/lib/inducted_factors.py -> repo root


def _symbol_short(symbol: str) -> str:
    s = str(symbol or "").strip()
    if "/" in s:
        s = s.split("/")[0]
    if "-" in s:
        s = s.split("-")[0]
    return s.lower()


def inducted_dir(symbol: str, root=None) -> Path:
    base = Path(root) if root is not None else (_REPO_ROOT / "research" / "lib")
    return base / "inducted" / _symbol_short(symbol)


def inducted_names(symbol: str, root=None) -> set:
    d = inducted_dir(symbol, root=root)
    if not d.is_dir():
        return set()
    return {p.stem for p in d.glob("*.py")
            if p.stem != "__init__" and (d / f"{p.stem}.meta.json").exists()}


import importlib.util
import logging
import os

import pandas as pd

log = logging.getLogger(__name__)


def _blacklisted() -> set:
    p = os.environ.get("INDUCTED_BLACKLIST_FILE", str(_REPO_ROOT / "runs" / "inducted_blacklist.txt"))
    path = Path(p)
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _load_compute(symbol: str, factor_id: str, path: Path):
    # A globally-unique module name -> no sys.modules cache clobber across
    # symbols, and each module is its own namespace (helpers can't collide).
    name = f"inducted_{_symbol_short(symbol)}_{factor_id}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.compute


def compute_inducted(panel, symbol: str, root=None) -> dict:
    """Compute every inducted factor for `symbol` over the full-library `panel`.

    Soft-fails per factor: a factor that raises (e.g. a missing column) is
    NaN-filled (Series of NaN aligned to panel.index) with a log line -- the
    key is always present and the batch schema never varies. A
    blacklisted factor (hot kill-switch) is NaN-filled (schema stays stable; the
    trader's own NaN-guard then pauses the strategy).
    """
    d = inducted_dir(symbol, root=root)
    black = _blacklisted()
    out: dict = {}
    for fid in sorted(inducted_names(symbol, root=root)):
        if fid in black:
            # NaN-fill (not skip): keeping the column present holds the feature
            # schema stable so the trader never KeyErrors on a missing column;
            # an all-NaN signal makes its own NaN-guard pause the strategy -- the
            # intended kill-switch effect, without a crash. (Same reasoning as the
            # failed-factor branch below.)
            log.warning("inducted factor %s is blacklisted (kill-switch); NaN-filling", fid)
            out[fid] = pd.Series(float("nan"), index=panel.index)
            continue
        try:
            compute = _load_compute(symbol, fid, d / f"{fid}.py")
            series = compute(panel)
            if not isinstance(series, pd.Series):
                raise TypeError(f"compute() returned {type(series).__name__}, expected pd.Series")
            out[fid] = series.reindex(panel.index)
        except Exception as exc:                    # noqa: BLE001 - soft-fail, never break the batch
            log.error("inducted factor %s failed (%s); NaN-filling for this run", fid, exc)
            out[fid] = pd.Series(float("nan"), index=panel.index)
    return out
