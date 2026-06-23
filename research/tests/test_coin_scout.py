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


def _cov(available=True, depth=1000):
    return SourceCoverage(
        available=available,
        earliest=date(2022, 1, 1) if available else None,
        depth_days=depth if available else None,
        error=None,
    )


def test_score_go_when_all_three_deep():
    v, reasons = coin_scout.score_coin(_cov(depth=1500), _cov(depth=1500), _cov(depth=1400))
    assert v == "GO"
    assert reasons


def test_score_nogo_when_ohlcv_missing():
    v, _ = coin_scout.score_coin(_cov(available=False), _cov(), _cov())
    assert v == "NO_GO"


def test_score_nogo_when_ohlcv_below_floor():
    v, reasons = coin_scout.score_coin(_cov(depth=300), _cov(), _cov())
    assert v == "NO_GO"
    assert any("365" in r for r in reasons)


def test_score_partial_when_archive_missing_flags_positioning():
    v, reasons = coin_scout.score_coin(_cov(depth=1500), _cov(depth=1500), _cov(available=False))
    assert v == "PARTIAL"
    assert any("positioning" in r for r in reasons)


def test_score_partial_when_archive_shallow():
    v, _ = coin_scout.score_coin(_cov(depth=1500), _cov(depth=1500), _cov(depth=400))
    assert v == "PARTIAL"


def test_score_go_at_exact_target_boundary():
    v, _ = coin_scout.score_coin(_cov(depth=730), _cov(depth=730), _cov(depth=730))
    assert v == "GO"


def test_score_partial_at_ohlcv_floor_but_below_target():
    # 365 is not < 365 → not NO_GO; but < 730 target → PARTIAL
    v, _ = coin_scout.score_coin(_cov(depth=365), _cov(depth=1500), _cov(depth=1500))
    assert v == "PARTIAL"
