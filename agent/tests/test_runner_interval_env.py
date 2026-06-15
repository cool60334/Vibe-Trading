# agent/tests/test_runner_interval_env.py
"""Runner propagates the run's interval to RESEARCH_INTERVAL — run from agent/ (pytest tests/)."""
import os

from backtest.runner import _apply_run_interval_env


def test_apply_run_interval_env_sets_from_config(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    iv = _apply_run_interval_env({"interval": "30m"})
    assert iv == "30m"
    assert os.environ["RESEARCH_INTERVAL"] == "30m"


def test_apply_run_interval_env_overrides_stale_shell_value(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")  # stale shell value
    _apply_run_interval_env({"interval": "1H"})
    assert os.environ["RESEARCH_INTERVAL"] == "1H"  # config wins


def test_apply_run_interval_env_defaults_1H(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    _apply_run_interval_env({})
    assert os.environ["RESEARCH_INTERVAL"] == "1H"
