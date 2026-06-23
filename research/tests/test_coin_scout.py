# research/tests/test_coin_scout.py
from datetime import date, timedelta

import pandas as pd

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


def test_earliest_archive_day_finds_boundary():
    start = date(2021, 3, 1)
    today = date(2026, 6, 23)
    got = coin_scout._earliest_archive_day(
        "BNBUSDT", today=today, exists=lambda s, d: d >= start
    )
    assert got == start


def test_earliest_archive_day_all_absent_returns_none():
    got = coin_scout._earliest_archive_day(
        "ZZZUSDT", today=date(2026, 6, 23), exists=lambda s, d: False
    )
    assert got is None


def test_earliest_archive_day_skips_tail_soft_gap():
    start = date(2022, 1, 1)
    today = date(2026, 6, 23)
    gap = today - timedelta(days=2)  # the first anchor probe day is a soft gap
    got = coin_scout._earliest_archive_day(
        "BNBUSDT", today=today, exists=lambda s, d: d >= start and d != gap
    )
    assert got == start


def test_earliest_archive_day_floor_clamped():
    # exists from before the floor → boundary is the floor itself
    got = coin_scout._earliest_archive_day(
        "BTCUSDT", today=date(2026, 6, 23), exists=lambda s, d: True
    )
    assert got == coin_scout._ARCHIVE_FLOOR


def test_probe_okx_ohlcv_available(monkeypatch):
    idx = pd.to_datetime(["2021-01-01", "2026-06-20"], utc=True)
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    monkeypatch.setattr(coin_scout.okx_data, "fetch_candles", lambda *a, **k: df)
    cov = coin_scout.probe_okx_ohlcv("BNB-USDT-SWAP")
    assert cov.available is True
    assert cov.earliest == date(2021, 1, 1)
    assert cov.depth_days >= 1800


def test_probe_okx_ohlcv_unknown_symbol_is_unavailable(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history-candles error: 51001 unknown instId")
    monkeypatch.setattr(coin_scout.okx_data, "fetch_candles", boom)
    cov = coin_scout.probe_okx_ohlcv("ZZZ-USDT-SWAP")
    assert cov.available is False
    assert cov.error is not None


def test_probe_okx_ohlcv_empty_frame_is_unavailable(monkeypatch):
    monkeypatch.setattr(coin_scout.okx_data, "fetch_candles", lambda *a, **k: pd.DataFrame())
    cov = coin_scout.probe_okx_ohlcv("NEW-USDT-SWAP")
    assert cov.available is False
    assert cov.error is None


def test_probe_okx_funding_available(monkeypatch):
    idx = pd.to_datetime(["2021-06-01", "2026-06-20"], utc=True)
    df = pd.DataFrame({"funding_rate": [0.0001, 0.0001]}, index=idx)
    monkeypatch.setattr(coin_scout.okx_data, "fetch_funding_history", lambda *a, **k: df)
    cov = coin_scout.probe_okx_funding("BNB-USDT-SWAP")
    assert cov.available is True
    assert cov.earliest == date(2021, 6, 1)


def test_probe_binance_archive_available(monkeypatch):
    monkeypatch.setattr(
        coin_scout, "_earliest_archive_day", lambda *a, **k: date(2021, 2, 10)
    )
    cov = coin_scout.probe_binance_archive("BNBUSDT")
    assert cov.available is True
    assert cov.earliest == date(2021, 2, 10)
    assert cov.depth_days >= 1800


def test_probe_binance_archive_absent(monkeypatch):
    monkeypatch.setattr(coin_scout, "_earliest_archive_day", lambda *a, **k: None)
    cov = coin_scout.probe_binance_archive("ZZZUSDT")
    assert cov.available is False


def _go_verdict():
    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    return coin_scout.CoinVerdict(
        "bnb", "BNB-USDT-SWAP", "BNB/USDT:USDT", "BNBUSDT",
        c, c, c, "GO", ["all sources >= 730d"],
    )


def test_scout_coins_runs_each_coin(monkeypatch):
    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    monkeypatch.setattr(coin_scout, "probe_okx_ohlcv", lambda s: c)
    monkeypatch.setattr(coin_scout, "probe_okx_funding", lambda s: c)
    monkeypatch.setattr(coin_scout, "probe_binance_archive", lambda s: c)
    rep = coin_scout.scout_coins(["bnb", "XRP", ""])  # blank skipped
    assert [v.name for v in rep.coins] == ["bnb", "xrp"]
    assert rep.coins[0].verdict == "GO"
    assert rep.thresholds["min_ohlcv_days"] == coin_scout.MIN_OHLCV_DAYS


def test_report_to_dict_serializes_dates():
    rep = coin_scout.ScoutReport("2026-06-23T00:00:00+00:00", {"min_ohlcv_days": 365}, [_go_verdict()])
    d = coin_scout.report_to_dict(rep)
    assert d["coins"][0]["okx_ohlcv"]["earliest"] == "2021-01-01"
    assert d["coins"][0]["verdict"] == "GO"


def test_format_table_lists_coin_and_verdict():
    rep = coin_scout.ScoutReport("t", {}, [_go_verdict()])
    table = coin_scout.format_table(rep)
    assert "bnb" in table and "GO" in table


def test_go_coins_yaml_only_includes_go():
    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    partial = coin_scout.CoinVerdict(
        "xrp", "XRP-USDT-SWAP", "XRP/USDT:USDT", "XRPUSDT", c, c, c, "PARTIAL", [],
    )
    rep = coin_scout.ScoutReport("t", {}, [_go_verdict(), partial])
    y = coin_scout.go_coins_yaml(rep)
    assert "name: bnb" in y and "BNB-USDT-SWAP" in y
    assert "xrp" not in y
