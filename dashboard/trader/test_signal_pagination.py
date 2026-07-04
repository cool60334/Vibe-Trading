"""Tests for signal._fetch_ohlcv pagination + compute_signal(min_bars=...).

Exchanges cap fetch_ohlcv at ~1000 rows per call (Bybit v5 kline limit), so a
2160-bar percentile window can never arrive in one call. _fetch_ohlcv must
paginate; compute_signal must flag insufficient history instead of silently
returning an all-NaN-driven 0 signal.
"""

from pathlib import Path

from trader.signal import _fetch_ohlcv, compute_signal, SignalResult

_BASE_MS = 1_700_000_000_000
_HOUR_MS = 3_600_000


class _PagedExchange:
    """ccxt-like stub with a since-aware, page-capped fetch_ohlcv."""

    def __init__(self, n_bars: int, page_cap: int = 1000):
        self.rows = [
            [_BASE_MS + i * _HOUR_MS, 1.0, 1.0, 1.0, 1.0 + i * 1e-6, 1.0]
            for i in range(n_bars)
        ]
        self.page_cap = page_cap
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append({"since": since, "limit": limit})
        n = min(limit or self.page_cap, self.page_cap)
        if since is None:
            return self.rows[-n:]
        eligible = [r for r in self.rows if r[0] >= since]
        return eligible[:n]


class _LegacyExchange:
    """Old-style stub WITHOUT a since parameter — small fetches must keep
    calling fetch_ohlcv(symbol, timeframe, limit=...) so existing brokers/fakes
    stay compatible."""

    def __init__(self):
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls += 1
        return [[_BASE_MS + i * _HOUR_MS, 1, 1, 1, 1, 1] for i in range(min(limit, 5))]


# ── _fetch_ohlcv ──────────────────────────────────────────────────────────────


def test_small_lookback_stays_single_legacy_call():
    ex = _LegacyExchange()
    df = _fetch_ohlcv(ex, "ETH/USDT:USDT", "1H", lookback=5)
    assert len(df) == 5
    assert ex.calls == 1


def test_paginated_fetch_assembles_full_window():
    ex = _PagedExchange(n_bars=3000)
    df = _fetch_ohlcv(ex, "ETH/USDT:USDT", "1H", lookback=2200)
    assert len(df) == 2200
    assert df.index.is_monotonic_increasing
    assert df.index.is_unique
    # newest bar of history must be the last row
    assert int(df["close"].iloc[-1] * 1e6) == int((1.0 + 2999 * 1e-6) * 1e6)
    assert len(ex.calls) >= 3  # 2200 bars can't arrive in <3 pages of 1000


def test_paginated_fetch_dedupes_overlapping_pages():
    ex = _PagedExchange(n_bars=2500)
    df = _fetch_ohlcv(ex, "ETH/USDT:USDT", "1H", lookback=1500)
    assert df.index.is_unique
    assert len(df) == 1500


def test_paginated_fetch_short_history_returns_all_available():
    ex = _PagedExchange(n_bars=500)
    df = _fetch_ohlcv(ex, "ETH/USDT:USDT", "1H", lookback=2200)
    assert len(df) == 500  # terminates instead of looping forever


# ── compute_signal(min_bars=...) ─────────────────────────────────────────────

_STUB_ENGINE = '''
import pandas as pd

class SignalEngine:
    SYMBOL = "ETH-USDT-SWAP"
    def generate(self, data_map):
        df = data_map[self.SYMBOL]
        return {self.SYMBOL: pd.Series(1.0, index=df.index)}
'''


def _make_run_dir(tmp_path: Path) -> Path:
    code = tmp_path / "code"
    code.mkdir(parents=True)
    (code / "signal_engine.py").write_text(_STUB_ENGINE, encoding="utf-8")
    return tmp_path


def test_min_bars_insufficient_flags_and_zeroes_signal(tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    ex = _PagedExchange(n_bars=5)
    result = compute_signal(
        run_dir, exchange=ex, symbol="ETH/USDT:USDT", lookback=5, min_bars=10,
    )
    assert isinstance(result, SignalResult)
    assert result.insufficient_history is True
    assert result.signal == 0
    assert result.bars == 5
    assert result.stale is False


def test_min_bars_satisfied_returns_engine_signal(tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    ex = _PagedExchange(n_bars=5)
    result = compute_signal(
        run_dir, exchange=ex, symbol="ETH/USDT:USDT", lookback=5, min_bars=3,
    )
    assert result.insufficient_history is False
    assert result.signal == 1
    assert result.bars == 5


def test_no_min_bars_keeps_legacy_behaviour(tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    ex = _PagedExchange(n_bars=5)
    result = compute_signal(run_dir, exchange=ex, symbol="ETH/USDT:USDT", lookback=5)
    assert result.insufficient_history is False
    assert result.signal == 1


# ── End-to-end regression of the D2 root cause ───────────────────────────────
# An engine computing rolling(2160, min_periods=1080) on the fetched frame is
# all-NaN on a 200-bar window (signal stuck at 0 — the bug that silenced
# eth_s5/sol_s1 for a month) and alive on a paginated 2200-bar window.

_PERCENTILE_ENGINE = '''
import pandas as pd

class SignalEngine:
    SYMBOL = "ETH-USDT-SWAP"
    WIN = 2160  # percentile_90d at 1H

    def generate(self, data_map):
        df = data_map[self.SYMBOL]
        factor = df["close"]  # stand-in for a factor reindexed onto ohlcv.index
        pct = factor.rolling(self.WIN, min_periods=self.WIN // 2).rank(pct=True) * 100
        entry = pct >= 70
        return {self.SYMBOL: entry.astype(float)}
'''


def test_percentile_engine_dead_on_short_window_alive_on_full(tmp_path):
    run_dir = tmp_path / "run"
    code = run_dir / "code"
    code.mkdir(parents=True)
    (code / "signal_engine.py").write_text(_PERCENTILE_ENGINE, encoding="utf-8")

    # close is monotonically increasing → the newest bar ranks at percentile
    # 100 whenever the rolling window has enough samples to be defined.
    ex = _PagedExchange(n_bars=2500)

    dead = compute_signal(run_dir, exchange=ex, symbol="ETH/USDT:USDT", lookback=200)
    assert dead.signal == 0  # all-NaN percentile → never fires (the D2 bug)

    alive = compute_signal(run_dir, exchange=ex, symbol="ETH/USDT:USDT", lookback=2200)
    assert alive.bars == 2200
    assert alive.signal == 1  # same engine, sufficient window → fires
