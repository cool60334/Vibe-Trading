# Massive Cross-Venue Premium Factor — Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated cross-venue premium factor (USDT depeg + US fiat/Coinbase premium) from Massive USD spot vs OKX USDT spot, and run a cheap Phase-1 backtest kill-gate to decide GO/No-Go.

**Architecture:** New `massive_data.py` fetches Massive USD 1H aggregate bars. `derived_factors.cross_venue_premium_factors` splits the spread into a deconfounded `depeg_z` (stablecoin) + `fiat_prem_z` (basis). `stage0a_features.py` wires both in behind None-guards (failure → factor absent → 1H main line unaffected). New `factor_gates.py` holds reusable validation gates. A driver applies all six gates to BTC/ETH/SOL.

**Tech Stack:** Python, pandas, numpy, requests, scipy.stats (existing), pytest. Massive (Polygon.io) REST aggregates API.

**Design spec:** `docs/superpowers/specs/2026-06-25-massive-crossvenue-premium-factor-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `research/lib/massive_data.py` | Massive REST access (USD aggregate bars) | Create |
| `research/tests/test_massive_data.py` | Unit tests for massive_data (fixtures, no network) | Create |
| `research/pipeline/config.py` | `SymbolConfig.massive_usd` ticker property | Modify (`:49-52` area) |
| `research/lib/derived_factors.py` | `cross_venue_premium_factors` | Modify (after `:21`) |
| `research/tests/test_derived_factors.py` | Tests for the new factor function | Modify |
| `research/pipeline/stage0a_features.py` | Fetch + `build_feature_dict` wiring | Modify (`:204-213`, `:291-296`, `:551-607`) |
| `research/tests/test_stage0a_features.py` | `build_feature_dict` emits new keys | Modify |
| `research/lib/sources.py` | `SOURCE_REGISTRY` entry for Massive | Modify (`:39-96`) |
| `research/lib/factor_gates.py` | Reusable Phase-1 gate helpers | Create |
| `research/tests/test_factor_gates.py` | Unit tests for gate helpers | Create |
| `research/scripts/xvenue_phase1_gates.py` | Phase-1 measurement driver (needs API key) | Create |

**Test invocation:** research tests run from the `research/` directory (imports are `from lib...` / `from pipeline...`). Always: `cd research && python -m pytest tests/<file> -v`. Do NOT mix research and dashboard pytest in one run.

---

## Task 1: `massive_data.py` — Massive USD aggregate-bar fetcher

**Files:**
- Create: `research/lib/massive_data.py`
- Test: `research/tests/test_massive_data.py`

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_massive_data.py
import pandas as pd
import pytest

from lib import massive_data


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _bar(ts_ms, c):
    return {"t": ts_ms, "o": c, "h": c, "l": c, "c": c, "v": 1.0, "n": 1}


def test_interval_to_mult_timespan():
    assert massive_data._interval_to_mult_timespan("1H") == (1, "hour")
    assert massive_data._interval_to_mult_timespan("30m") == (30, "minute")
    assert massive_data._interval_to_mult_timespan("15m") == (15, "minute")


def test_fetch_spot_bars_parses_results(monkeypatch):
    payload = {"results": [_bar(1_700_000_000_000, 100.0), _bar(1_700_003_600_000, 101.0)]}
    monkeypatch.setattr(massive_data.requests, "get", lambda *a, **k: FakeResp(payload))
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    df = massive_data.fetch_spot_bars("X:BTCUSD", days=2, interval="1H")
    assert df is not None
    assert list(df.columns) >= ["close"]
    assert df["close"].tolist() == [100.0, 101.0]
    assert str(df.index.tz) == "UTC"


def test_fetch_spot_bars_empty_results_returns_none(monkeypatch):
    monkeypatch.setattr(massive_data.requests, "get", lambda *a, **k: FakeResp({"results": []}))
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    assert massive_data.fetch_spot_bars("X:NOPEUSD", days=2, interval="1H") is None


def test_fetch_spot_bars_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert massive_data.fetch_spot_bars("X:BTCUSD", days=2, interval="1H") is None


def test_fetch_spot_bars_retries_on_429(monkeypatch):
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp({}, status=429)
        return FakeResp({"results": [_bar(1_700_000_000_000, 100.0)]})

    monkeypatch.setattr(massive_data.requests, "get", flaky_get)
    monkeypatch.setattr(massive_data.time, "sleep", lambda *_: None)
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    df = massive_data.fetch_spot_bars("X:BTCUSD", days=2, interval="1H", max_retries=2)
    assert df is not None and calls["n"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_massive_data.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lib.massive_data'`

- [ ] **Step 3: Write minimal implementation**

```python
# research/lib/massive_data.py
"""Massive (Polygon.io) crypto USD aggregate-bar fetcher.

Only USD spot aggregate bars (1H by default) are needed for the cross-venue
premium factor. No tick / quote / WebSocket here (see design spec §2 non-goals).
Failure (missing key, no coverage, empty result) returns None so callers can
skip the factor without breaking the 1H main line.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

#: API host. Polygon endpoints remain valid post-Massive-rebrand; override here
#: in one place if the host moves to api.massive.com.
BASE_URL = "https://api.polygon.io"


def _interval_to_mult_timespan(interval: str) -> tuple[int, str]:
    """Map a candle string ('1H','30m','15m') to (multiplier, timespan)."""
    s = interval.strip().lower()
    if s.endswith("h"):
        return int(s[:-1] or "1"), "hour"
    if s.endswith("m"):
        return int(s[:-1]), "minute"
    if s.endswith("d"):
        return int(s[:-1] or "1"), "day"
    raise ValueError(f"unsupported interval: {interval!r}")


def fetch_spot_bars(
    ticker: str,
    days: int,
    interval: str = "1H",
    *,
    max_retries: int = 3,
) -> pd.DataFrame | None:
    """Fetch Massive USD aggregate bars for `ticker` (e.g. 'X:BTCUSD').

    Returns a DataFrame indexed by UTC DatetimeIndex with OHLCV columns
    (at least 'close'), or None on missing key / no coverage / empty result.
    """
    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        return None
    mult, timespan = _interval_to_mult_timespan(interval)
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    to = now.strftime("%Y-%m-%d")
    url = (
        f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/{mult}/{timespan}/{frm}/{to}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}

    rows: list[dict] = []
    next_url: str | None = url
    next_params: dict | None = params
    while next_url:
        resp = None
        for attempt in range(max_retries):
            resp = requests.get(next_url, params=next_params, timeout=30)
            if getattr(resp, "status_code", 200) == 429:
                time.sleep(2 ** attempt)
                continue
            break
        if resp is None or getattr(resp, "status_code", 200) == 429:
            break
        resp.raise_for_status()
        body = resp.json()
        rows.extend(body.get("results") or [])
        nxt = body.get("next_url")
        if nxt:
            next_url, next_params = nxt, {"apiKey": key}  # next_url carries the cursor
        else:
            next_url = None

    if not rows:
        return None
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["open", "high", "low", "close", "volume"]].sort_index()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_massive_data.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add research/lib/massive_data.py research/tests/test_massive_data.py
git commit -m "feat(massive): USD aggregate-bar fetcher with 429 backoff + None-on-failure"
```

---

## Task 2: `SymbolConfig.massive_usd` ticker property

**Files:**
- Modify: `research/pipeline/config.py:49-52` (add property next to `binance_usdt`)
- Test: `research/tests/test_massive_data.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to research/tests/test_massive_data.py
from pipeline.config import SymbolConfig


def test_symbolconfig_massive_usd_ticker():
    sym = SymbolConfig(name="btc", okx_swap="BTC-USDT-SWAP", ccxt_bybit="BTC/USDT:USDT")
    assert sym.massive_usd == "X:BTCUSD"
    sym2 = SymbolConfig(name="sol", okx_swap="SOL-USDT-SWAP", ccxt_bybit="SOL/USDT:USDT")
    assert sym2.massive_usd == "X:SOLUSD"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_massive_data.py::test_symbolconfig_massive_usd_ticker -v`
Expected: FAIL with `AttributeError: 'SymbolConfig' object has no attribute 'massive_usd'`

- [ ] **Step 3: Add the property**

In `research/pipeline/config.py`, immediately after the `binance_usdt` property (`:52`), add:

```python
    @property
    def massive_usd(self) -> str:
        """Massive (Polygon) USD spot ticker (e.g. 'X:BTCUSD') for cross-venue premium.

        Note: no-coverage coins (e.g. BNB on US venues) still return a ticker;
        the fetch then returns None and the factor is simply skipped.
        """
        return f"X:{self.name.upper()}USD"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_massive_data.py::test_symbolconfig_massive_usd_ticker -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/config.py research/tests/test_massive_data.py
git commit -m "feat(config): SymbolConfig.massive_usd ticker property"
```

---

## Task 3: `cross_venue_premium_factors` (deconfounded depeg + fiat premium)

**Files:**
- Modify: `research/lib/derived_factors.py` (add function after `basis_factors`, `:21`)
- Test: `research/tests/test_derived_factors.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to research/tests/test_derived_factors.py
import numpy as np
import pandas as pd
from lib.derived_factors import cross_venue_premium_factors


def _idx(n):
    return pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")


def test_cross_venue_premium_keys_and_shape():
    idx = _idx(800)
    okx = pd.Series(100.0, index=idx)
    usd = pd.Series(100.0, index=idx)
    r = pd.Series(1.0, index=idx)
    out = cross_venue_premium_factors(okx, usd, r)
    assert set(out) == {"depeg", "depeg_z", "fiat_prem", "fiat_prem_z"}
    for v in out.values():
        assert len(v) == len(idx)


def test_depeg_isolated_from_price():
    """A pure USDT discount (R<1) with equal USD/OKX prices => depeg negative,
    fiat_prem ~0 after deconfounding."""
    idx = _idx(800)
    okx = pd.Series(100.0, index=idx)   # USDT-denominated
    usd = pd.Series(100.0, index=idx)   # real USD
    r = pd.Series(0.99, index=idx)      # 1 USDT = 0.99 USD (discount)
    out = cross_venue_premium_factors(okx, usd, r)
    assert (out["depeg"] < 0).all()
    # fiat_prem = okx*R/usd - 1 = 100*0.99/100 - 1 = -0.01  (NOT ~0 here)
    assert np.allclose(out["fiat_prem"].dropna(), -0.01, atol=1e-9)


def test_fiat_premium_pure_when_no_depeg():
    """R==1 (no depeg): fiat_prem reduces to OKX-vs-USD spread."""
    idx = _idx(800)
    okx = pd.Series(101.0, index=idx)
    usd = pd.Series(100.0, index=idx)
    r = pd.Series(1.0, index=idx)
    out = cross_venue_premium_factors(okx, usd, r)
    assert np.allclose(out["fiat_prem"].dropna(), 0.01, atol=1e-9)
    assert np.allclose(out["depeg"].dropna(), 0.0, atol=1e-9)


def test_reindex_aligns_to_okx_index():
    idx = _idx(800)
    okx = pd.Series(100.0, index=idx)
    usd = pd.Series(100.0, index=idx[::2])   # sparser
    r = pd.Series(1.0, index=idx[::3])
    out = cross_venue_premium_factors(okx, usd, r)
    assert (out["fiat_prem"].index == idx).all()


def test_truncation_stability():
    idx = _idx(800)
    okx = pd.Series(np.linspace(100, 110, 800), index=idx)
    usd = pd.Series(np.linspace(100, 109, 800), index=idx)
    r = pd.Series(1.0, index=idx)
    full = cross_venue_premium_factors(okx, usd, r)
    trunc = cross_venue_premium_factors(okx.iloc[:-1], usd.iloc[:-1], r.iloc[:-1])
    # earlier values must not change when one bar is appended
    assert np.allclose(
        full["fiat_prem"].iloc[:-1].dropna().values,
        trunc["fiat_prem"].dropna().values,
        atol=1e-12,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_derived_factors.py -k cross_venue -v`
Expected: FAIL with `ImportError: cannot import name 'cross_venue_premium_factors'`

- [ ] **Step 3: Write the implementation**

In `research/lib/derived_factors.py`, after `basis_factors` (`:21`), add:

```python
def cross_venue_premium_factors(
    okx_spot_close: pd.Series,
    massive_usd_close: pd.Series,
    usdt_usd_rate: pd.Series,
) -> dict[str, pd.Series]:
    """Cross-venue premium factors from OKX USDT spot vs Massive USD spot.

    Splits the muddy spread into two deconfounded factors (design spec §3):
      depeg     = R - 1                       (pure USDT vs USD; category stablecoin)
      fiat_prem = okx*R / usd - 1             (OKX repriced to USD, depeg removed;
                                               pure offshore-vs-US fiat premium; basis)
    where R = usdt_usd_rate. All outputs are aligned to okx_spot_close.index.
    """
    idx = okx_spot_close.index
    usd = massive_usd_close.reindex(idx, method="ffill")
    r = usdt_usd_rate.reindex(idx, method="ffill")

    depeg = r - 1.0
    fiat_prem = (okx_spot_close * r) / usd - 1.0

    return {
        "depeg": depeg,
        "depeg_z": _rolling_z(depeg, SCREEN_ZSCORE_DAYS * 24),
        "fiat_prem": fiat_prem,
        "fiat_prem_z": _rolling_z(fiat_prem, SCREEN_ZSCORE_DAYS * 24),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_derived_factors.py -k cross_venue -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add research/lib/derived_factors.py research/tests/test_derived_factors.py
git commit -m "feat(factors): cross_venue_premium_factors (deconfounded depeg + fiat_prem)"
```

---

## Task 4: Wire the factor into `stage0a_features.py`

**Files:**
- Modify: `research/pipeline/stage0a_features.py` — `build_feature_dict` signature (`:204-213`), guard (`:291-296`), runner fetch (`:551-607`)
- Test: `research/tests/test_stage0a_features.py`

- [ ] **Step 1: Write the failing test**

```python
# append to research/tests/test_stage0a_features.py
def test_build_feature_dict_emits_cross_venue_keys(make_candles_fixture=None):
    """build_feature_dict with all three spot legs emits the 4 cross-venue keys;
    absent legs => keys simply absent (1H main line unaffected)."""
    import pandas as pd
    from pipeline.stage0a_features import build_feature_dict
    from pipeline.config import load_config

    cfg = load_config()
    idx = pd.date_range("2024-01-01", periods=800, freq="1h", tz="UTC")
    candles = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
        index=idx,
    )
    okx_spot = pd.Series(100.0, index=idx)
    massive_usd = pd.Series(100.0, index=idx)
    usdt_usd = pd.Series(1.0, index=idx)

    feats = build_feature_dict(
        candles=candles,
        config=cfg,
        spot_close=okx_spot,
        massive_usd_close=massive_usd,
        usdt_usd_close=usdt_usd,
    )
    assert {"depeg_z", "fiat_prem_z"} <= set(feats)

    # absent massive/usdt => no cross-venue keys, but basis_* still present
    feats2 = build_feature_dict(candles=candles, config=cfg, spot_close=okx_spot)
    assert "fiat_prem_z" not in feats2
    assert "basis_rel" in feats2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_stage0a_features.py::test_build_feature_dict_emits_cross_venue_keys -v`
Expected: FAIL with `TypeError: build_feature_dict() got an unexpected keyword argument 'massive_usd_close'`

- [ ] **Step 3a: Extend the import and signature**

In `research/pipeline/stage0a_features.py`, update the import (`:69`):

```python
from lib.derived_factors import (
    basis_factors,
    cross_venue_premium_factors,
    funding_factors,
    oi_factors,
    positioning_factors,
)
```

Add two kwargs to `build_feature_dict` (after `spot_close` at `:210`):

```python
    spot_close: pd.Series | None = None,
    massive_usd_close: pd.Series | None = None,
    usdt_usd_close: pd.Series | None = None,
```

- [ ] **Step 3b: Add the guarded factor computation**

In `build_feature_dict`, right after the existing basis block (`:291-292`):

```python
    if spot_close is not None:
        features.update(basis_factors(candles["close"], spot_close))
    if (
        spot_close is not None
        and massive_usd_close is not None
        and usdt_usd_close is not None
    ):
        features.update(
            cross_venue_premium_factors(spot_close, massive_usd_close, usdt_usd_close)
        )
```

- [ ] **Step 3c: Add the runner fetch block**

In the runner, immediately after the existing OKX spot fetch (`:551-564`), add:

```python
        # ── 2b. Fetch Massive USD spot + USDT/USD rate (cross-venue premium) ──
        massive_usd_close: pd.Series | None = None
        usdt_usd_close: pd.Series | None = None
        try:
            from lib import massive_data
            from lib.ccxt_data import fetch_ohlcv_ccxt

            mdf = massive_data.fetch_spot_bars(sym_cfg.massive_usd, cfg.period, cfg.interval)
            if mdf is not None and not mdf.empty:
                massive_usd_close = mdf["close"]
                # USDT/USD rate: prefer Massive X:USDTUSD, else ccxt Kraken USDT/USD
                rdf = massive_data.fetch_spot_bars("X:USDTUSD", cfg.period, cfg.interval)
                if rdf is None or rdf.empty:
                    rdf = fetch_ohlcv_ccxt("kraken", "USDT/USD", cfg.period, timeframe="1h")
                if rdf is not None and not rdf.empty:
                    usdt_usd_close = rdf["close"]
                    log.info("%s: fetched Massive USD spot + USDT/USD rate", sym)
        except Exception as exc:  # noqa: BLE001 — fetch failure must not break 1H line
            log.warning("%s: Massive fetch failed: %s — skipping cross-venue factors", sym, exc)
```

Then pass them into the `build_feature_dict(...)` call (`:598-607`), adding:

```python
            spot_close=spot_close,
            massive_usd_close=massive_usd_close,
            usdt_usd_close=usdt_usd_close,
            orderflow_df=orderflow_df,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_stage0a_features.py -v`
Expected: PASS (existing tests + the new one). The pre-existing derived-keys test that passes only `spot_close` must still pass (cross-venue keys absent there).

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_features.py
git commit -m "feat(stage0a): wire cross-venue premium factor behind None-guards"
```

---

## Task 5: Register the Massive source in `sources.py`

**Files:**
- Modify: `research/lib/sources.py:39-96` (add a `SOURCE_REGISTRY` entry)
- Test: `research/tests/test_derived_factors.py` (append a small registry assertion)

- [ ] **Step 1: Write the failing test**

```python
# append to research/tests/test_derived_factors.py
def test_massive_source_registered():
    from lib.sources import SOURCE_REGISTRY
    spec = SOURCE_REGISTRY["massive_spot_usd"]
    assert spec.status == "available"
    assert spec.category in {"basis", "stablecoin"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_derived_factors.py::test_massive_source_registered -v`
Expected: FAIL with `KeyError: 'massive_spot_usd'`

- [ ] **Step 3: Add the registry entry**

In `research/lib/sources.py`, add an import near `:17-19`:

```python
from lib.massive_data import fetch_spot_bars as _fetch_massive_spot
```

Add inside `SOURCE_REGISTRY` (after the `coingecko_stablecoin_supply` entry, `:64`):

```python
    "massive_spot_usd": SourceSpec(
        fetcher=_fetch_massive_spot,
        status="available",
        description="Massive (Polygon) 真美元現貨 1H bar（跨場域溢價：USDT 脫鉤 + 法幣溢價）",
        category="basis",
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_derived_factors.py::test_massive_source_registered -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/lib/sources.py research/tests/test_derived_factors.py
git commit -m "feat(sources): register massive_spot_usd source"
```

---

## Task 6: `factor_gates.py` — reusable Phase-1 gate helpers

**Files:**
- Create: `research/lib/factor_gates.py`
- Test: `research/tests/test_factor_gates.py`

The entry-lag gate reuses the existing `lib.orderflow_eval.execution_ic` (generic: takes a factor series + price). This task adds the three gates that do not yet exist: partial IC, turnover, and net-of-cost decile spread.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_factor_gates.py
import numpy as np
import pandas as pd

from lib import factor_gates


def _idx(n):
    return pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")


def test_partial_ic_drops_to_zero_when_factor_is_a_control():
    idx = _idx(500)
    rng = np.random.default_rng(0)
    ctrl = pd.Series(rng.normal(size=500), index=idx)
    fwd = pd.Series(rng.normal(size=500), index=idx)
    factor = ctrl.copy()  # factor IS the control => no incremental info
    pic = factor_gates.partial_ic(factor, [ctrl], fwd)
    assert abs(pic) < 0.05


def test_partial_ic_survives_when_orthogonal_signal_present():
    idx = _idx(500)
    rng = np.random.default_rng(1)
    ctrl = pd.Series(rng.normal(size=500), index=idx)
    signal = pd.Series(rng.normal(size=500), index=idx)
    fwd = signal * 0.3 + pd.Series(rng.normal(size=500) * 0.1, index=idx)
    factor = signal + ctrl * 0.0  # carries orthogonal predictive signal
    pic = factor_gates.partial_ic(factor, [ctrl], fwd)
    assert abs(pic) > 0.1


def test_sign_flip_rate_high_for_alternating_series():
    idx = _idx(100)
    alt = pd.Series([1.0, -1.0] * 50, index=idx)
    assert factor_gates.sign_flip_rate(alt) > 0.9
    flat = pd.Series([1.0] * 100, index=idx)
    assert factor_gates.sign_flip_rate(flat) == 0.0


def test_decile_spread_net_of_cost():
    idx = _idx(400)
    rng = np.random.default_rng(2)
    factor = pd.Series(rng.normal(size=400), index=idx)
    fwd = factor * 0.05 + pd.Series(rng.normal(size=400) * 0.001, index=idx)  # strong monotone
    net = factor_gates.decile_spread_net_of_cost(factor, fwd, slippage_bps=7.5)
    assert net > 0  # gross spread beats round-trip cost
    # zero-signal factor => net should be negative after cost
    noise = pd.Series(rng.normal(size=400), index=idx)
    fwd_noise = pd.Series(rng.normal(size=400) * 0.0001, index=idx)
    assert factor_gates.decile_spread_net_of_cost(noise, fwd_noise, slippage_bps=7.5) < 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_factor_gates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lib.factor_gates'`

- [ ] **Step 3: Write the implementation**

```python
# research/lib/factor_gates.py
"""Phase-1 factor validation gates (design spec §6).

Reusable, side-effect-free helpers. The entry-lag gate is NOT here — it reuses
lib.orderflow_eval.execution_ic (already generic). These cover the gates that
did not previously exist: partial IC, turnover, net-of-cost decile spread.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lib.factor_metrics import compute_ic


def partial_ic(
    factor: pd.Series,
    controls: list[pd.Series],
    fwd_ret: pd.Series,
    method: str = "spearman",
) -> float:
    """IC of `factor` residualised against `controls` vs forward return.

    Linearly removes the controls (OLS with intercept) from the factor, then
    computes IC of the residual. A near-zero result means the factor is a
    re-skin of the controls (e.g. funding_z / basis_rel). Spec gate (b).
    """
    cols = {"f": factor, "r": fwd_ret}
    for i, c in enumerate(controls):
        cols[f"c{i}"] = c
    df = pd.concat(cols, axis=1).dropna()
    if len(df) < 50:
        return float("nan")
    X = np.column_stack([np.ones(len(df))] + [df[f"c{i}"].values for i in range(len(controls))])
    beta, *_ = np.linalg.lstsq(X, df["f"].values, rcond=None)
    resid = pd.Series(df["f"].values - X @ beta, index=df.index)
    return compute_ic(resid, df["r"], method=method)


def sign_flip_rate(factor: pd.Series) -> float:
    """Fraction of bars where the factor sign flips (turnover proxy). Spec gate (d).

    High value => the factor oscillates around 0 and fees eat the alpha.
    """
    s = np.sign(factor.dropna())
    if len(s) < 2:
        return float("nan")
    return float((s.diff().abs() > 0).mean())


def decile_spread_net_of_cost(
    factor: pd.Series,
    fwd_ret: pd.Series,
    slippage_bps: float = 7.5,
    q: float = 0.1,
) -> float:
    """|top-decile minus bottom-decile forward return| minus round-trip cost.

    A cheap economic check (spec gate (e)): if this is <= 0 the factor's spread
    does not survive slippage, so Phase 2 is not worth it. `slippage_bps` is the
    one-way cost; round trip = 2x.
    """
    df = pd.concat({"f": factor, "r": fwd_ret}, axis=1).dropna()
    if len(df) < 50:
        return float("nan")
    lo = df["f"].quantile(q)
    hi = df["f"].quantile(1.0 - q)
    top = df.loc[df["f"] >= hi, "r"].mean()
    bot = df.loc[df["f"] <= lo, "r"].mean()
    gross = abs(top - bot)
    cost = 2.0 * slippage_bps / 1e4
    return float(gross - cost)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_factor_gates.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add research/lib/factor_gates.py research/tests/test_factor_gates.py
git commit -m "feat(gates): partial_ic + sign_flip_rate + net-of-cost decile spread"
```

---

## Task 7: Phase-1 measurement driver + runbook (needs API key)

**Files:**
- Create: `research/scripts/xvenue_phase1_gates.py`

This driver is run **once real data exists** (free Massive key obtained). It is not unit-tested (it reads the live feature store + needs the key); all the logic it calls is already unit-tested in Tasks 1/3/6 and `factor_metrics` / `orderflow_eval`.

- [ ] **Step 1: Write the driver script**

```python
# research/scripts/xvenue_phase1_gates.py
"""Phase-1 kill-gate for the cross-venue premium factor (design spec §6).

Prereqs:
  1. MASSIVE_API_KEY set in env.
  2. Feature store + evidence regenerated for btc/eth/sol WITH the Massive fetch
     (run stage0a after backing up research/manifests/{btc,eth,sol}).

Prints, per (symbol, factor in {depeg_z, fiat_prem_z}):
  abs IC, partial IC (controls funding_z+basis_rel), entry-lag IC (lag0 vs lag1),
  sign-flip rate, net-of-cost decile spread.
Then you make the GO/No-Go call against the spec gate thresholds.
"""
from pathlib import Path

import pandas as pd

from lib.factor_metrics import add_forward_returns, compute_ic
from lib.factor_gates import partial_ic, sign_flip_rate, decile_spread_net_of_cost
from lib.orderflow_eval import execution_ic
from pipeline.config import load_config

FACTORS = ["depeg_z", "fiat_prem_z"]
CONTROLS = ["funding_z", "basis_rel"]
HORIZON_H = 24
SYMBOLS = ["btc", "eth", "sol"]
FEATURE_DIR = Path(__file__).resolve().parents[1] / "manifests"


def _load_feature_store(sym: str) -> pd.DataFrame:
    """Load the stored feature parquet for one symbol (path per repo convention)."""
    # Feature store path convention — adjust if dump_features writes elsewhere.
    path = FEATURE_DIR / f"features_{sym}.parquet"
    return pd.read_parquet(path)


def main() -> None:
    cfg = load_config()
    header = f"{'sym/factor':22}{'absIC':>9}{'partIC':>9}{'lag0':>9}{'lag1':>9}{'flip':>7}{'net_bps':>9}"
    print(header)
    print("-" * len(header))
    for sym in SYMBOLS:
        df = _load_feature_store(sym)
        if "close" not in df.columns:
            print(f"{sym}: no close column — skip")
            continue
        df = add_forward_returns(df, "close", [HORIZON_H], interval=cfg.interval)
        ret = df[f"ret_{HORIZON_H}h"]
        controls = [df[c] for c in CONTROLS if c in df.columns]
        for fac in FACTORS:
            if fac not in df.columns:
                print(f"{sym}/{fac:14} MISSING (no coverage?)")
                continue
            f = df[fac]
            abs_ic = compute_ic(f, ret)
            pic = partial_ic(f, controls, ret) if controls else float("nan")
            ic0 = execution_ic(f, df["close"], entry_lag_bars=0, hold_bars=HORIZON_H, min_obs=200)
            ic1 = execution_ic(f, df["close"], entry_lag_bars=1, hold_bars=HORIZON_H, min_obs=200)
            flip = sign_flip_rate(f)
            net = decile_spread_net_of_cost(f, ret, slippage_bps=7.5) * 1e4
            print(f"{sym}/{fac:14}{abs_ic:>9.3f}{pic:>9.3f}{ic0:>9.3f}{ic1:>9.3f}{flip:>7.2f}{net:>9.1f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Runbook — regenerate data + run gates**

```bash
# 1. set key
export MASSIVE_API_KEY=...   # free tier is fine (1H bars, 5 rpm)

# 2. back up manifests (avoid displacing existing candidate sets — spec §8)
cp -r research/manifests research/manifests_bak_pre_xvenue

# 3. regenerate features+evidence for the 3 coins (measurement only, no deploy)
cd research
RESEARCH_ONLY_SYMBOL=btc python -m pipeline.stage0a_features
RESEARCH_ONLY_SYMBOL=eth python -m pipeline.stage0a_features
RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage0a_features

# 4. run the gate table
python scripts/xvenue_phase1_gates.py
```

Expected: a table with one row per (symbol, factor). Confirm `_load_feature_store`'s parquet path matches where `dump_features` actually writes (grep `dump_features` in `stage0a_features.py` if the path differs, and fix the constant).

- [ ] **Step 3: Apply the GO / No-Go decision (spec §6)**

GO to Phase 2 only if a factor clears ALL of:
- **(a) abs IC** ≥ the pipeline evidence bar (`min_abs_ic`, see `factor_selector`).
- **(b) partial IC** still material after controlling funding_z + basis_rel (not collapsed to ~0).
- **(c) entry-lag**: `lag1` does NOT collapse vs `lag0` (collapse ⇒ fast spread, dead like order-flow).
- **(d) sign-flip rate** not pathologically high (turnover would eat alpha).
- **(e) net-of-cost decile spread** > 0.
- **(f)** spot-check IC is not carried by 2-3 depeg days (re-run on a window excluding the largest depeg event, or eyeball a quantile plot).

- [ ] **Step 4: Record the result in memory**

- **GO** → proceed to Phase 2 (separate brainstorm/plan: live freshness + archetype strategy build).
- **No-Go** → write a `project_*` memory file capturing the negative result (which gate failed, the numbers), mirroring prior No-Go records. Restore manifests if needed: `rm -rf research/manifests && mv research/manifests_bak_pre_xvenue research/manifests`.

- [ ] **Step 5: Commit the driver**

```bash
git add research/scripts/xvenue_phase1_gates.py
git commit -m "feat(scripts): Phase-1 cross-venue premium kill-gate driver"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- §3 factor defs (depeg_z, fiat_prem_z, deconfound) → Task 3 ✓
- §4 components 1-4 (massive_data, derived_factors, stage0a, config+sources) → Tasks 1,2,3,4,5 ✓
- §5 data hazards: timestamp alignment → factor reindexes to OKX index (Task 3) + truncation-stability test; **note:** bar-boundary label equality across sources is enforced by both fetchers returning UTC bar-open `t` — Task 1 returns Massive `t` (ms, bar-open) and OKX `fetch_candles` likewise; the driver should assert index intersection is non-trivial before trusting results (add an `assert len(df.dropna()) > 200` guard if paranoid).
- §6 six gates → Task 6 (b,d,e) + reuse execution_ic (c) + compute_ic (a) + driver step 3 (f) ✓
- §7 Phase 2 → explicitly deferred, not in this plan ✓
- §8 impact mitigation (backup manifests) → Task 7 runbook step 2 ✓
- §9 tests → Tasks 1,3,4,6 ✓

**Placeholder scan:** No TBD/TODO. `BASE_URL` is a real default (not a placeholder). The one residual real-world unknown — exact `dump_features` parquet path — is called out explicitly in Task 7 Step 2 with how to resolve it.

**Type consistency:** `cross_venue_premium_factors(okx_spot, massive_usd, usdt_usd)` signature identical across Task 3 (def), Task 4 (call), Task 7 (factors consumed: depeg_z/fiat_prem_z). `fetch_spot_bars(ticker, days, interval)` identical in Tasks 1, 4, 5. `partial_ic`/`sign_flip_rate`/`decile_spread_net_of_cost` signatures identical Task 6 ↔ Task 7.
