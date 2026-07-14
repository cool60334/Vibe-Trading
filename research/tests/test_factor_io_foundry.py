import json
import numpy as np
import pandas as pd
import pytest
from research.lib.factor_io import foundry_enabled, load_features


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
