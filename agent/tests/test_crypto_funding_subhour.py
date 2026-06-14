# agent/tests/test_crypto_funding_subhour.py
"""Funding settlement cadence on sub-hour bars — run from agent/ (pytest tests/)."""
import pandas as pd

from backtest.engines._market_hooks import calc_crypto_funding_fee
from backtest.models import Position


def _long_position(symbol="BTC-USDT", size=1.0, price=100.0):
    return Position(
        symbol, 1, price, pd.Timestamp("2025-01-01"), size, leverage=1.0
    )


def _charges_over_one_day(interval, freq):
    """Count non-zero funding charges for a held long over a full UTC day."""
    symbol = "BTC-USDT"
    positions = {symbol: _long_position(symbol)}
    applied, daily = set(), set()
    idx = pd.date_range("2025-01-01 00:00", "2025-01-01 23:59", freq=freq, tz="UTC")
    charges = []
    for ts in idx:
        bar = pd.Series({"close": 100.0})
        fee = calc_crypto_funding_fee(
            symbol, bar, ts, positions, 0.0001, applied, daily, interval=interval
        )
        if fee != 0.0:
            charges.append(ts)
    return charges


def test_15m_charges_exactly_three_settlements_per_day():
    charges = _charges_over_one_day("15m", "15min")
    assert [t.hour for t in charges] == [0, 8, 16]


def test_30m_charges_exactly_three_settlements_per_day():
    charges = _charges_over_one_day("30m", "30min")
    assert [t.hour for t in charges] == [0, 8, 16]


def test_1H_behaviour_preserved_with_default_interval():
    # Default interval keeps the legacy path: the call must still accept no
    # interval kwarg and charge at the settlement hours.
    symbol = "BTC-USDT"
    positions = {symbol: _long_position(symbol)}
    applied, daily = set(), set()
    ts = pd.Timestamp("2025-01-01 08:00", tz="UTC")
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(symbol, bar, ts, positions, 0.0001, applied, daily)
    assert fee == 100.0 * 0.0001 * 1  # notional * rate * direction
