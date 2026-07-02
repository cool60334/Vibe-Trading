import pandas as pd
from research.lib.intrabar_stop_audit import reconstruct_trades, TradeWindow


def test_reconstruct_two_trades_long_then_short():
    pos = pd.Series([0.0, 0.0, 0.45, 0.45, 0.45, 0.0, -0.45, -0.45, 0.0])
    windows = reconstruct_trades(pos)
    assert windows == [
        TradeWindow(direction=1, entry_idx=2, end_idx=4, exit_idx=5),
        TradeWindow(direction=-1, entry_idx=6, end_idx=7, exit_idx=8),
    ]


def test_reconstruct_run_reaching_last_bar_has_no_exit_idx():
    pos = pd.Series([0.0, 0.45, 0.45])
    windows = reconstruct_trades(pos)
    assert windows == [TradeWindow(direction=1, entry_idx=1, end_idx=2, exit_idx=None)]


def test_reconstruct_direct_flip_has_exit_at_flip_bar():
    pos = pd.Series([0.45, 0.45, -0.45])
    windows = reconstruct_trades(pos)
    assert windows[0] == TradeWindow(direction=1, entry_idx=0, end_idx=1, exit_idx=2)
    assert windows[1] == TradeWindow(direction=-1, entry_idx=2, end_idx=2, exit_idx=None)


from research.lib.intrabar_stop_audit import audit_trades, AuditResult


def _ohlcv(rows):
    # rows: list of (open, high, low, close)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_long_breach_optimistic_stop_ref_off_prior_close():
    # entry_idx=1; stop_ref = close[0] = 100 -> stop = 97 (3%).
    # held bars 1..2; bar 1 low=96 <= 97 -> breach. Real exit at open[exit_idx=3]=101
    # (signal), which is a BETTER long price than honest 97 -> optimistic.
    oh = _ohlcv([
        (100, 101, 99, 100),   # 0 signal bar
        (100, 100, 96, 99),    # 1 entry bar, wick to 96
        (99, 102, 98, 101),    # 2
        (101, 101, 100, 100),  # 3 exit bar (flat)
    ])
    windows = [TradeWindow(direction=1, entry_idx=1, end_idx=2, exit_idx=3)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["signal"])
    assert r.n_trades == 1 and r.n_breached == 1 and r.n_optimistic == 1
    assert round(r.max_breach_depth_pct, 4) == round((97 - 96) / 97 * 100, 4)


def test_stop_ref_uses_signal_close_not_fill_gap_up():
    # close[0]=100 -> stop=97. Entry gaps up: open[1]=110 (a fill-based stop would be
    # 106.7). Bar 1 low=105 breaches the fill-based level but NOT the close-based 97.
    oh = _ohlcv([
        (100, 101, 99, 100),   # 0 signal close = 100
        (110, 112, 105, 108),  # 1 entry, low 105 > 97
        (108, 109, 98, 100),   # 2 low 98 > 97 (still no breach)
        (100, 100, 99, 99),    # 3 flat
    ])
    windows = [TradeWindow(direction=1, entry_idx=1, end_idx=2, exit_idx=3)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["signal"])
    assert r.n_breached == 0


def test_signal_exit_bar_is_excluded():
    # Held bars 1..1 (end_idx=1); exit bar 2 (flat) wicks through the stop but is
    # outside the run -> not counted.
    oh = _ohlcv([
        (100, 101, 99, 100),   # 0 signal close 100 -> stop 97
        (100, 101, 99, 100),   # 1 held, no breach
        (100, 101, 90, 95),    # 2 flat bar wick to 90 (excluded)
    ])
    windows = [TradeWindow(direction=1, entry_idx=1, end_idx=1, exit_idx=2)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["signal"])
    assert r.n_breached == 0


def test_end_of_backtest_includes_last_held_bar():
    # Force-close run reaching the last bar: end_idx=1, exit_idx=None. Last bar wicks
    # through the stop -> counted; actual exit uses close[end_idx].
    oh = _ohlcv([
        (100, 101, 99, 100),   # 0 signal close 100 -> stop 97
        (100, 101, 90, 92),    # 1 last held bar, low 90 <= 97
    ])
    windows = [TradeWindow(direction=1, entry_idx=1, end_idx=1, exit_idx=None)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["end_of_backtest"])
    assert r.n_breached == 1


def test_gap_through_stop_uses_open():
    # Bar 1 opens 95 (below stop 97) -> honest fill at open 95, not 97.
    oh = _ohlcv([
        (100, 101, 99, 100),   # 0 -> stop 97
        (95, 96, 93, 94),      # 1 gap open 95 < 97
        (94, 95, 90, 91),      # 2 real exit worse -> pessimistic (honest 95 better than 91)
        (91, 92, 90, 91),      # 3 flat
    ])
    windows = [TradeWindow(direction=1, entry_idx=1, end_idx=2, exit_idx=3)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["signal"])
    assert r.n_breached == 1 and r.n_optimistic == 0   # honest 95 > actual open[3]=91 (long): pessimistic


def test_short_breach_symmetric():
    # short entry_idx=1; stop_ref=close[0]=100 -> stop=103. bar 1 high=104 >= 103.
    oh = _ohlcv([
        (100, 101, 99, 100),   # 0
        (100, 104, 100, 101),  # 1 high 104 >= 103
        (101, 102, 100, 100),  # 2
        (100, 100, 99, 99),    # 3 flat, real exit open=100 (better short price than honest 103)
    ])
    windows = [TradeWindow(direction=-1, entry_idx=1, end_idx=2, exit_idx=3)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["signal"])
    assert r.n_breached == 1 and r.n_optimistic == 1


def test_no_breach_not_counted():
    oh = _ohlcv([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
    ])
    windows = [TradeWindow(direction=1, entry_idx=1, end_idx=1, exit_idx=2)]
    r = audit_trades(windows, oh, stop_pct=3.0, slippage_rate=0.0, exit_reasons=["signal"])
    assert r.n_trades == 1 and r.n_breached == 0 and r.mean_breach_depth_pct is None
