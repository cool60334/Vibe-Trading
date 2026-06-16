# research/tests/test_orderflow.py
import polars as pl
import pytest

from lib import orderflow


def _trades(rows):
    """rows: list of (transact_time_ms, price, quantity, is_buyer_maker)."""
    return pl.DataFrame(
        {
            "transact_time": [r[0] for r in rows],
            "price": [r[1] for r in rows],
            "quantity": [r[2] for r in rows],
            "is_buyer_maker": [r[3] for r in rows],
        }
    )


# 2025-01-01T00:00:00Z = 1735689600000 ms
T0 = 1735689600000
MIN = 60_000
HALF_HOUR = 30 * MIN


def test_buy_sell_split_by_is_buyer_maker():
    # is_buyer_maker=False -> aggressive BUY; True -> aggressive SELL
    df = _trades([
        (T0, 100.0, 2.0, False),   # buy 2
        (T0 + MIN, 100.0, 1.0, True),  # sell 1
    ])
    out = orderflow.aggregate(df, "30m")
    row = out.row(0, named=True)
    assert row["buy_vol"] == pytest.approx(2.0)
    assert row["sell_vol"] == pytest.approx(1.0)
    assert row["buy_count"] == 1
    assert row["sell_count"] == 1
    assert row["total_vol"] == pytest.approx(3.0)


def test_bucket_is_left_closed_right_open():
    # a trade exactly at T0+30m must fall in the SECOND bar, not the first
    df = _trades([
        (T0, 100.0, 1.0, False),
        (T0 + HALF_HOUR, 100.0, 5.0, False),  # boundary -> next bar
    ])
    out = orderflow.aggregate(df, "30m").sort("ts")
    assert out.height == 2
    assert out.row(0, named=True)["buy_vol"] == pytest.approx(1.0)
    assert out.row(1, named=True)["buy_vol"] == pytest.approx(5.0)


def test_future_trades_do_not_change_past_bar():
    base = _trades([(T0, 100.0, 1.0, False)])
    extended = _trades([
        (T0, 100.0, 1.0, False),
        (T0 + HALF_HOUR, 100.0, 9.0, True),  # future bar
    ])
    b0 = orderflow.aggregate(base, "30m").sort("ts").row(0, named=True)
    e0 = orderflow.aggregate(extended, "30m").sort("ts").row(0, named=True)
    assert b0 == e0  # first bar identical regardless of later data


def test_duplicate_agg_trade_ids_dropped():
    df = pl.DataFrame({
        "agg_trade_id": [1, 1, 2],
        "transact_time": [T0, T0, T0 + MIN],
        "price": [100.0, 100.0, 100.0],
        "quantity": [1.0, 1.0, 2.0],
        "is_buyer_maker": [False, False, False],
    })
    out = orderflow.aggregate(df, "30m")
    assert out.row(0, named=True)["buy_vol"] == pytest.approx(3.0)  # 1 + 2, dup ignored


def test_empty_bar_has_no_zero_division_and_open_close_set():
    df = _trades([(T0, 100.0, 2.0, False), (T0 + MIN, 110.0, 2.0, True)])
    out = orderflow.aggregate(df, "30m").row(0, named=True)
    assert out["open"] == pytest.approx(100.0)
    assert out["close"] == pytest.approx(110.0)


def test_usd_notional_buckets():
    # price 2000 USD/ETH: 30 ETH = $60k -> 50_200k bucket; 2 ETH = $4k -> lt10k
    df = _trades([
        (T0, 2000.0, 30.0, False),   # $60k -> vol_50_200k
        (T0 + MIN, 2000.0, 2.0, False),  # $4k -> vol_lt10k
        (T0 + 2 * MIN, 2000.0, 200.0, True),  # $400k -> vol_gt200k
    ])
    out = orderflow.aggregate(df, "30m").row(0, named=True)
    assert out["vol_lt10k"] == pytest.approx(2.0)
    assert out["vol_50_200k"] == pytest.approx(30.0)
    assert out["vol_gt200k"] == pytest.approx(200.0)
    assert out["vol_10_50k"] == pytest.approx(0.0)
