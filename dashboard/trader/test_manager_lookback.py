"""Tests for control.json → --lookback passthrough in the trader manager."""

from trader.manager import build_trader_command

_COMMON = dict(
    mode="paper",
    strategy_id="s",
    testnet_id="t",
    run_dir="/repo/runs/x",
    symbol="ETH/USDT:USDT",
    interval="1H",
    repo_root="/repo",
    qty=0.01,
)


def test_build_command_omits_lookback_by_default():
    cmd = build_trader_command("python", **_COMMON)
    assert "--lookback" not in cmd


def test_build_command_includes_lookback_when_given():
    cmd = build_trader_command("python", lookback=2500, **_COMMON)
    i = cmd.index("--lookback")
    assert cmd[i + 1] == "2500"


def test_build_command_coerces_string_lookback():
    cmd = build_trader_command("python", lookback="2500", **_COMMON)
    i = cmd.index("--lookback")
    assert cmd[i + 1] == "2500"
