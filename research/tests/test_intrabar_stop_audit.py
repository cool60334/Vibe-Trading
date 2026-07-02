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
