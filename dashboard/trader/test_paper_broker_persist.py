"""Tests for PaperBroker state persistence — survive process restart.

A paper account that resets to PAPER_EQUITY on every restart makes a long
dry-run discontinuous. With a state_path the broker reloads cash / position /
funding cursor instead of starting fresh.
"""

from datetime import datetime, timezone

import pytest

from trader.paper_broker import PaperBroker


class FakeExchange:
    def __init__(self, last=100.0, bid=100.0, ask=100.0, funding=0.0):
        self._last, self._bid, self._ask, self._funding = last, bid, ask, funding

    def fetch_ticker(self, symbol):
        return {"last": self._last, "bid": self._bid, "ask": self._ask}

    def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[self._bid, 1.0]], "asks": [[self._ask, 1.0]]}

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": self._funding}


def test_fresh_state_path_starts_at_equity(tmp_path):
    ex = FakeExchange()
    b = PaperBroker(ex, equity=12345.0, state_path=tmp_path / "none.json")
    assert b.get_equity() == 12345.0


def test_restart_resumes_cash_and_position(tmp_path):
    ex = FakeExchange(last=100.0, bid=100.0, ask=100.0)
    sp = tmp_path / "paper_state.json"

    b1 = PaperBroker(ex, equity=10000.0, taker_fee=0.0, slippage_bps=0.0, state_path=sp)
    b1.place_market_order("X/USDT:USDT", "buy", 2.0)  # long 2 @ 100, no fee

    # New instance, same state file — equity arg must be ignored in favour of saved state.
    b2 = PaperBroker(ex, equity=999999.0, taker_fee=0.0, slippage_bps=0.0, state_path=sp)
    assert b2.cash == 10000.0
    assert b2.get_position("X/USDT:USDT") == {
        "side": "long", "size": 2.0, "entry_price": 100.0,
    }


def test_restart_does_not_recharge_already_paid_funding(tmp_path):
    ex = FakeExchange(last=100.0, bid=100.0, ask=100.0, funding=0.01)
    sp = tmp_path / "paper_state.json"
    clock = {"t": datetime(2026, 6, 6, 7, 0, tzinfo=timezone.utc)}

    b1 = PaperBroker(ex, equity=10000.0, taker_fee=0.0, slippage_bps=0.0,
                     state_path=sp, clock=lambda: clock["t"])
    b1.place_market_order("X/USDT:USDT", "buy", 2.0)  # notional 200
    clock["t"] = datetime(2026, 6, 6, 8, 30, tzinfo=timezone.utc)
    assert b1.get_equity() == pytest.approx(9998.0)  # charged 08:00 funding once

    # Restart at the same instant — must not re-charge the 08:00 boundary.
    b2 = PaperBroker(ex, equity=10000.0, taker_fee=0.0, slippage_bps=0.0,
                     state_path=sp, clock=lambda: clock["t"])
    assert b2.cash == pytest.approx(9998.0)
    clock["t"] = datetime(2026, 6, 6, 8, 45, tzinfo=timezone.utc)  # same period
    assert b2.get_equity() == pytest.approx(9998.0)
