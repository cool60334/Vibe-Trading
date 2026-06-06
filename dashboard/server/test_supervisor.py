"""Tests for the control-file Supervisor — start/stop write control.json."""

import json
from pathlib import Path

import pytest

from supervisor import Supervisor

_RUN = dict(run_dir="/repo/runs/strat_oos", symbol="BTC/USDT:USDT")


def _control(tmp_path: Path, testnet_id: str) -> dict:
    path = tmp_path / "runs" / "testnet" / testnet_id / "control.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_start_writes_running_control(tmp_path):
    Supervisor(tmp_path).start("strat", "tn1", mode="paper", **_RUN)
    ctrl = _control(tmp_path, "tn1")
    assert ctrl["desired_state"] == "running"
    assert ctrl["mode"] == "paper"
    assert ctrl["strategy_id"] == "strat"
    assert ctrl["symbol"] == "BTC/USDT:USDT"


def test_start_paper_needs_no_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    Supervisor(tmp_path).start("s", "tn", mode="paper", **_RUN)  # must not raise


def test_start_testnet_requires_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    with pytest.raises(EnvironmentError):
        Supervisor(tmp_path).start("s", "tn", mode="testnet", **_RUN)


def test_stop_flips_to_stopped(tmp_path):
    sup = Supervisor(tmp_path)
    sup.start("s", "tn1", mode="paper", **_RUN)
    assert sup.stop("tn1") is True
    assert _control(tmp_path, "tn1")["desired_state"] == "stopped"


def test_stop_unknown_returns_false(tmp_path):
    assert Supervisor(tmp_path).stop("nope") is False


def test_is_running_and_status_reflect_control(tmp_path):
    sup = Supervisor(tmp_path)
    assert sup.is_running("tn1") is False
    sup.start("s", "tn1", mode="paper", **_RUN)
    assert sup.is_running("tn1") is True
    assert sup.status("tn1")["desired_state"] == "running"
    sup.stop("tn1")
    assert sup.is_running("tn1") is False
    assert sup.status("tn1")["running"] is False
