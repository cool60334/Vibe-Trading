# research/tests/test_factor_io_interval.py
"""factor_io interval-aware default + index guard — run from research/ (pytest tests/)."""
import warnings

import pandas as pd
import pytest

from lib import factor_io


def test_default_dir_follows_research_interval(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    d = factor_io._default_manifests_dir()
    assert d.name == "30m" and d.parent.name == "manifests"


def test_default_dir_root_when_unset(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    assert factor_io._default_manifests_dir().name == "manifests"


def test_load_factor_values_reads_namespaced_dir(monkeypatch, tmp_path):
    # Write a 30m parquet under <base>/30m and confirm the zero-arg load finds it.
    monkeypatch.setattr(factor_io, "_MANIFESTS_BASE_OVERRIDE", tmp_path, raising=False)
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    sub = tmp_path / "30m"
    idx = pd.date_range("2025-01-01", periods=10, freq="30min", tz="UTC")
    df = pd.DataFrame({"mom_4": range(10)}, index=idx, dtype="float64")
    factor_io.dump_factor_values("eth", {"mom_4": df["mom_4"]}, sub)
    out = factor_io.load_factor_values("eth")  # no manifests_dir -> default -> 30m
    assert list(out.columns) == ["mom_4"] and len(out) == 10


def test_load_factor_values_raises_on_index_freq_mismatch(monkeypatch, tmp_path):
    # A mismatched-interval parquet must hard-fail (not warn): a 1H parquet in a
    # 30m run would otherwise silently produce a fake backtest. Deterministic
    # regardless of the process warning filter.
    monkeypatch.setattr(factor_io, "_MANIFESTS_BASE_OVERRIDE", tmp_path, raising=False)
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    sub = tmp_path / "30m"
    idx = pd.date_range("2025-01-01", periods=10, freq="1h", tz="UTC")  # 1H data, 30m expected
    factor_io.dump_factor_values("eth", {"mom_4": pd.Series(range(10), index=idx, dtype="float64")}, sub)
    with pytest.raises(ValueError, match="index spacing"):
        factor_io.load_factor_values("eth")
