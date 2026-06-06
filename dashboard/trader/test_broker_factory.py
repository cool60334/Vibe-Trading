"""Tests for make_broker — mode selection (paper / testnet / live)."""

import pytest

from trader.broker import Broker, make_broker
from trader.paper_broker import PaperBroker


def test_make_broker_paper_needs_no_keys(monkeypatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    b = make_broker("paper")
    assert isinstance(b, PaperBroker)


def test_make_broker_paper_reads_env_knobs(monkeypatch):
    monkeypatch.setenv("PAPER_EQUITY", "25000")
    monkeypatch.setenv("PAPER_TAKER_FEE", "0.001")
    monkeypatch.setenv("PAPER_SLIPPAGE_BPS", "12")
    b = make_broker("paper")
    assert b.cash == 25000.0
    assert b.taker_fee == 0.001
    assert b.slippage == pytest.approx(0.0012)


def test_make_broker_default_mode_is_paper(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    b = make_broker()
    assert isinstance(b, PaperBroker)


def test_make_broker_testnet_requires_keys(monkeypatch):
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    with pytest.raises(EnvironmentError):
        make_broker("testnet")


def test_make_broker_testnet_returns_live_broker_with_keys(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    b = make_broker("testnet")
    assert isinstance(b, Broker)


def test_make_broker_unknown_mode_raises():
    with pytest.raises(ValueError):
        make_broker("bogus")
