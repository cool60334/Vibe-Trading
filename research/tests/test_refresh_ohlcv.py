from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ETH = SimpleNamespace(name="eth", okx_swap="ETH-USDT-SWAP")
CFG = SimpleNamespace(interval="1H")


def _write_features(mdir, short="eth", start="2022-06-26", periods=200):
    idx = pd.date_range(start, periods=periods, freq="h", tz="UTC")
    pd.DataFrame({"rsi_14": np.arange(periods, dtype=float)}, index=idx).to_parquet(
        mdir / f"features_{short}.parquet")
    return idx


def _ohlcv_for(idx):
    return pd.DataFrame({c: np.arange(len(idx), dtype=float)
                         for c in ["open", "high", "low", "close", "volume"]}, index=idx)


def test_writes_ohlcv_parquet_next_to_features(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path)
    monkeypatch.setattr(mod, "fetch_candles", lambda sym, days, bar: _ohlcv_for(idx))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is True
    out = pd.read_parquet(tmp_path / "ohlcv_eth.parquet")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.tz is not None


def test_depth_derives_from_features_earliest_tz_aware(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path, start="2022-06-26", periods=100)
    captured = {}
    def fake(sym, days, bar): captured["days"] = days; return _ohlcv_for(idx)
    monkeypatch.setattr(mod, "fetch_candles", fake)
    mod.refresh_symbol(ETH, CFG, tmp_path)         # must not raise TypeError on the subtraction
    assert captured["days"] >= (datetime.now(timezone.utc) - idx.min()).days


def test_gappy_fetch_fails_via_align_ohlcv(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path, periods=200)
    half = idx[:80]                                 # covers 40% of the feature index -> < 95%
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: _ohlcv_for(half))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert not (tmp_path / "ohlcv_eth.parquet").exists()


def test_missing_features_fails_without_fetching(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    called = {"fetch": False}
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: called.__setitem__("fetch", True))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert called["fetch"] is False                # no features -> never fetched


def test_empty_features_fails_before_fetch(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    pd.DataFrame({"rsi_14": pd.Series([], dtype=float)},
                 index=pd.DatetimeIndex([], tz="UTC")).to_parquet(tmp_path / "features_eth.parquet")
    called = {"fetch": False}
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: called.__setitem__("fetch", True))
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert called["fetch"] is False                # NaT-guard fires before fetch


def test_fetch_failure_leaves_old_parquet_intact(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path)
    (tmp_path / "ohlcv_eth.parquet").write_bytes(b"OLD")     # sentinel
    def boom(*a, **k): raise RuntimeError("OKX 500")
    monkeypatch.setattr(mod, "fetch_candles", boom)
    assert mod.refresh_symbol(ETH, CFG, tmp_path) is False
    assert (tmp_path / "ohlcv_eth.parquet").read_bytes() == b"OLD"   # untouched


def test_atomic_write_used(tmp_path, monkeypatch):
    from pathlib import Path
    import research.pipeline.refresh_ohlcv as mod
    idx = _write_features(tmp_path)
    monkeypatch.setattr(mod, "fetch_candles", lambda *a, **k: _ohlcv_for(idx))
    seen = []
    real = mod._atomic_to_parquet
    monkeypatch.setattr(mod, "_atomic_to_parquet",
                        lambda df, path: (seen.append(Path(path).name), real(df, path))[1])
    mod.refresh_symbol(ETH, CFG, tmp_path)
    assert seen == ["ohlcv_eth.parquet"]


def test_main_attempts_all_symbols_and_exits_1_on_any_failure(tmp_path, monkeypatch):
    import research.pipeline.refresh_ohlcv as mod
    cfg = SimpleNamespace(
        symbols=[SimpleNamespace(name="btc", okx_swap="BTC-USDT-SWAP"),
                 SimpleNamespace(name="eth", okx_swap="ETH-USDT-SWAP")],
        feature_store_path="research/manifests", interval="1H")
    monkeypatch.setattr(mod, "load_config", lambda path=None: cfg)
    done = []
    monkeypatch.setattr(mod, "refresh_symbol",
                        lambda s, c, m: (done.append(s.name), s.name == "eth")[1])  # btc fails
    rc = mod.main(["--manifests-dir", str(tmp_path)])
    assert rc == 1 and done == ["btc", "eth"]       # both attempted despite btc failing


def test_only_symbol_env_is_honored_by_load_config(monkeypatch):
    # RESEARCH_ONLY_SYMBOL filtering lives in load_config (reused); verify the reuse.
    monkeypatch.setenv("RESEARCH_ONLY_SYMBOL", "eth")
    import importlib
    import research.pipeline.refresh_ohlcv as mod
    cfg = mod.load_config()
    assert [s.name for s in cfg.symbols] == ["eth"]
