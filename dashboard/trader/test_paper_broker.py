"""Tests for PaperBroker — mainnet-data dry-run with local virtual account.

The exchange is injected as a fake so the accounting math is tested in
isolation (no network). Funding accrual is driven by an injectable clock.
"""

from datetime import datetime, timezone

import pytest

from trader.paper_broker import PaperBroker


class FakeExchange:
    """Minimal ccxt-like stub: public market data only."""

    def __init__(self, last=100.0, bid=100.0, ask=100.0, funding=0.0):
        self._last = last
        self._bid = bid
        self._ask = ask
        self._funding = funding

    def fetch_ticker(self, symbol):
        return {"last": self._last, "bid": self._bid, "ask": self._ask}

    def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[self._bid, 1.0]], "asks": [[self._ask, 1.0]]}

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": self._funding}


def test_open_long_equity_reflects_unrealized_minus_open_fee():
    # buy fills at ask=100; mark (last)=110 → +20 unrealized on size 2.
    ex = FakeExchange(last=110.0, bid=99.0, ask=100.0)
    b = PaperBroker(ex, equity=10000.0, taker_fee=0.001, slippage_bps=0.0)

    b.place_market_order("BTC/USDT:USDT", "buy", 2.0)

    # open fee = 100 * 2 * 0.001 = 0.2 → cash 9999.8
    # unrealized = (110 - 100) * 2 = 20
    assert b.get_equity() == pytest.approx(10019.8)


def test_close_long_realizes_pnl_and_charges_second_fee():
    ex = FakeExchange(last=100.0, bid=120.0, ask=100.0)
    b = PaperBroker(ex, equity=10000.0, taker_fee=0.001, slippage_bps=0.0)

    b.place_market_order("BTC/USDT:USDT", "buy", 2.0)  # fill 100, fee 0.2
    b.close_position("BTC/USDT:USDT")  # sell fill 120, fee 0.24, realized +40

    assert b.get_position("BTC/USDT:USDT") is None
    # 9999.8 + 40 - 0.24 = 10039.56
    assert b.get_equity() == pytest.approx(10039.56)


def test_slippage_buy_pays_above_ask_sell_below_bid():
    ex = FakeExchange(last=100.0, bid=100.0, ask=100.0)
    b = PaperBroker(ex, equity=10000.0, taker_fee=0.0, slippage_bps=10.0)  # 10bps = 0.001

    open_order = b.place_market_order("X/USDT:USDT", "buy", 1.0)
    assert open_order["average"] == pytest.approx(100.0 * 1.001)  # 100.1

    close_order = b.close_position("X/USDT:USDT")
    assert close_order["average"] == pytest.approx(100.0 * 0.999)  # 99.9


def test_funding_charges_long_once_per_boundary_not_within_period():
    ex = FakeExchange(last=100.0, bid=100.0, ask=100.0, funding=0.01)
    clock = {"t": datetime(2026, 6, 6, 7, 0, tzinfo=timezone.utc)}
    b = PaperBroker(
        ex, equity=10000.0, taker_fee=0.0, slippage_bps=0.0,
        clock=lambda: clock["t"],
    )

    b.place_market_order("X/USDT:USDT", "buy", 2.0)  # open 07:00, notional 200

    # Cross the 08:00 funding boundary once.
    clock["t"] = datetime(2026, 6, 6, 8, 30, tzinfo=timezone.utc)
    # funding = 0.01 * 200 * (+1 long) = 2 → cash -= 2
    assert b.get_equity() == pytest.approx(9998.0)

    # Same funding period (no new boundary) → no extra charge.
    clock["t"] = datetime(2026, 6, 6, 8, 45, tzinfo=timezone.utc)
    assert b.get_equity() == pytest.approx(9998.0)


def test_funding_pays_short_when_rate_positive():
    ex = FakeExchange(last=100.0, bid=100.0, ask=100.0, funding=0.01)
    clock = {"t": datetime(2026, 6, 6, 7, 0, tzinfo=timezone.utc)}
    b = PaperBroker(
        ex, equity=10000.0, taker_fee=0.0, slippage_bps=0.0,
        clock=lambda: clock["t"],
    )

    b.place_market_order("X/USDT:USDT", "sell", 2.0)  # short, notional 200

    clock["t"] = datetime(2026, 6, 6, 8, 30, tzinfo=timezone.utc)
    # short receives funding when rate positive: cash += 2
    assert b.get_equity() == pytest.approx(10002.0)


def test_get_position_shape_matches_live_interface():
    ex = FakeExchange(last=50.0, bid=50.0, ask=50.0)
    b = PaperBroker(ex, equity=10000.0, taker_fee=0.00055, slippage_bps=5.0)

    assert b.get_position("X/USDT:USDT") is None

    b.place_market_order("X/USDT:USDT", "sell", 3.0)  # short, fill below bid
    pos = b.get_position("X/USDT:USDT")
    assert set(pos.keys()) == {"side", "size", "entry_price"}
    assert pos["side"] == "short"
    assert pos["size"] == 3.0
    assert pos["entry_price"] == pytest.approx(50.0 * (1 - 0.0005))  # 49.975
