"""Golden-run contract test for the upstream agent/backtest engine.

research/ depends on agent/backtest as its simulator. Upstream pulls can
silently change engine behaviour (fee defaults, signal alignment,
execution timing) and every research conclusion with it. This test runs a
fixed synthetic market through CryptoEngine and pins the metrics.

If this test goes red after an upstream update: the ENGINE changed, not
your strategy. Investigate the diff in agent/backtest before re-freezing.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_DIR = _REPO_ROOT / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.crypto import CryptoEngine  # noqa: E402

SYMBOL = "BTC-USDT-SWAP"
N_BARS = 24 * 120  # 120 days of 1H bars


def _synthetic_ohlcv() -> pd.DataFrame:
    """Deterministic random-walk OHLCV. Seeded → identical on every run."""
    rng = np.random.default_rng(20260704)
    idx = pd.date_range("2024-01-01", periods=N_BARS, freq="h")
    ret = rng.normal(0, 0.004, N_BARS)
    close = 30000 * np.exp(np.cumsum(ret))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.002, N_BARS))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.002, N_BARS))
    vol = rng.uniform(100, 1000, N_BARS)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


class _StubLoader:
    name = "golden-stub"

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def fetch(self, codes, start_date, end_date, fields=None, interval=None):
        return {c: self._df.copy() for c in codes}


class _FlipEngine:
    """Deterministic long/short flipper: sign of a slow sine → ~dozens of trades."""

    def generate(self, data_map):
        df = data_map[SYMBOL]
        phase = np.arange(len(df)) / (24 * 10) * 2 * np.pi  # 10-day cycle
        sig = pd.Series(np.sign(np.sin(phase)), index=df.index, dtype=float)
        return {SYMBOL: sig}


# Frozen snapshot — captured from the current agent/backtest CryptoEngine
# behaviour on 2026-07-05 (Task 1 Step 3). All four keys exist verbatim in
# the metrics dict returned by run_backtest — no substitutions needed.
GOLDEN: "dict | None" = {
    "sharpe": 1.0058150781350466,
    "total_return": 0.10649427050171179,
    "max_drawdown": -0.16641135776478017,
    "trade_count": 24.0,
}


def _run() -> dict:
    import tempfile

    df = _synthetic_ohlcv()
    config = {"codes": [SYMBOL], "interval": "1H", "initial_cash": 100_000}
    engine = CryptoEngine(config)
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        (run_dir / "code").mkdir()
        (run_dir / "code" / "signal_engine.py").write_text("# golden stub\n")
        metrics = engine.run_backtest(
            config, _StubLoader(df), _FlipEngine(), run_dir, bars_per_year=8760,
        )
    return metrics


def test_engine_behaviour_is_frozen():
    assert GOLDEN is not None, (
        "GOLDEN snapshot not frozen yet — run the capture step "
        "(see plan Task 1 Step 3) and paste the printed dict here."
    )
    m = _run()
    for key, expected in GOLDEN.items():
        actual = float(m[key])
        assert actual == pytest.approx(expected, abs=1e-9), (
            f"engine contract drift on {key!r}: frozen={expected} now={actual} — "
            "upstream agent/backtest behaviour changed"
        )
