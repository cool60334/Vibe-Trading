"""Tests for trader.monitor — expected-vs-actual trade telemetry."""
import csv
import json
from datetime import datetime, timedelta, timezone

from trader.monitor import (
    actual_fills_last_30d,
    expected_fills_per_30d,
    regime_age_days,
    silence_alert_needed,
)

_NOW = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)


def _make_repo(tmp_path, yaml_text):
    (tmp_path / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "research" / "strategy_runs.json").write_text(json.dumps({
        "eth_s5_half_size": {"spec_yaml": "research/strategies/s.yaml"}
    }), encoding="utf-8")
    y = tmp_path / "research" / "strategies" / "s.yaml"
    y.parent.mkdir(parents=True, exist_ok=True)
    y.write_text(yaml_text, encoding="utf-8")
    return tmp_path


def test_expected_fills_from_yaml_estimate(tmp_path):
    repo = _make_repo(tmp_path, "expected_behavior:\n  trades_per_year_estimate: 36\n")
    # 36 trades/yr ~= 2.958 trades/30d ~= 5.916 fills/30d
    val = expected_fills_per_30d(repo, "eth_s5_half_size")
    assert val is not None and abs(val - 36 / 365.25 * 30 * 2) < 1e-9


def test_expected_fills_missing_estimate_is_none(tmp_path):
    repo = _make_repo(tmp_path, "name: x\n")
    assert expected_fills_per_30d(repo, "eth_s5_half_size") is None


def test_expected_fills_missing_strategy_is_none(tmp_path):
    repo = _make_repo(tmp_path, "expected_behavior:\n  trades_per_year_estimate: 36\n")
    assert expected_fills_per_30d(repo, "nope") is None


def _write_trades(out_dir, ts_list):
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "side", "qty", "price"])
        w.writeheader()
        for ts in ts_list:
            w.writerow({"timestamp": ts, "symbol": "E", "side": "buy", "qty": 1, "price": 1})


def test_actual_fills_counts_only_last_30d(tmp_path):
    _write_trades(tmp_path, [
        (_NOW - timedelta(days=40)).isoformat(),
        (_NOW - timedelta(days=10)).isoformat(),
        (_NOW - timedelta(days=1)).isoformat(),
    ])
    assert actual_fills_last_30d(tmp_path, _NOW) == 2


def test_actual_fills_missing_file_is_zero(tmp_path):
    assert actual_fills_last_30d(tmp_path, _NOW) == 0


def test_silence_alert_thresholds():
    assert silence_alert_needed(6.0, 0) is True     # >=3 expected trades, none seen
    assert silence_alert_needed(6.0, 1) is False    # any fill clears it
    assert silence_alert_needed(4.0, 0) is False    # too low-freq to judge
    assert silence_alert_needed(None, 0) is False   # unknown expectancy: fail-open


def test_regime_age_days_reads_last_breakdown_date(tmp_path):
    (tmp_path / "regime_eth.json").write_text(json.dumps({
        "breakdown": [{"date": "2026-07-01", "regime": "bear"},
                      {"date": "2026-07-02", "regime": "bear"}]
    }), encoding="utf-8")
    age = regime_age_days(tmp_path, "ETH/USDT:USDT", _NOW)
    assert age is not None and abs(age - 2.5) < 0.01  # 07-02 00:00 -> 07-04 12:00


def test_regime_age_missing_file_is_none(tmp_path):
    assert regime_age_days(tmp_path, "ETH/USDT:USDT", _NOW) is None
