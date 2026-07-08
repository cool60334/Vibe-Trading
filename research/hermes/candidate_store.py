"""Isolated candidate-feature store — NEVER writes production parquet.

Foundry output lands in manifests/candidate_features/<sym>.parquet. Production
features_<sym>.parquet / factor_values_<sym>.parquet (read by the live trader +
freshness scheduler) are off-limits; a human promote step merges candidates in.
Atomic write reuses factor_io so concurrent nightly iterations can't corrupt.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from research.lib.factor_io import _atomic_to_parquet, _symbol_short

CANDIDATE_SUBDIR = "candidate_features"
# production basenames Foundry must never target
_PRODUCTION_PREFIXES = ("features_", "factor_values_")


class ProductionWriteError(RuntimeError):
    """Raised when a write would land outside the isolated candidate store."""


def _candidate_path(symbol: str, manifests_dir: Path) -> Path:
    return Path(manifests_dir) / CANDIDATE_SUBDIR / f"cand_{_symbol_short(symbol)}.parquet"


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
