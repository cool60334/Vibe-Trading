"""Tests for loop._write_status — trading mode is persisted to status JSON."""

import json

from trader.loop import _write_status

_COMMON = dict(
    strategy_id="s",
    testnet_id="t",
    symbol="BTC/USDT:USDT",
    live_status="running",
    equity=100.0,
    open_positions=0,
    trades=0,
    sharpe=None,
    max_drawdown=None,
    ks_triggered=False,
    ks_triggered_at=None,
    ks_reason=None,
    pause_dd=0.05,
    terminate_dd=0.07,
    alerts=[],
    started_at="2024-01-01T00:00:00Z",
)


def _read_status(out_dir):
    return json.loads((out_dir / "testnet_status.json").read_text(encoding="utf-8"))


def test_write_status_includes_mode(tmp_path):
    _write_status(out_dir=tmp_path, mode="paper", **_COMMON)
    assert _read_status(tmp_path)["mode"] == "paper"


def test_write_status_mode_can_be_live(tmp_path):
    _write_status(out_dir=tmp_path, mode="live", **_COMMON)
    assert _read_status(tmp_path)["mode"] == "live"


def test_write_status_includes_monitor_block(tmp_path):
    monitor = {"expected_fills_30d": 5.9, "actual_fills_30d": 0,
               "silent": False, "regime_age_days": 1.2}
    _write_status(out_dir=tmp_path, mode="paper", monitor=monitor, **_COMMON)
    assert _read_status(tmp_path)["monitor"] == monitor


def test_write_status_monitor_defaults_to_none(tmp_path):
    _write_status(out_dir=tmp_path, mode="paper", **_COMMON)
    assert _read_status(tmp_path)["monitor"] is None
