# research/tests/test_massive_data.py
import pandas as pd
import pytest

from lib import massive_data


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _bar(ts_ms, c):
    return {"t": ts_ms, "o": c, "h": c, "l": c, "c": c, "v": 1.0, "n": 1}


def test_interval_to_mult_timespan():
    assert massive_data._interval_to_mult_timespan("1H") == (1, "hour")
    assert massive_data._interval_to_mult_timespan("30m") == (30, "minute")
    assert massive_data._interval_to_mult_timespan("15m") == (15, "minute")


def test_fetch_spot_bars_parses_results(monkeypatch):
    payload = {"results": [_bar(1_700_000_000_000, 100.0), _bar(1_700_003_600_000, 101.0)]}
    monkeypatch.setattr(massive_data.requests, "get", lambda *a, **k: FakeResp(payload))
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    df = massive_data.fetch_spot_bars("X:BTCUSD", days=2, interval="1H")
    assert df is not None
    assert list(df.columns) >= ["close"]
    assert df["close"].tolist() == [100.0, 101.0]
    assert str(df.index.tz) == "UTC"


def test_fetch_spot_bars_empty_results_returns_none(monkeypatch):
    monkeypatch.setattr(massive_data.requests, "get", lambda *a, **k: FakeResp({"results": []}))
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    assert massive_data.fetch_spot_bars("X:NOPEUSD", days=2, interval="1H") is None


def test_fetch_spot_bars_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert massive_data.fetch_spot_bars("X:BTCUSD", days=2, interval="1H") is None


def test_fetch_spot_bars_retries_on_429(monkeypatch):
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp({}, status=429)
        return FakeResp({"results": [_bar(1_700_000_000_000, 100.0)]})

    monkeypatch.setattr(massive_data.requests, "get", flaky_get)
    monkeypatch.setattr(massive_data.time, "sleep", lambda *_: None)
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    df = massive_data.fetch_spot_bars("X:BTCUSD", days=2, interval="1H", max_retries=2)
    assert df is not None and calls["n"] == 2


from pipeline.config import SymbolConfig


def test_symbolconfig_massive_usd_ticker():
    sym = SymbolConfig(name="btc", okx_swap="BTC-USDT-SWAP", ccxt_bybit="BTC/USDT:USDT")
    assert sym.massive_usd == "X:BTCUSD"
    sym2 = SymbolConfig(name="sol", okx_swap="SOL-USDT-SWAP", ccxt_bybit="SOL/USDT:USDT")
    assert sym2.massive_usd == "X:SOLUSD"
