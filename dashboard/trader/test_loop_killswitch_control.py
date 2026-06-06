"""Tests for env-configurable killswitch thresholds + terminate→stop control flip.

Two behaviours guarding a long paper dry-run:
  * kill_thresholds() reads KILL_PAUSE_DD / KILL_TERMINATE_DD so a strategy whose
    expected drawdown exceeds the 7% default does not auto-terminate.
  * _flip_control_stopped() flips the run's control.json to desired_state=stopped
    when the kill switch terminates, so the manager does NOT respawn the loop
    into an immediate re-terminate loop.
"""

import json

from trader.loop import kill_thresholds, _flip_control_stopped


# ── kill_thresholds ───────────────────────────────────────────────────────────

def test_kill_thresholds_defaults(monkeypatch):
    monkeypatch.delenv("KILL_PAUSE_DD", raising=False)
    monkeypatch.delenv("KILL_TERMINATE_DD", raising=False)
    assert kill_thresholds() == (0.05, 0.07)


def test_kill_thresholds_env_override(monkeypatch):
    monkeypatch.setenv("KILL_PAUSE_DD", "0.08")
    monkeypatch.setenv("KILL_TERMINATE_DD", "0.12")
    assert kill_thresholds() == (0.08, 0.12)


def test_kill_thresholds_explicit_env_arg():
    assert kill_thresholds({"KILL_TERMINATE_DD": "0.15"}) == (0.05, 0.15)


# ── _flip_control_stopped ─────────────────────────────────────────────────────

def test_flip_control_stopped_flips_state(tmp_path):
    ctrl = {
        "desired_state": "running",
        "strategy_id": "eth_s5_half_size",
        "run_dir": "runs/eth_s5_half_size_oos",
        "symbol": "ETH/USDT:USDT",
        "mode": "paper",
    }
    (tmp_path / "control.json").write_text(json.dumps(ctrl), encoding="utf-8")

    assert _flip_control_stopped(tmp_path) is True

    out = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert out["desired_state"] == "stopped"
    # other launch params preserved so a manual restart keeps the same config
    assert out["strategy_id"] == "eth_s5_half_size"
    assert out["symbol"] == "ETH/USDT:USDT"
    assert "updated_at" in out


def test_flip_control_stopped_missing_file_is_safe(tmp_path):
    assert _flip_control_stopped(tmp_path) is False
