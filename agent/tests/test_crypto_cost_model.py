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
