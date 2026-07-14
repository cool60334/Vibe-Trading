"""
factor_io.py — Persist and reload factor time series as Parquet files.

Public API:
    dump_factor_values(symbol, factor_series_dict, manifests_dir) -> Path
    load_factor_values(symbol) -> pd.DataFrame
    load_factor_meta(symbol) -> dict

    dump_features(symbol, feature_series_dict, manifests_dir) -> Path
    load_features(symbol, manifests_dir=None, include_foundry=None) -> pd.DataFrame
    load_features_meta(symbol, manifests_dir=None) -> dict
    dump_evidence(symbol, evidence, manifests_dir) -> Path
    load_evidence(symbol, manifests_dir=None) -> list | dict
    append_feature_column(symbol, key, series, manifests_dir=None, coverage_threshold=0.5) -> None
    foundry_enabled() -> bool
    load_manifest(symbol, manifests_dir=None, include_foundry=None) -> dict
"""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    pass

SCHEMA_VERSION = 1
FEATURES_SCHEMA_VERSION = 1

ENV_INCLUDE_FOUNDRY = "RESEARCH_INCLUDE_FOUNDRY"
ENV_FOUNDRY_OVERLAY_DIR = "RESEARCH_FOUNDRY_OVERLAY_DIR"


def foundry_enabled() -> bool:
    """Strict '1' comparison. bool(os.getenv(...)) would treat "0" and "false"
    as True — i.e. the overlay would be ON by default, which would leak research
    factors into every read. Off unless explicitly '1'."""
    return os.getenv(ENV_INCLUDE_FOUNDRY) == "1"


def _overlay_dir():
    d = os.getenv(ENV_FOUNDRY_OVERLAY_DIR)
    return Path(d) if d else None


# Resolve manifests dir relative to this file's location:
# factor_io.py lives at research/lib/factor_io.py
# research/ is two parents up from this file when resolved as:
#   factor_io.py -> lib/ -> research/
_LIB_DIR = Path(__file__).resolve().parent       # research/lib/
_RESEARCH_DIR = _LIB_DIR.parent                  # research/
# Test hook: when set (monkeypatched), the manifests base is taken from here
# instead of the active_manifests_dir() helper. None in production.
_MANIFESTS_BASE_OVERRIDE: "Path | None" = None


def _default_manifests_dir() -> Path:
    """Active manifests dir: interval-namespaced via RESEARCH_INTERVAL."""
    if _MANIFESTS_BASE_OVERRIDE is not None:
        iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
        if iv and iv != "1H":
            return _MANIFESTS_BASE_OVERRIDE / iv
        return _MANIFESTS_BASE_OVERRIDE
    from lib.timeframe import active_manifests_dir
    return active_manifests_dir()


def _check_index_freq(df: "pd.DataFrame") -> None:
    """Raise if the parquet's median index spacing doesn't match RESEARCH_INTERVAL.

    Hard-fail (not a warning): loading a mismatched-interval factor parquet would
    silently produce a fake backtest (e.g. 1H factors in a 30m run). Raising is
    deterministic regardless of the process warning filter.
    """
    iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
    if not iv or len(df) < 3:
        return
    try:
        from lib.timeframe import bars_per_hour
        expected_min = 60 / bars_per_hour(iv)
    except Exception:
        return
    deltas = df.index.to_series().diff().dropna()
    if deltas.empty:
        return
    median_min = deltas.median().total_seconds() / 60.0
    if abs(median_min - expected_min) > 0.5:
        raise ValueError(
            f"factor_values index spacing ~{median_min:.0f}m != expected {expected_min:.0f}m "
            f"for RESEARCH_INTERVAL={iv!r}: loaded a mismatched-interval parquet "
            f"(re-run stage1 at this interval)."
        )


def _symbol_short(symbol: str) -> str:
    """Normalize symbol to short lowercase form.

    Accepts either short form ("eth") or full ticker ("ETH-USDT-SWAP").
    Returns lowercase short base, e.g. "eth".
    """
    s = symbol.strip()
    if "-" in s:
        # e.g. "ETH-USDT-SWAP" → "eth"
        return s.split("-")[0].lower()
    return s.lower()


def _atomic_to_parquet(df: "pd.DataFrame", path: Path) -> None:
    """Write a parquet file atomically: write to a temp file in the same
    directory, then os.replace onto the target so a concurrent reader (the
    separate trader container) never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".parquet.tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        df.to_parquet(tmp_path, engine="pyarrow", compression="snappy")
        os.chmod(tmp_path, 0o644)  # mkstemp is 0600 regardless of umask; readers need group read
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (temp file + os.replace)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o644)  # mkstemp is 0600 regardless of umask; readers need group read
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def dump_factor_values(
    symbol: str,
    factor_series_dict: dict[str, pd.Series],
    manifests_dir: Path,
) -> Path:
    """Write factor time series to parquet + sidecar meta.json.

    Parameters
    ----------
    symbol:
        Symbol name, e.g. "eth" or "ETH-USDT-SWAP".
    factor_series_dict:
        Mapping from factor name → aligned pd.Series (DatetimeIndex, UTC).
    manifests_dir:
        Directory to write output into.  Tests pass tmp_path here.

    Returns
    -------
    Path to the written parquet file.
    """
    try:
        import pyarrow  # noqa: F401  # imported to trigger helpful error early
    except ImportError as exc:
        raise ImportError(
            "pyarrow is required to write factor parquet files.  "
            "Install it with:  pip install pyarrow"
        ) from exc

    sym_short = _symbol_short(symbol)

    # Build DataFrame — one column per factor, DatetimeIndex.
    if not factor_series_dict:
        raise ValueError(f"factor_series_dict is empty for symbol '{symbol}'")

    df = pd.DataFrame(factor_series_dict)
    # Ensure index is UTC tz-aware (coerce if naive).
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # Cast all columns to float64.
    df = df.astype("float64")

    manifests_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = manifests_dir / f"factor_values_{sym_short}.parquet"
    meta_path = manifests_dir / f"factor_values_{sym_short}.meta.json"

    _atomic_to_parquet(df, parquet_path)

    factor_names = list(factor_series_dict.keys())
    n_rows = len(df)
    generated_at = datetime.now(timezone.utc).isoformat()
    index_start = df.index.min().isoformat() if n_rows > 0 else ""
    index_end = df.index.max().isoformat() if n_rows > 0 else ""

    meta = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "generated_at": generated_at,
        "factor_names": factor_names,
        "index_start": index_start,
        "index_end": index_end,
        "n_rows": n_rows,
    }
    _atomic_write_text(meta_path, json.dumps(meta, indent=2))

    n_factors = len(factor_names)
    print(f"[stage1] {sym_short}: wrote factor_values parquet ({n_factors} factors, {n_rows} rows)")

    return parquet_path


def load_factor_values(symbol: str, manifests_dir: Path | None = None) -> pd.DataFrame:
    """Load factor parquet for the given symbol.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).

    Raises
    ------
    FileNotFoundError
        If the parquet file does not exist.  Message includes
        "run stage1_factors first" as a hint.
    """
    sym_short = _symbol_short(symbol)
    mdir = manifests_dir if manifests_dir is not None else _default_manifests_dir()
    parquet_path = mdir / f"factor_values_{sym_short}.parquet"

    if not parquet_path.exists():
        raise FileNotFoundError(
            f"Factor values parquet not found at '{parquet_path}'. "
            "run stage1_factors first to generate factor parquet files."
        )

    df = pd.read_parquet(parquet_path, engine="pyarrow")
    _check_index_freq(df)
    return df


def load_factor_meta(symbol: str, manifests_dir: Path | None = None) -> dict:
    """Load sidecar meta.json for the given symbol.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).

    Raises
    ------
    FileNotFoundError
        If the meta.json does not exist.
    ValueError
        If schema_version != SCHEMA_VERSION.
    """
    sym_short = _symbol_short(symbol)
    mdir = manifests_dir if manifests_dir is not None else _default_manifests_dir()
    meta_path = mdir / f"factor_values_{sym_short}.meta.json"

    if not meta_path.exists():
        raise FileNotFoundError(
            f"Factor meta.json not found at '{meta_path}'. "
            "run stage1_factors first to generate factor parquet files."
        )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    version = meta.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"factor_values meta.json has schema_version={version!r}, "
            f"expected {SCHEMA_VERSION}.  Re-run stage1_factors to regenerate."
        )

    return meta


# ---------------------------------------------------------------------------
# Features store  (features_<sym>.parquet / features_<sym>.meta.json)
# ---------------------------------------------------------------------------


def dump_features(
    symbol: str,
    feature_series_dict: dict[str, pd.Series],
    manifests_dir: Path,
) -> Path:
    """Write feature time series to parquet + sidecar meta.json.

    Parameters
    ----------
    symbol:
        Symbol name, e.g. "eth" or "ETH-USDT-SWAP".
    feature_series_dict:
        Mapping from feature name → aligned pd.Series (DatetimeIndex, UTC).
    manifests_dir:
        Directory to write output into.

    Returns
    -------
    Path to the written parquet file.
    """
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "pyarrow is required to write features parquet files.  "
            "Install it with:  pip install pyarrow"
        ) from exc

    sym_short = _symbol_short(symbol)

    if not feature_series_dict:
        raise ValueError(f"feature_series_dict is empty for symbol '{symbol}'")

    df = pd.DataFrame(feature_series_dict)
    # Ensure index is UTC tz-aware (coerce if naive).
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # Cast all columns to float64.
    df = df.astype("float64")

    manifests_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = manifests_dir / f"features_{sym_short}.parquet"
    meta_path = manifests_dir / f"features_{sym_short}.meta.json"

    _atomic_to_parquet(df, parquet_path)

    feature_names = list(feature_series_dict.keys())
    n_rows = len(df)
    generated_at = datetime.now(timezone.utc).isoformat()
    index_start = df.index.min().isoformat() if n_rows > 0 else ""
    index_end = df.index.max().isoformat() if n_rows > 0 else ""

    meta = {
        "schema_version": FEATURES_SCHEMA_VERSION,
        "symbol": symbol,
        "generated_at": generated_at,
        "feature_names": feature_names,
        "index_start": index_start,
        "index_end": index_end,
        "n_rows": n_rows,
    }
    _atomic_write_text(meta_path, json.dumps(meta, indent=2))

    n_features = len(feature_names)
    print(f"[stage0a] {sym_short}: wrote features parquet ({n_features} features, {n_rows} rows)")

    return parquet_path


def _load_production_features(symbol: str, manifests_dir: Path | None = None) -> pd.DataFrame:
    """Load production features parquet for the given symbol.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).

    Raises
    ------
    FileNotFoundError
        If the parquet file does not exist.
    """
    sym_short = _symbol_short(symbol)
    # Coerce: callers (the foundry CLI) may pass a plain str manifests_dir, and
    # `str / str` is a TypeError. Path(Path) is a no-op, so this is safe for the
    # tmp_path callers too.
    mdir = Path(manifests_dir) if manifests_dir is not None else _default_manifests_dir()
    parquet_path = mdir / f"features_{sym_short}.parquet"

    if not parquet_path.exists():
        raise FileNotFoundError(
            f"Features parquet not found at '{parquet_path}'. Run stage0a_features first."
        )

    return pd.read_parquet(parquet_path, engine="pyarrow")


def load_features(symbol: str, manifests_dir: Path | None = None,
                  include_foundry: bool | None = None) -> pd.DataFrame:
    """Production features, optionally unioned with a per-run Foundry overlay.

    This is a READ path only: it never runs the sandbox and never computes a
    factor. The bridge materialises the overlay ONCE per pipeline run; the
    stages are separate subprocesses, so computing here would wake Docker once
    per stage and could yield different values to different stages.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).
    include_foundry:
        If None, defer to RESEARCH_INCLUDE_FOUNDRY env var (via foundry_enabled()).
        If True, union the overlay. If False, ignore it.

    Returns
    -------
    pd.DataFrame
        Production features, optionally merged with Foundry overlay columns
        (production columns always win in a name collision).
    """
    prod = _load_production_features(symbol, manifests_dir=manifests_dir)
    use = foundry_enabled() if include_foundry is None else include_foundry
    if not use:
        return prod
    d = _overlay_dir()
    if d is None:
        return prod
    path = d / f"foundry_overlay_{_symbol_short(symbol)}.parquet"
    if not path.exists():
        return prod
    overlay = pd.read_parquet(path)
    # production always wins a name clash — an overlay must never shadow a
    # feature the rest of the system (and the trader) treats as ground truth.
    overlay = overlay.drop(columns=[c for c in overlay.columns if c in prod.columns],
                           errors="ignore")
    if overlay.empty:
        return prod
    return prod.join(overlay.reindex(prod.index), how="left")


def load_manifest(symbol: str, manifests_dir: Path | None = None,
                  include_foundry: bool | None = None) -> dict:
    """stage1's factor_<sym>.json, optionally with the per-run Foundry entries
    appended. Foundry entries carry Foundry's own (pre-oos) statistics.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).
    include_foundry:
        If None, defer to RESEARCH_INCLUDE_FOUNDRY env var (via foundry_enabled()).
        If True, union the overlay. If False, ignore it.

    Returns
    -------
    dict
        Factor manifest with optional Foundry factors appended.
    """
    mdir = Path(manifests_dir) if manifests_dir is not None else _default_manifests_dir()
    sym = _symbol_short(symbol)
    manifest = json.loads((mdir / f"factor_{sym}.json").read_text(encoding="utf-8"))
    use = foundry_enabled() if include_foundry is None else include_foundry
    d = _overlay_dir()
    if not use or d is None:
        return manifest
    path = d / f"foundry_manifest_{sym}.json"
    if not path.exists():
        return manifest
    extra = json.loads(path.read_text(encoding="utf-8")).get("factors", [])
    existing = {f["name"] for f in manifest.get("factors", [])}
    manifest["factors"] = manifest.get("factors", []) + [
        f for f in extra if f["name"] not in existing]
    return manifest


def load_features_meta(symbol: str, manifests_dir: Path | None = None) -> dict:
    """Load sidecar meta.json for the given symbol's features store.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).

    Raises
    ------
    FileNotFoundError
        If the meta.json does not exist.
    ValueError
        If schema_version != FEATURES_SCHEMA_VERSION.
    """
    sym_short = _symbol_short(symbol)
    mdir = manifests_dir if manifests_dir is not None else _default_manifests_dir()
    meta_path = mdir / f"features_{sym_short}.meta.json"

    if not meta_path.exists():
        raise FileNotFoundError(
            f"Features meta.json not found at '{meta_path}'. Run stage0a_features first."
        )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    version = meta.get("schema_version")
    if version != FEATURES_SCHEMA_VERSION:
        raise ValueError(
            f"features meta.json has schema_version={version!r}, "
            f"expected {FEATURES_SCHEMA_VERSION}.  Re-run stage0a_features to regenerate."
        )

    return meta


# ---------------------------------------------------------------------------
# Evidence store  (evidence_<sym>.json)
# ---------------------------------------------------------------------------


def dump_evidence(
    symbol: str,
    evidence: list | dict,
    manifests_dir: Path,
) -> Path:
    """Write evidence to JSON.

    Parameters
    ----------
    symbol:
        Symbol name, e.g. "eth" or "ETH-USDT-SWAP".
    evidence:
        Either a list of EvidenceEntry-like dicts (bare list form), or a full
        evidence payload dict containing keys such as ``symbol``,
        ``generated_at``, ``caveat``, and ``evidence`` (list).  Both forms are
        serialised as-is via ``json.dumps`` — the caller controls the shape.
    manifests_dir:
        Directory to write output into.

    Returns
    -------
    Path to the written JSON file.
    """
    sym_short = _symbol_short(symbol)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    json_path = manifests_dir / f"evidence_{sym_short}.json"

    json_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return json_path


def load_evidence(symbol: str, manifests_dir: Path | None = None) -> list | dict:
    """Load evidence JSON for the given symbol.

    Parameters
    ----------
    symbol:
        Short ("eth") or full ticker ("ETH-USDT-SWAP").
    manifests_dir:
        Override the default manifests directory (useful for tests).

    Raises
    ------
    FileNotFoundError
        If the evidence JSON does not exist.
    """
    sym_short = _symbol_short(symbol)
    mdir = manifests_dir if manifests_dir is not None else _default_manifests_dir()
    json_path = mdir / f"evidence_{sym_short}.json"

    if not json_path.exists():
        raise FileNotFoundError(
            f"Evidence JSON not found at '{json_path}'. Run stage0a_features first."
        )

    return json.loads(json_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Append feature column helper
# ---------------------------------------------------------------------------


def append_feature_column(
    symbol: str,
    key: str,
    series: pd.Series,
    manifests_dir: Path | None = None,
    coverage_threshold: float = 0.5,
) -> None:
    """Append (or overwrite) a single feature column in the features parquet.

    Parameters
    ----------
    symbol:
        Symbol name, e.g. "eth" or "ETH-USDT-SWAP".
    key:
        Column name to add or overwrite.
    series:
        New data series.  Will be reindexed to match the existing parquet index.
    manifests_dir:
        Override the default manifests directory (useful for tests).
    coverage_threshold:
        Minimum fraction of non-NaN values required after reindex (default 0.5).

    Raises
    ------
    FileNotFoundError
        If the features parquet does not exist (load_features will raise).
    ValueError
        If the reindexed series is all-NaN or coverage is below threshold.
    """
    mdir = manifests_dir if manifests_dir is not None else _default_manifests_dir()

    # Load existing data (may raise FileNotFoundError).
    df = load_features(symbol, manifests_dir=mdir)

    # Reindex series to existing index — NaN fills for missing rows.
    aligned = series.reindex(df.index)

    # Validate: all-NaN check.
    if aligned.isna().all():
        raise ValueError(f"All-NaN series rejected for key '{key}'")

    # Validate: coverage check.
    coverage = aligned.notna().mean()
    if coverage < coverage_threshold:
        raise ValueError(
            f"Coverage too low for key '{key}': {coverage:.2%} < {coverage_threshold:.2%}"
        )

    # Overwrite or add column.
    df[key] = aligned.astype("float64")

    # Rewrite parquet.
    sym_short = _symbol_short(symbol)
    parquet_path = mdir / f"features_{sym_short}.parquet"
    _atomic_to_parquet(df, parquet_path)

    # Update meta: feature_names (deduplicated, preserve order), n_rows, generated_at.
    meta_path = mdir / f"features_{sym_short}.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {
            "schema_version": FEATURES_SCHEMA_VERSION,
            "symbol": symbol,
            "feature_names": [],
        }

    existing_names: list[str] = meta.get("feature_names", [])
    if key not in existing_names:
        existing_names.append(key)
    meta["feature_names"] = existing_names
    meta["n_rows"] = len(df)
    meta["generated_at"] = datetime.now(timezone.utc).isoformat()

    _atomic_write_text(meta_path, json.dumps(meta, indent=2))
