"""Tests for the trader Manager — control-file-driven spawn/stop.

The Manager runs in the standalone trader container. It watches
``runs/testnet/*/control.json`` and spawns / stops ``trader.loop``
subprocesses to match each file's ``desired_state``. Spawning is injected so
the scheduling logic is tested without real processes.
"""

import json
from pathlib import Path

import pytest

from trader.manager import Manager, build_trader_command, require_credentials


# ── Pure helpers (moved here from the server supervisor) ──────────────────────

def test_paper_mode_needs_no_credentials():
    require_credentials({}, "paper")


def test_testnet_mode_missing_keys_raises():
    with pytest.raises(EnvironmentError):
        require_credentials({}, "testnet")


def test_build_command_includes_mode_and_entrypoint():
    cmd = build_trader_command(
        "python", mode="paper", strategy_id="s", testnet_id="t",
        run_dir="d", symbol="BTC/USDT:USDT", interval="1H",
        repo_root="/repo", qty=0.001,
    )
    assert cmd[:3] == ["python", "-m", "trader.loop"]
    assert cmd[cmd.index("--mode") + 1] == "paper"
    assert cmd[cmd.index("--testnet-id") + 1] == "t"


# ── Manager scan logic (injected spawner) ─────────────────────────────────────

class _FakeHandle:
    def __init__(self):
        self.alive = True


def _write_control(repo_root: Path, testnet_id: str, desired: str, **extra) -> None:
    d = repo_root / "runs" / "testnet" / testnet_id
    d.mkdir(parents=True, exist_ok=True)
    body = {
        "desired_state": desired,
        "strategy_id": "strat",
        "run_dir": "/repo/runs/strat_oos",
        "symbol": "BTC/USDT:USDT",
        "interval": "1H",
        "qty": 0.001,
        "mode": "paper",
    }
    body.update(extra)
    (d / "control.json").write_text(json.dumps(body), encoding="utf-8")


def _make_manager(repo_root: Path):
    spawned: list[str] = []
    stopped: list[str] = []

    def spawner(tid, ctrl):
        spawned.append(tid)
        return _FakeHandle()

    def is_alive(h):
        return h.alive

    def stopper(h):
        h.alive = False
        stopped.append("x")

    m = Manager(repo_root, spawner=spawner, is_alive=is_alive, stopper=stopper)
    return m, spawned, stopped


def test_spawns_when_desired_running(tmp_path):
    _write_control(tmp_path, "t1", "running")
    m, spawned, _ = _make_manager(tmp_path)
    m.scan_once()
    assert spawned == ["t1"]


def test_does_not_double_spawn(tmp_path):
    _write_control(tmp_path, "t1", "running")
    m, spawned, _ = _make_manager(tmp_path)
    m.scan_once()
    m.scan_once()
    assert spawned == ["t1"]  # only once


def test_stops_when_desired_stopped(tmp_path):
    _write_control(tmp_path, "t1", "running")
    m, spawned, stopped = _make_manager(tmp_path)
    m.scan_once()
    _write_control(tmp_path, "t1", "stopped")
    m.scan_once()
    assert len(stopped) == 1


def test_stops_when_control_file_removed(tmp_path):
    _write_control(tmp_path, "t1", "running")
    m, spawned, stopped = _make_manager(tmp_path)
    m.scan_once()
    (tmp_path / "runs" / "testnet" / "t1" / "control.json").unlink()
    m.scan_once()
    assert len(stopped) == 1


def test_respawns_after_process_dies(tmp_path):
    _write_control(tmp_path, "t1", "running")
    m, spawned, _ = _make_manager(tmp_path)
    m.scan_once()
    # Simulate the loop process dying.
    m._procs["t1"].alive = False
    m.scan_once()
    assert spawned == ["t1", "t1"]  # respawned
