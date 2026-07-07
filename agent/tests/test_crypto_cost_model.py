import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.crypto import CryptoEngine  # noqa: E402


def test_close_leg_uses_maker_by_default():
    """Legacy: open hits taker, close hits maker (unchanged upstream behaviour)."""
    eng = CryptoEngine({"taker_rate": 0.00055, "maker_rate": 0.0002})
    open_fee = eng.calc_commission(size=1.0, price=100.0, _direction=1, is_open=True)
    close_fee = eng.calc_commission(size=1.0, price=100.0, _direction=-1, is_open=False)
    assert open_fee == 1.0 * 100.0 * 0.00055
    assert close_fee == 1.0 * 100.0 * 0.0002


def test_close_leg_uses_taker_when_flag_set():
    """taker_both_legs=True: both legs charged taker (matches live market orders)."""
    eng = CryptoEngine({"taker_rate": 0.00055, "maker_rate": 0.0002, "taker_both_legs": True})
    close_fee = eng.calc_commission(size=1.0, price=100.0, _direction=-1, is_open=False)
    assert close_fee == 1.0 * 100.0 * 0.00055


import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from backtest.engines._market_hooks import calc_crypto_funding_fee  # noqa: E402
from backtest.models import Position  # noqa: E402


def _long_pos(symbol="BTC-USDT-SWAP", size=1.0, price=100.0):
    return {symbol: Position(symbol=symbol, direction=1, size=size,
                             entry_price=price, entry_time=pd.Timestamp("2024-01-01"),
                             leverage=1.0)}


def test_funding_lookup_uses_settlement_timestamp_value():
    """At settlement bar T, charge funding_rate_raw[T] — NOT a future value (PIT)."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")            # settlement hour
    future_ts = pd.Timestamp("2024-01-01 16:00")
    lookup = {ts: 0.0003, future_ts: 0.0009}         # T value 0.0003, future 0.0009
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(
        sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
        interval="1H", funding_lookup=lookup,
    )
    # notional 100 * rate 0.0003 * direction +1 — uses T's value, not 0.0009
    assert fee == pytest.approx(100.0 * 0.0003 * 1)


def test_funding_lookup_missing_value_fails_loud():
    """Path provided but settlement timestamp absent → raise, never silently fallback."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")
    lookup = {pd.Timestamp("2024-01-01 00:00"): 0.0002}   # 08:00 missing
    bar = pd.Series({"close": 100.0})
    with pytest.raises(ValueError, match="funding value missing"):
        calc_crypto_funding_fee(
            sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
            interval="1H", funding_lookup=lookup,
        )


def test_funding_lookup_nan_value_fails_loud():
    """NaN at a settlement point is a data gap → fail loud, not fallback."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")
    lookup = {ts: float("nan")}
    bar = pd.Series({"close": 100.0})
    with pytest.raises(ValueError, match="funding value missing"):
        calc_crypto_funding_fee(
            sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
            interval="1H", funding_lookup=lookup,
        )


def test_no_funding_lookup_uses_fixed_rate():
    """No lookup (legacy / path absent) → fixed scalar, unchanged behaviour."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(
        sym, bar, ts, _long_pos(sym), 0.0001, set(), set(), interval="1H",
    )
    assert fee == pytest.approx(100.0 * 0.0001 * 1)


def test_non_settlement_bar_charges_nothing():
    """Bar outside {0,8,16}, with the daily fallback already consumed for
    that date, never settles again even with a lookup present — the
    settlement/dedup gating (untouched, pre-existing logic) runs before the
    lookup is ever consulted, so the new PIT feature can't cause a double
    charge. (Note: the legacy 1H/daily dedup fallback — unrelated to this
    task — will fire on the *first* non-settlement bar of a day if the day
    hasn't been marked done yet; pre-seeding daily_done_set here isolates the
    lookup behaviour from that pre-existing fallback quirk.)"""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 09:00")            # not a settlement hour
    lookup = {ts: 0.0003}
    bar = pd.Series({"close": 100.0})
    daily_done = {(sym, ts.date())}  # day's funding already settled/fallback-consumed
    fee = calc_crypto_funding_fee(
        sym, bar, ts, _long_pos(sym), 0.0001, set(), daily_done,
        interval="1H", funding_lookup=lookup,
    )
    assert fee == 0.0


def test_engine_loads_funding_series_into_dict(tmp_path):
    """__init__ loads funding_series_path parquet → timestamp-keyed dict once."""
    idx = pd.date_range("2024-01-01", periods=24, freq="h")
    df = pd.DataFrame({"funding_rate_raw": [0.0001] * 24}, index=idx)
    p = tmp_path / "features_btc.parquet"
    df.to_parquet(p)
    eng = CryptoEngine({"funding_series_path": str(p)})
    assert eng._funding_lookup is not None
    assert eng._funding_lookup[idx[8]] == pytest.approx(0.0001)


def test_engine_no_funding_path_leaves_lookup_none():
    eng = CryptoEngine({})
    assert eng._funding_lookup is None


def test_engine_normalizes_tz_aware_funding_index_to_naive(tmp_path):
    """Real feature-store parquets (research/lib/factor_io.py) always save a
    tz-aware UTC DatetimeIndex. The engine's own bar timestamps (from the okx
    loader / local_loader, both tz-naive UTC-equivalent) are tz-naive. A dict
    keyed by tz-aware pd.Timestamps can never match a tz-naive lookup key
    (pd.Timestamp("2024-01-01", tz="UTC") != pd.Timestamp("2024-01-01")) even
    though they represent the same instant -- so __init__ must normalize the
    loaded index to tz-naive before building the lookup dict, or every
    settlement bar spuriously fails the fail-loud missing-value check on real
    data.
    """
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"funding_rate_raw": [0.00025] * 24}, index=idx)
    p = tmp_path / "features_eth.parquet"
    df.to_parquet(p)

    eng = CryptoEngine({"funding_series_path": str(p)})
    assert eng._funding_lookup is not None

    # The engine's own bar timestamps are tz-naive (see okx.py / local_loader.py).
    naive_ts = pd.Timestamp("2024-01-01 08:00")
    assert naive_ts in eng._funding_lookup
    assert eng._funding_lookup[naive_ts] == pytest.approx(0.00025)

    fee = calc_crypto_funding_fee(
        "ETH-USDT-SWAP", pd.Series({"close": 100.0}), naive_ts,
        _long_pos("ETH-USDT-SWAP"), 0.0001, set(), set(),
        interval="1H", funding_lookup=eng._funding_lookup,
    )
    assert fee == pytest.approx(100.0 * 0.00025 * 1)
