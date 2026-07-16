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
