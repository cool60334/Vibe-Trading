import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from trader.signal import compute_signal, SignalResult


# ── A trivial stub signal engine written to a run_dir ──────────────────────────
_STUB_ENGINE = '''
import pandas as pd

class SignalEngine:
    SYMBOL = "ETH-USDT-SWAP"
    def generate(self, data_map):
        df = data_map[self.SYMBOL]
        # always long on the last bar
        return {self.SYMBOL: pd.Series(1.0, index=df.index)}
'''


def _make_run_dir(tmp_path: Path) -> Path:
    code = tmp_path / "code"
    code.mkdir(parents=True)
    (code / "signal_engine.py").write_text(_STUB_ENGINE, encoding="utf-8")
    return tmp_path


def _write_meta(manifests: Path, index_end: str) -> None:
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "factor_values_eth.meta.json").write_text(
        json.dumps({"index_end": index_end}), encoding="utf-8"
    )


class _FakeExchange:
    """Minimal ccxt-like stub returning canned OHLCV."""
    def fetch_ohlcv(self, symbol, timeframe, limit):
        base = 1_700_000_000_000  # ms
        hour = 3_600_000
        return [[base + i * hour, 1, 1, 1, 1, 1] for i in range(5)]


def test_compute_signal_stale_short_circuits_without_fetch(tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    manifests = tmp_path / "manifests"
    _write_meta(manifests, "2026-06-01T00:00:00+00:00")  # 3 days before `now`

    # exchange=None proves the OHLCV fetch is NOT reached on the stale path.
    result = compute_signal(
        run_dir, exchange=None, symbol="ETH/USDT:USDT",
        manifests_dir=manifests, now=datetime(2026, 6, 4, tzinfo=timezone.utc),
    )
    assert isinstance(result, SignalResult)
    assert result.stale is True
    assert result.signal == 0
    assert result.age_days is not None and result.age_days > 2


def test_compute_signal_fresh_returns_engine_signal(tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    manifests = tmp_path / "manifests"
    _write_meta(manifests, "2026-06-04T00:00:00+00:00")  # same day as `now`

    result = compute_signal(
        run_dir, exchange=_FakeExchange(), symbol="ETH/USDT:USDT",
        manifests_dir=manifests, now=datetime(2026, 6, 4, 12, tzinfo=timezone.utc),
    )
    assert result.stale is False
    assert result.signal == 1


def test_compute_signal_no_manifests_dir_is_never_stale(tmp_path):
    run_dir = _make_run_dir(tmp_path / "run")
    result = compute_signal(
        run_dir, exchange=_FakeExchange(), symbol="ETH/USDT:USDT",
    )
    assert result.stale is False
    assert result.signal == 1
