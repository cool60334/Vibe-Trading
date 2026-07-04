"""Tests for loop-side lookback resolution.

The live loop must never feed an engine fewer bars than its rolling windows
need: the effective lookback is max(CLI/default, YAML-derived requirement).
"""

from trader.loop import DEFAULT_LOOKBACK, build_parser, resolve_lookback


def test_default_lookback_covers_percentile_90d():
    # 90d percentile window = 2160 bars + buffer; the old default (200) is the
    # root cause of the silent all-NaN signal. Default must cover it.
    assert DEFAULT_LOOKBACK >= 90 * 24 + 16


def test_parser_default_uses_default_lookback():
    args = build_parser().parse_args([
        "--strategy-id", "s", "--testnet-id", "t",
        "--run-dir", "r", "--symbol", "ETH/USDT:USDT",
    ])
    assert args.lookback == DEFAULT_LOOKBACK


def test_resolve_prefers_required_when_larger():
    assert resolve_lookback(2200, 2600) == 2600


def test_resolve_keeps_cli_when_larger():
    assert resolve_lookback(3000, 2176) == 3000


def test_resolve_without_requirement_keeps_cli():
    assert resolve_lookback(2200, None) == 2200
