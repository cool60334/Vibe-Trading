import json
import numpy as np
import pandas as pd
import pytest
from research.lib.factor_io import (
    foundry_enabled,
    load_features,
    load_factor_values,
    load_manifest,
)


@pytest.fixture
def prod(tmp_path):
    idx = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame({"funding_z": np.arange(10.0)}, index=idx)
    df.to_parquet(tmp_path / "features_eth.parquet")
    return tmp_path, idx


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("0", False), ("", False), ("false", False), (None, False),
])
def test_foundry_enabled_requires_the_exact_string_1(monkeypatch, val, expected):
    # bool(os.getenv(...)) would make "0" and "false" TRUE — i.e. on by default.
    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    if val is not None:
        monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", val)
    assert foundry_enabled() is expected


def test_load_features_default_ignores_the_overlay(prod, tmp_path, monkeypatch):
    mdir, idx = prod
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_x": np.ones(10)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_features("eth", manifests_dir=mdir)

    assert list(out.columns) == ["funding_z"]      # production path untouched


def test_load_features_unions_the_overlay_when_enabled(prod, tmp_path, monkeypatch):
    mdir, idx = prod
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_x": np.ones(10)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_features("eth", manifests_dir=mdir)

    assert set(out.columns) == {"funding_z", "foundry_x"}
    assert out.index.equals(idx)


def test_overlay_never_overwrites_a_production_column(prod, tmp_path, monkeypatch):
    mdir, idx = prod
    ov = tmp_path / "ov"; ov.mkdir()
    # a hostile/buggy overlay carrying a production name must not shadow it
    pd.DataFrame({"funding_z": np.full(10, -999.0)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_features("eth", manifests_dir=mdir)

    assert (out["funding_z"].to_numpy() == np.arange(10.0)).all()   # production wins


# ─────────────────────────────────────────────────────────────────────────────
# Tests for load_manifest() with Foundry union/dedup/default paths
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def base_manifest(tmp_path):
    """Create a base factor_eth.json manifest with two factors."""
    mdir = tmp_path / "manifests"; mdir.mkdir()
    base = {
        "factors": [
            {"name": "funding_z", "ic_by_horizon": {24: 0.05}, "ir": 0.12},
            {"name": "basis_rel", "ic_by_horizon": {24: 0.03}, "ir": 0.08},
        ]
    }
    (mdir / "factor_eth.json").write_text(json.dumps(base, indent=2), encoding="utf-8")
    return mdir


def test_load_manifest_default_ignores_overlay_when_flag_unset(base_manifest, tmp_path, monkeypatch):
    """When RESEARCH_INCLUDE_FOUNDRY is unset, load_manifest returns base only."""
    ov = tmp_path / "ov"; ov.mkdir()
    foundry_manifest = {
        "factors": [
            {"name": "foundry_factor1", "ic_by_horizon": {24: 0.10}, "ir": 0.20},
        ]
    }
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest, include_foundry=False)

    # Only base factors, no Foundry entries
    assert len(result["factors"]) == 2
    assert [f["name"] for f in result["factors"]] == ["funding_z", "basis_rel"]


def test_load_manifest_default_ignores_overlay_when_env_not_set(base_manifest, tmp_path, monkeypatch):
    """When env var is not set and include_foundry=None, return base only."""
    ov = tmp_path / "ov"; ov.mkdir()
    foundry_manifest = {
        "factors": [
            {"name": "foundry_factor1", "ic_by_horizon": {24: 0.10}, "ir": 0.20},
        ]
    }
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest)  # include_foundry=None

    # Only base factors (env var defaults to False when unset)
    assert len(result["factors"]) == 2
    assert [f["name"] for f in result["factors"]] == ["funding_z", "basis_rel"]


def test_load_manifest_unions_new_factors_when_enabled(base_manifest, tmp_path, monkeypatch):
    """When enabled, load_manifest appends NEW (non-duplicate) Foundry factors."""
    ov = tmp_path / "ov"; ov.mkdir()
    foundry_manifest = {
        "factors": [
            {"name": "foundry_factor1", "ic_by_horizon": {24: 0.10}, "ir": 0.20},
            {"name": "foundry_factor2", "ic_by_horizon": {24: 0.08}, "ir": 0.15},
        ]
    }
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest)

    # Base factors + 2 new Foundry factors
    assert len(result["factors"]) == 4
    names = [f["name"] for f in result["factors"]]
    assert names == ["funding_z", "basis_rel", "foundry_factor1", "foundry_factor2"]


def test_load_manifest_deduplicates_by_name(base_manifest, tmp_path, monkeypatch):
    """When a Foundry factor's name already exists in base, it is NOT duplicated."""
    ov = tmp_path / "ov"; ov.mkdir()
    # Foundry manifest carries the same name as an existing base factor
    foundry_manifest = {
        "factors": [
            {"name": "funding_z", "ic_by_horizon": {24: 0.99}, "ir": 0.99},  # different values
            {"name": "foundry_new", "ic_by_horizon": {24: 0.10}, "ir": 0.20},
        ]
    }
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest)

    # Should have 3 factors: base's funding_z, base's basis_rel, and only the new foundry_new
    # The duplicate "funding_z" from Foundry must NOT overwrite the base version
    assert len(result["factors"]) == 3
    names = [f["name"] for f in result["factors"]]
    assert names == ["funding_z", "basis_rel", "foundry_new"]

    # Verify it's the base version of funding_z (with original IR 0.12), not Foundry's (0.99)
    funding_z_entry = next(f for f in result["factors"] if f["name"] == "funding_z")
    assert funding_z_entry["ir"] == 0.12  # base version, not Foundry's 0.99


def test_load_manifest_missing_overlay_file_returns_base_unchanged(base_manifest, tmp_path, monkeypatch):
    """When overlay dir is set but foundry_manifest_<sym>.json doesn't exist, return base unchanged."""
    ov = tmp_path / "ov"; ov.mkdir()
    # Intentionally do NOT create foundry_manifest_eth.json in the overlay dir

    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest)

    # Should return base unchanged (no crash)
    assert len(result["factors"]) == 2
    assert [f["name"] for f in result["factors"]] == ["funding_z", "basis_rel"]


def test_load_manifest_no_overlay_dir_env_returns_base_unchanged(base_manifest, monkeypatch):
    """When RESEARCH_FOUNDRY_OVERLAY_DIR is not set but include_foundry=True, return base unchanged."""
    monkeypatch.delenv("RESEARCH_FOUNDRY_OVERLAY_DIR", raising=False)
    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)

    result = load_manifest("eth", manifests_dir=base_manifest, include_foundry=True)

    # Should return base unchanged (overlay dir is None)
    assert len(result["factors"]) == 2
    assert [f["name"] for f in result["factors"]] == ["funding_z", "basis_rel"]


def test_load_manifest_normalizes_full_ticker_symbol(base_manifest, tmp_path, monkeypatch):
    """load_manifest accepts a full ticker ('ETH-USDT-SWAP') and normalizes it via
    _symbol_short to resolve the same eth-scoped files as the short form."""
    ov = tmp_path / "ov"; ov.mkdir()
    foundry_manifest = {
        "factors": [
            {"name": "foundry_factor1", "ic_by_horizon": {24: 0.10}, "ir": 0.20},
        ]
    }
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    # Pass the full ticker form; _symbol_short must normalize it down to "eth"
    # to find factor_eth.json / foundry_manifest_eth.json.
    result = load_manifest("ETH-USDT-SWAP", manifests_dir=base_manifest)

    assert len(result["factors"]) == 3
    names = [f["name"] for f in result["factors"]]
    assert "foundry_factor1" in names


def test_load_manifest_base_manifest_with_no_factors_key(base_manifest, tmp_path, monkeypatch):
    """load_manifest handles base manifest without 'factors' key gracefully."""
    # Overwrite the base manifest to have no 'factors' key
    (base_manifest / "factor_eth.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8")

    ov = tmp_path / "ov"; ov.mkdir()
    foundry_manifest = {
        "factors": [
            {"name": "foundry_factor1", "ic_by_horizon": {24: 0.10}, "ir": 0.20},
        ]
    }
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest)

    # Should have only the Foundry factor (base has no factors to contribute)
    assert len(result["factors"]) == 1
    assert result["factors"][0]["name"] == "foundry_factor1"


def test_load_manifest_foundry_manifest_with_no_factors_key(base_manifest, tmp_path, monkeypatch):
    """load_manifest handles Foundry manifest without 'factors' key gracefully."""
    ov = tmp_path / "ov"; ov.mkdir()
    # Foundry manifest with no 'factors' key
    foundry_manifest = {"version": 1}
    (ov / "foundry_manifest_eth.json").write_text(
        json.dumps(foundry_manifest, indent=2), encoding="utf-8")

    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    result = load_manifest("eth", manifests_dir=base_manifest)

    # Should return base unchanged (Foundry factors list is empty)
    assert len(result["factors"]) == 2
    assert [f["name"] for f in result["factors"]] == ["funding_z", "basis_rel"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests for load_factor_values() with the same Foundry overlay union support
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def prod_factor_values(tmp_path):
    idx = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame({"funding_z": np.arange(10.0)}, index=idx)
    df.to_parquet(tmp_path / "factor_values_eth.parquet")
    return tmp_path, idx


def test_load_factor_values_default_ignores_the_overlay(prod_factor_values, tmp_path, monkeypatch):
    mdir, idx = prod_factor_values
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_x": np.ones(10)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_factor_values("eth", manifests_dir=mdir)

    assert list(out.columns) == ["funding_z"]      # production path untouched


def test_load_factor_values_unions_the_overlay_when_enabled(prod_factor_values, tmp_path, monkeypatch):
    mdir, idx = prod_factor_values
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_x": np.ones(10)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_factor_values("eth", manifests_dir=mdir)

    assert set(out.columns) == {"funding_z", "foundry_x"}
    assert out.index.equals(idx)


def test_load_factor_values_overlay_never_overwrites_a_production_column(
        prod_factor_values, tmp_path, monkeypatch):
    mdir, idx = prod_factor_values
    ov = tmp_path / "ov"; ov.mkdir()
    # a hostile/buggy overlay carrying a production name must not shadow it
    pd.DataFrame({"funding_z": np.full(10, -999.0)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_factor_values("eth", manifests_dir=mdir)

    assert (out["funding_z"].to_numpy() == np.arange(10.0)).all()   # production wins


def test_load_factor_values_missing_overlay_file_returns_prod_unchanged(
        prod_factor_values, tmp_path, monkeypatch):
    mdir, idx = prod_factor_values
    ov = tmp_path / "ov"; ov.mkdir()
    # Intentionally do NOT create foundry_overlay_eth.parquet in the overlay dir
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_factor_values("eth", manifests_dir=mdir)

    assert list(out.columns) == ["funding_z"]


def test_load_factor_values_no_overlay_dir_env_returns_prod_unchanged(
        prod_factor_values, monkeypatch):
    mdir, idx = prod_factor_values
    monkeypatch.delenv("RESEARCH_FOUNDRY_OVERLAY_DIR", raising=False)
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")

    out = load_factor_values("eth", manifests_dir=mdir)

    assert list(out.columns) == ["funding_z"]
