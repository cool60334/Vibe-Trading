"""Tests for supervisor mode wiring — credential gating and command build."""

import pytest

from supervisor import build_trader_command, require_credentials


def test_paper_mode_needs_no_credentials():
    # Must not raise even with an empty environment.
    require_credentials({}, "paper")


def test_testnet_mode_missing_keys_raises():
    with pytest.raises(EnvironmentError):
        require_credentials({}, "testnet")


def test_live_mode_missing_keys_raises():
    with pytest.raises(EnvironmentError):
        require_credentials({"BYBIT_API_KEY": "k"}, "live")


def test_testnet_mode_with_keys_ok():
    require_credentials({"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}, "testnet")


def test_build_command_includes_mode_and_loop_entrypoint():
    cmd = build_trader_command(
        "python",
        mode="paper",
        strategy_id="s",
        testnet_id="t",
        run_dir="d",
        symbol="BTC/USDT:USDT",
        interval="1H",
        repo_root="/repo",
        qty=0.001,
    )
    assert cmd[:3] == ["python", "-m", "trader.loop"]
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "paper"
    assert cmd[cmd.index("--symbol") + 1] == "BTC/USDT:USDT"
