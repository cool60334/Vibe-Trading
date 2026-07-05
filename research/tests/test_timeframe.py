"""Tests for research/lib/timeframe.py — run from research/ (pytest tests/)."""
from pathlib import Path

import pytest

from lib.timeframe import SUPPORTED_INTERVALS, active_manifests_dir, bars_per_day, bars_per_hour


def test_bars_per_hour_known_intervals():
    assert bars_per_hour("15m") == 4
    assert bars_per_hour("30m") == 2
    assert bars_per_hour("1H") == 1


def test_bars_per_day_known_intervals():
    assert bars_per_day("15m") == 96
    assert bars_per_day("30m") == 48
    assert bars_per_day("1H") == 24


def test_supported_intervals_set():
    assert SUPPORTED_INTERVALS == frozenset({"15m", "30m", "1H"})


def test_unsupported_interval_raises():
    with pytest.raises(ValueError, match="unsupported interval"):
        bars_per_hour("4H")


def test_active_manifests_dir_default_is_root(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    p = active_manifests_dir()
    assert p.name == "manifests" and p.parent.name == "research"


def test_active_manifests_dir_1H_is_root(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    assert active_manifests_dir().name == "manifests"


def test_active_manifests_dir_subhour_is_namespaced(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    p = active_manifests_dir()
    assert p.name == "30m" and p.parent.name == "manifests"


def test_active_manifests_dir_invalid_raises(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "7m")
    with pytest.raises(ValueError, match="RESEARCH_INTERVAL"):
        active_manifests_dir()


def test_active_manifests_dir_env_override(monkeypatch, tmp_path):
    """RESEARCH_MANIFESTS_DIR overrides the repo manifests base (hermetic tests)."""
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    monkeypatch.setenv("RESEARCH_MANIFESTS_DIR", str(tmp_path))
    assert active_manifests_dir() == tmp_path


def test_active_manifests_dir_env_override_with_interval(monkeypatch, tmp_path):
    """Override base still applies the sub-hour interval namespace on top."""
    monkeypatch.setenv("RESEARCH_MANIFESTS_DIR", str(tmp_path))
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    assert active_manifests_dir() == tmp_path / "30m"
