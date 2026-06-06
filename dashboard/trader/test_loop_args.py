"""Tests for the trader loop CLI parser — trading mode selection."""

from trader.loop import build_parser

_REQUIRED = [
    "--strategy-id", "s",
    "--testnet-id", "t",
    "--run-dir", "d",
    "--symbol", "BTC/USDT:USDT",
]


def test_parser_mode_defaults_to_paper(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    args = build_parser().parse_args(_REQUIRED)
    assert args.mode == "paper"


def test_parser_mode_defaults_to_env_when_set(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "testnet")
    args = build_parser().parse_args(_REQUIRED)
    assert args.mode == "testnet"


def test_parser_mode_explicit_flag_overrides(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    args = build_parser().parse_args(_REQUIRED + ["--mode", "live"])
    assert args.mode == "live"
