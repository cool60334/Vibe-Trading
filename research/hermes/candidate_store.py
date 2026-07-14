"""Isolated candidate-feature store — NEVER writes production parquet.

Foundry output lands in manifests/candidate_features/<sym>.parquet. Production
features_<sym>.parquet / factor_values_<sym>.parquet (read by the live trader +
freshness scheduler) are off-limits; a human promote step merges candidates in.
Atomic write reuses factor_io so concurrent nightly iterations can't corrupt.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research.hermes.errors import HermesGuardError
from research.lib.factor_io import _atomic_to_parquet, _symbol_short

CANDIDATE_SUBDIR = "candidate_features"
# production basenames Foundry must never target
_PRODUCTION_PREFIXES = ("features_", "factor_values_")


class ProductionWriteError(HermesGuardError, RuntimeError):
    """Raised when a write would land outside the isolated candidate store."""


def _candidate_path(symbol: str, manifests_dir: Path) -> Path:
    return Path(manifests_dir) / CANDIDATE_SUBDIR / f"cand_{_symbol_short(symbol)}.parquet"


def code_dir(symbol: str, manifests_dir) -> Path:
    """Isolated code store for forged candidate factors. Mirrors the candidate
    parquet's containment law: everything stays under candidate_features/."""
    return Path(manifests_dir) / CANDIDATE_SUBDIR / "code" / _symbol_short(symbol)


def write_candidate_code(factor_id: str, symbol: str, manifests_dir,
                         code: str, meta: dict) -> Path:
    """Persist a forged factor's SOURCE (not just its sha) so the bridge can
    re-run it over the full span later. Foundry otherwise discards it."""
    d = code_dir(symbol, manifests_dir)
    d.mkdir(parents=True, exist_ok=True)
    py = d / f"{factor_id}.py"
    py.write_text(code, encoding="utf-8")
    (d / f"{factor_id}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    return py


def load_candidate_code(factor_id: str, symbol: str, manifests_dir) -> tuple:
    """(code, meta). Raises FileNotFoundError when the factor was never stored."""
    d = code_dir(symbol, manifests_dir)
    py = d / f"{factor_id}.py"
    if not py.exists():
        raise FileNotFoundError(f"no stored code for {symbol}:{factor_id} at {py}")
    meta_path = d / f"{factor_id}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return py.read_text(encoding="utf-8"), meta


def write_candidate(df: pd.DataFrame, symbol: str, manifests_dir) -> Path:
    """Atomically write a candidate feature frame; refuse any production path."""
    path = _candidate_path(symbol, Path(manifests_dir))
    # Hardcoded safety check: parent directory MUST be exactly "candidate_features"
    # (not the runtime value of CANDIDATE_SUBDIR, which could be monkeypatched)
    if path.parent.name != "candidate_features":
        raise ProductionWriteError(
            f"candidate write must stay under candidate_features/, got {path}"
        )
    # Additional check: filename must not start with production prefixes
    if path.name.startswith(_PRODUCTION_PREFIXES):
        raise ProductionWriteError(
            f"candidate filename must not start with production prefix, got {path}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_to_parquet(df, path)
    return path
