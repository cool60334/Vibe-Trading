# research/tests/test_coin_scout.py
from datetime import date

from lib import coin_scout
from lib.coin_scout import SourceCoverage


def test_tickers_for_derives_all_three():
    assert coin_scout.tickers_for("bnb") == (
        "BNB-USDT-SWAP", "BNB/USDT:USDT", "BNBUSDT",
    )


def test_tickers_for_is_case_insensitive():
    assert coin_scout.tickers_for("XrP") == (
        "XRP-USDT-SWAP", "XRP/USDT:USDT", "XRPUSDT",
    )


def test_source_coverage_holds_fields():
    c = SourceCoverage(available=True, earliest=date(2022, 1, 1), depth_days=900, error=None)
    assert c.available and c.depth_days == 900 and c.error is None
