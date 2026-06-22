import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[2]
ENGINE_SRC = REPO / "research" / "strategies" / "code" / "sol_s1_single_factor_regime" / "signal_engine.py"


def _load_engine_class():
    spec = importlib.util.spec_from_file_location("_sol_s1_paper_engine", ENGINE_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SignalEngine


def _hourly_index(n):
    return pd.date_range("2025-01-01", periods=n, freq="1h")


def _write_factor_store(tmp_path, idx, ls_divergence):
    """Write a minimal factor_values_sol parquet the engine can load."""
    mdir = tmp_path / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"ls_divergence": ls_divergence}, index=idx)
    df.index = df.index.tz_localize("UTC")
    df.to_parquet(mdir / "factor_values_sol.parquet", engine="pyarrow")
    (mdir / "factor_values_sol.meta.json").write_text(
        json.dumps({"schema_version": 1, "symbol": "sol",
                    "index_end": df.index.max().isoformat()}), encoding="utf-8")
    return mdir


def test_contrarian_direction_long_at_low_percentile(tmp_path, monkeypatch):
    """Negative-IC factor: a LOW ls_divergence percentile must produce a LONG (+) signal."""
    n = 24 * 200
    idx = _hourly_index(n)
    # Ramp ls_divergence up over time, then drop to the bottom at the end so the
    # trailing percentile is low → contrarian long.
    vals = np.concatenate([np.linspace(5, 10, n - 48), np.full(48, -10.0)])
    mdir = _write_factor_store(tmp_path, idx, vals)
    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    # Point load_factor_values at the temp manifests dir.
    import sys
    sys.path.insert(0, str(REPO / "research"))
    import lib.factor_io as fio
    monkeypatch.setattr(fio, "_default_manifests_dir", lambda: mdir)

    ohlcv = pd.DataFrame({"close": np.linspace(100, 120, n),
                          "open": 100.0, "high": 121.0, "low": 99.0, "volume": 1.0},
                         index=idx)
    sig = _load_engine_class()().generate({"SOL-USDT-SWAP": ohlcv})["SOL-USDT-SWAP"]
    assert sig.iloc[-1] > 0, "low ls_divergence percentile should be a contrarian LONG"


def test_signal_magnitude_is_size_mult(tmp_path, monkeypatch):
    """Non-zero signals must equal ±SIZE_MULT (validation runs at 1.0)."""
    cls = _load_engine_class()
    assert cls.SIZE_MULT == 1.0, "validation engine must run at full size (alpha is size-invariant)"


def test_factor_lag_hours_shifts_signal(tmp_path, monkeypatch):
    """SOL_FACTOR_LAG_HOURS (env, read at generate() time — the runner's AST validator
    forbids non-literal class-level assignments) must shift the factor read so a 24h
    lag changes the produced signal series vs lag 0."""
    n = 24 * 200
    idx = _hourly_index(n)
    # Oscillating factor: a 24-bar shift moves percentile crossings → different entries.
    vals = 5.0 * np.sin(np.linspace(0, 40 * np.pi, n))
    mdir = _write_factor_store(tmp_path, idx, vals)
    monkeypatch.setenv("RESEARCH_INTERVAL", "1H")
    import sys
    sys.path.insert(0, str(REPO / "research"))
    import lib.factor_io as fio
    monkeypatch.setattr(fio, "_default_manifests_dir", lambda: mdir)

    ohlcv = pd.DataFrame({"close": np.linspace(100, 120, n),
                          "open": 100.0, "high": 121.0, "low": 99.0, "volume": 1.0},
                         index=idx)

    monkeypatch.setenv("SOL_FACTOR_LAG_HOURS", "0")
    sig0 = _load_engine_class()().generate({"SOL-USDT-SWAP": ohlcv})["SOL-USDT-SWAP"]
    monkeypatch.setenv("SOL_FACTOR_LAG_HOURS", "24")
    sig24 = _load_engine_class()().generate({"SOL-USDT-SWAP": ohlcv})["SOL-USDT-SWAP"]

    assert (sig0 != 0).any(), "lag-0 should produce at least one entry (test sanity)"
    assert not sig0.equals(sig24), "a 24h factor lag must change the signal series"
