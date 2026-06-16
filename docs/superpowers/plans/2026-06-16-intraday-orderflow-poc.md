# Intraday Order-Flow 因子 POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove (or reject) that Binance aggTrades-derived intraday order-flow factors have IC at ETH @ 30m, reusing the existing stage0a/stage1 evaluation machinery.

**Architecture:** A one-time offline ingestion path (downloader + Polars aggregator) produces a cached bar-level order-flow feature parquet. A new `orderflow_factors()` feature family — mirroring the existing `funding_factors`/`oi_factors` — feeds those features into `build_feature_dict`, so the existing stage1 IC/IR/cross-regime/verdict machinery judges them on the same scale as `funding_z`.

**Tech Stack:** Python, Polars (heavy aggregation, lazy/chunked), pandas (factor family, matches existing pipeline), pytest, existing research pipeline (`research/pipeline/stage0a_features.py`, `research/lib/`).

**Spec:** `docs/superpowers/specs/2026-06-16-intraday-orderflow-poc-design.md`

**Test/run convention:** research tests run from the `research/` dir (so `from lib...` / `from pipeline...` imports resolve). Run pytest **separately** from the dashboard suite (combined run hits sys.path collisions). Examples below use `cd research && python -m pytest ...`.

---

## File Structure

| Path | Responsibility | New/Modify |
|---|---|---|
| `research/lib/binance_dump.py` | Download + checksum-verify + cache Binance aggTrades monthly archives. Network-facing. | New |
| `research/lib/orderflow.py` | Aggregate raw aggTrades → per-bar order-flow primitives (Polars). Cache read/write + versioned meta. Pure/offline. | New |
| `research/lib/orderflow_factors.py` | `orderflow_factors()` family: primitives df → 4 named factor Series, bph-scaled. Pure pandas. | New |
| `research/pipeline/stage0a_features.py` | `build_feature_dict` gains `orderflow_df` param + calls family; stage0a `main` loads cache. | Modify |
| `research/tests/test_binance_dump.py` | Downloader tests (mock HTTP). | New |
| `research/tests/test_orderflow.py` | Aggregator + cache tests. | New |
| `research/tests/test_orderflow_factors.py` | Factor family tests. | New |
| `research/tests/test_stage0a_orderflow_integration.py` | End-to-end: fixture parquet → feature_dict → IC entries. | New |
| `.gitignore` | Ignore `research/data/`. | Modify |
| `research/requirements.txt` (or pyproject) | Add `polars`. | Modify |

---

## Task 1: Scaffolding — gitignore + polars dep

**Files:**
- Modify: `.gitignore`
- Modify: `research/requirements.txt` (confirm actual deps file first; could be `pyproject.toml`)

- [ ] **Step 1: Confirm the deps file**

Run: `ls research/requirements.txt research/pyproject.toml pyproject.toml 2>/dev/null`
Use whichever exists for the dependency edit below.

- [ ] **Step 2: Ignore the data dir**

Append to `.gitignore`:

```gitignore
# Order-flow POC runtime data (raw dumps + cached bar-level parquet)
research/data/
```

- [ ] **Step 3: Add polars dependency**

Add `polars>=1.0` to the deps file. If `requirements.txt`: append a line `polars>=1.0`. If `pyproject.toml`: add to the dependencies array.

- [ ] **Step 4: Install + verify import**

Run: `python -c "import polars as pl; print(pl.__version__)"`
Expected: prints a version `>= 1.0`.

- [ ] **Step 5: Commit**

```bash
git add .gitignore research/requirements.txt
git commit -m "chore(orderflow): gitignore data dir + add polars dep"
```

---

## Task 2: Binance aggTrades downloader

**Files:**
- Create: `research/lib/binance_dump.py`
- Test: `research/tests/test_binance_dump.py`

**Reference — URL + schema (verify against a real sample in Step 6):**
- Monthly archive: `https://data.binance.vision/data/futures/um/monthly/aggTrades/ETHUSDT/ETHUSDT-aggTrades-YYYY-MM.zip`
- Checksum: same URL + `.CHECKSUM` (one line: `<sha256>  <filename>`)
- CSV columns (futures UM aggTrades): `agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time, is_buyer_maker`. `transact_time` is epoch **milliseconds**. Header row presence varies by month — handle both.

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_binance_dump.py
import hashlib
from pathlib import Path

import pytest

from lib import binance_dump


def test_build_url_monthly():
    url = binance_dump.aggtrades_url("ETHUSDT", 2025, 6)
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/aggTrades/"
        "ETHUSDT/ETHUSDT-aggTrades-2025-06.zip"
    )


def test_download_skips_when_cached(tmp_path, monkeypatch):
    target = tmp_path / "ETHUSDT-aggTrades-2025-06.zip"
    target.write_bytes(b"already-here")

    def boom(*a, **k):
        raise AssertionError("should not fetch when cached")

    monkeypatch.setattr(binance_dump, "_fetch", boom)
    out = binance_dump.download_month("ETHUSDT", 2025, 6, dest_dir=tmp_path, verify=False)
    assert out == target


def test_download_verifies_checksum(tmp_path, monkeypatch):
    payload = b"fake-zip-bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_fetch(url, dest):
        if url.endswith(".CHECKSUM"):
            dest.write_text(f"{digest}  ETHUSDT-aggTrades-2025-06.zip\n")
        else:
            dest.write_bytes(payload)

    monkeypatch.setattr(binance_dump, "_fetch", fake_fetch)
    out = binance_dump.download_month("ETHUSDT", 2025, 6, dest_dir=tmp_path, verify=True)
    assert out.read_bytes() == payload


def test_download_raises_on_checksum_mismatch(tmp_path, monkeypatch):
    def fake_fetch(url, dest):
        if url.endswith(".CHECKSUM"):
            dest.write_text("deadbeef  ETHUSDT-aggTrades-2025-06.zip\n")
        else:
            dest.write_bytes(b"corrupt")

    monkeypatch.setattr(binance_dump, "_fetch", fake_fetch)
    with pytest.raises(ValueError, match="checksum"):
        binance_dump.download_month("ETHUSDT", 2025, 6, dest_dir=tmp_path, verify=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_binance_dump.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.binance_dump'` (or AttributeError).

- [ ] **Step 3: Write minimal implementation**

```python
# research/lib/binance_dump.py
"""Download + checksum-verify Binance futures aggTrades monthly archives.

Free public data dumps from data.binance.vision. Network-facing; everything
downstream (aggregation, factors) is offline and deterministic.
"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

_BASE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"


def aggtrades_url(symbol: str, year: int, month: int) -> str:
    fname = f"{symbol}-aggTrades-{year:04d}-{month:02d}.zip"
    return f"{_BASE}/{symbol}/{fname}"


def _fetch(url: str, dest: Path) -> None:
    """Download `url` to `dest`. Isolated for test monkeypatching."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (trusted host)
        dest.write_bytes(resp.read())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_month(
    symbol: str, year: int, month: int, dest_dir: Path, verify: bool = True
) -> Path:
    """Download one month's aggTrades zip into `dest_dir` (idempotent).

    Returns the cached zip path. Raises ValueError on checksum mismatch.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{symbol}-aggTrades-{year:04d}-{month:02d}.zip"
    target = dest_dir / fname
    if target.exists():
        return target

    url = aggtrades_url(symbol, year, month)
    _fetch(url, target)

    if verify:
        chk = dest_dir / (fname + ".CHECKSUM")
        _fetch(url + ".CHECKSUM", chk)
        expected = chk.read_text().split()[0].strip()
        actual = _sha256(target)
        if actual != expected:
            target.unlink(missing_ok=True)
            raise ValueError(
                f"checksum mismatch for {fname}: expected {expected}, got {actual}"
            )
    return target
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_binance_dump.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/binance_dump.py research/tests/test_binance_dump.py
git commit -m "feat(orderflow): binance aggTrades downloader with checksum verify"
```

- [ ] **Step 6: Real-sample schema verification (manual, run once)**

Run: `cd research && python -c "from lib import binance_dump; from pathlib import Path; p=binance_dump.download_month('ETHUSDT',2025,6,Path('data/raw/binance')); import zipfile; z=zipfile.ZipFile(p); n=z.namelist()[0]; print(n); print(z.open(n).readline()[:200])"`
Expected: prints the CSV filename and first line. **Confirm column order + whether a header row exists** before Task 3. If the schema differs from the Reference above, note it and adjust Task 3's column mapping.

---

## Task 3: Aggregator — raw aggTrades → per-bar primitives

**Files:**
- Create: `research/lib/orderflow.py`
- Test: `research/tests/test_orderflow.py`

**Per-bar primitive columns produced:** `ts` (bar open, UTC, tz-aware), `buy_vol`, `sell_vol`, `buy_count`, `sell_count`, `total_vol`, `open`, `close`, and **four USD-notional bucket volumes** `vol_lt10k`, `vol_10_50k`, `vol_50_200k`, `vol_gt200k` (per-trade USD notional = `price * quantity`; the bucket value is the summed ETH `quantity`). Bucketing trades into bars is `[T, T+interval)` left-closed, right-open. The USD-notional binning is single-pass and look-ahead-safe; "large trade" is composed downstream (Task 5) by summing buckets above a chosen edge — so the threshold can be re-tuned without re-scanning raw. Bucket edges are module constants `BUCKET_EDGES_USD = (10_000, 50_000, 200_000)`.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_orderflow.py
import polars as pl
import pytest

from lib import orderflow


def _trades(rows):
    """rows: list of (transact_time_ms, price, quantity, is_buyer_maker)."""
    return pl.DataFrame(
        {
            "transact_time": [r[0] for r in rows],
            "price": [r[1] for r in rows],
            "quantity": [r[2] for r in rows],
            "is_buyer_maker": [r[3] for r in rows],
        }
    )


# 2025-01-01T00:00:00Z = 1735689600000 ms
T0 = 1735689600000
MIN = 60_000
HALF_HOUR = 30 * MIN


def test_buy_sell_split_by_is_buyer_maker():
    # is_buyer_maker=False -> aggressive BUY; True -> aggressive SELL
    df = _trades([
        (T0, 100.0, 2.0, False),   # buy 2
        (T0 + MIN, 100.0, 1.0, True),  # sell 1
    ])
    out = orderflow.aggregate(df, "30m")
    row = out.row(0, named=True)
    assert row["buy_vol"] == pytest.approx(2.0)
    assert row["sell_vol"] == pytest.approx(1.0)
    assert row["buy_count"] == 1
    assert row["sell_count"] == 1
    assert row["total_vol"] == pytest.approx(3.0)


def test_bucket_is_left_closed_right_open():
    # a trade exactly at T0+30m must fall in the SECOND bar, not the first
    df = _trades([
        (T0, 100.0, 1.0, False),
        (T0 + HALF_HOUR, 100.0, 5.0, False),  # boundary -> next bar
    ])
    out = orderflow.aggregate(df, "30m").sort("ts")
    assert out.height == 2
    assert out.row(0, named=True)["buy_vol"] == pytest.approx(1.0)
    assert out.row(1, named=True)["buy_vol"] == pytest.approx(5.0)


def test_future_trades_do_not_change_past_bar():
    base = _trades([(T0, 100.0, 1.0, False)])
    extended = _trades([
        (T0, 100.0, 1.0, False),
        (T0 + HALF_HOUR, 100.0, 9.0, True),  # future bar
    ])
    b0 = orderflow.aggregate(base, "30m").sort("ts").row(0, named=True)
    e0 = orderflow.aggregate(extended, "30m").sort("ts").row(0, named=True)
    assert b0 == e0  # first bar identical regardless of later data


def test_duplicate_agg_trade_ids_dropped():
    df = pl.DataFrame({
        "agg_trade_id": [1, 1, 2],
        "transact_time": [T0, T0, T0 + MIN],
        "price": [100.0, 100.0, 100.0],
        "quantity": [1.0, 1.0, 2.0],
        "is_buyer_maker": [False, False, False],
    })
    out = orderflow.aggregate(df, "30m")
    assert out.row(0, named=True)["buy_vol"] == pytest.approx(3.0)  # 1 + 2, dup ignored


def test_empty_bar_has_no_zero_division_and_open_close_set():
    df = _trades([(T0, 100.0, 2.0, False), (T0 + MIN, 110.0, 2.0, True)])
    out = orderflow.aggregate(df, "30m").row(0, named=True)
    assert out["open"] == pytest.approx(100.0)
    assert out["close"] == pytest.approx(110.0)


def test_usd_notional_buckets():
    # price 2000 USD/ETH: 30 ETH = $60k -> 50_200k bucket; 2 ETH = $4k -> lt10k
    df = _trades([
        (T0, 2000.0, 30.0, False),   # $60k -> vol_50_200k
        (T0 + MIN, 2000.0, 2.0, False),  # $4k -> vol_lt10k
        (T0 + 2 * MIN, 2000.0, 200.0, True),  # $400k -> vol_gt200k
    ])
    out = orderflow.aggregate(df, "30m").row(0, named=True)
    assert out["vol_lt10k"] == pytest.approx(2.0)
    assert out["vol_50_200k"] == pytest.approx(30.0)
    assert out["vol_gt200k"] == pytest.approx(200.0)
    assert out["vol_10_50k"] == pytest.approx(0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_orderflow.py -v`
Expected: FAIL — `AttributeError: module 'lib.orderflow' has no attribute 'aggregate'`.

- [ ] **Step 3: Write minimal implementation**

```python
# research/lib/orderflow.py
"""Aggregate Binance aggTrades into per-bar order-flow primitives (Polars).

Offline + deterministic. Bucketing is [T, T+interval) left-closed/right-open
(no look-ahead). is_buyer_maker semantics: False => aggressive BUY (taker is
buyer, fill at ask); True => aggressive SELL (taker is seller, fill at bid).
"""
from __future__ import annotations

import polars as pl

# USD-notional bucket edges (single-pass, look-ahead-safe). "Large trade" is
# composed downstream by summing buckets above a chosen edge (Task 5), so the
# threshold re-tunes without re-scanning raw.
BUCKET_EDGES_USD = (10_000.0, 50_000.0, 200_000.0)

_INTERVAL_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1H": 60 * 60_000}


def aggregate(trades: pl.DataFrame, interval: str) -> pl.DataFrame:
    """raw aggTrades -> per-bar primitives. `trades` needs columns
    transact_time (ms), price, quantity, is_buyer_maker (+ optional agg_trade_id)."""
    step = _INTERVAL_MS[interval]
    df = trades
    if "agg_trade_id" in df.columns:
        df = df.unique(subset=["agg_trade_id"], keep="first")
    df = df.sort("transact_time")  # open/close depend on time order

    e1, e2, e3 = BUCKET_EDGES_USD
    df = df.with_columns(
        ((pl.col("transact_time") // step) * step).alias("_bar_ms"),
        (~pl.col("is_buyer_maker")).alias("_is_buy"),
        (pl.col("price") * pl.col("quantity")).alias("_usd"),
    )

    out = (
        df.group_by("_bar_ms")
        .agg(
            pl.col("quantity").filter(pl.col("_is_buy")).sum().alias("buy_vol"),
            pl.col("quantity").filter(~pl.col("_is_buy")).sum().alias("sell_vol"),
            pl.col("_is_buy").filter(pl.col("_is_buy")).count().alias("buy_count"),
            (~pl.col("_is_buy")).filter(~pl.col("_is_buy")).count().alias("sell_count"),
            pl.col("quantity").sum().alias("total_vol"),
            pl.col("price").first().alias("open"),
            pl.col("price").last().alias("close"),
            pl.col("quantity").filter(pl.col("_usd") < e1).sum().alias("vol_lt10k"),
            pl.col("quantity").filter((pl.col("_usd") >= e1) & (pl.col("_usd") < e2)).sum().alias("vol_10_50k"),
            pl.col("quantity").filter((pl.col("_usd") >= e2) & (pl.col("_usd") < e3)).sum().alias("vol_50_200k"),
            pl.col("quantity").filter(pl.col("_usd") >= e3).sum().alias("vol_gt200k"),
        )
        .with_columns(
            pl.col("_bar_ms").cast(pl.Datetime("ms")).dt.replace_time_zone("UTC").alias("ts"),
            *[pl.col(c).fill_null(0.0) for c in
              ("buy_vol", "sell_vol", "vol_lt10k", "vol_10_50k", "vol_50_200k", "vol_gt200k")],
        )
        .drop("_bar_ms")
        .sort("ts")
    )
    return out
```

Note: `df.sort("transact_time")` (included above) makes `price.first()`/`last()` give true bar open/close.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_orderflow.py -v`
Expected: PASS (6 passed). If `open`/`close` ordering fails, add the `sort("transact_time")` noted above.

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow.py research/tests/test_orderflow.py
git commit -m "feat(orderflow): aggregate aggTrades to per-bar primitives"
```

---

## Task 4: Cache write/read with versioned meta + streaming driver

**Files:**
- Modify: `research/lib/orderflow.py` (add cache + meta + a chunked monthly driver)
- Test: `research/tests/test_orderflow.py` (append)

**Meta fields:** `version` (int, bump on logic change), `git_sha`, `logic_hash` (sha256 of the aggregator source), `interval`, `symbol`, `generated_at`, `months`, `raw_checksums`, `factor_columns`. Loader raises `ValueError` if `version != AGG_VERSION`.

- [ ] **Step 1: Write the failing tests**

```python
# append to research/tests/test_orderflow.py
import json


def test_cache_roundtrip_writes_meta(tmp_path):
    df = orderflow.aggregate(_trades([(T0, 100.0, 1.0, False)]), "30m")
    p = orderflow.write_cache(df, "eth", "30m", dest_dir=tmp_path)
    meta = json.loads(p.with_suffix(".meta.json").read_text())
    assert meta["version"] == orderflow.AGG_VERSION
    assert meta["interval"] == "30m"
    assert meta["symbol"] == "eth"
    assert "logic_hash" in meta and "git_sha" in meta
    loaded = orderflow.read_cache("eth", "30m", dest_dir=tmp_path)
    assert loaded.height == df.height


def test_read_cache_rejects_version_mismatch(tmp_path, monkeypatch):
    df = orderflow.aggregate(_trades([(T0, 100.0, 1.0, False)]), "30m")
    p = orderflow.write_cache(df, "eth", "30m", dest_dir=tmp_path)
    meta_path = p.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["version"] = orderflow.AGG_VERSION + 99
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="version"):
        orderflow.read_cache("eth", "30m", dest_dir=tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_orderflow.py -k cache -v`
Expected: FAIL — `AttributeError: ... has no attribute 'write_cache'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to research/lib/orderflow.py
import hashlib as _hashlib
import json as _json
import subprocess as _subprocess
from datetime import datetime, timezone
from pathlib import Path

AGG_VERSION = 1


def _git_sha() -> str:
    try:
        return _subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, text=True
        ).strip()
    except Exception:
        return "unknown"


def _logic_hash() -> str:
    return _hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def _cache_path(symbol: str, interval: str, dest_dir: Path) -> Path:
    return Path(dest_dir) / f"of_{symbol}_{interval}_v{AGG_VERSION}.parquet"


def write_cache(df: pl.DataFrame, symbol: str, interval: str, dest_dir: Path,
                months: list | None = None, raw_checksums: dict | None = None) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path(symbol, interval, dest_dir)
    df.write_parquet(p)
    meta = {
        "version": AGG_VERSION,
        "git_sha": _git_sha(),
        "logic_hash": _logic_hash(),
        "interval": interval,
        "symbol": symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months": months or [],
        "raw_checksums": raw_checksums or {},
        "factor_columns": [c for c in df.columns if c != "ts"],
    }
    p.with_suffix(".meta.json").write_text(_json.dumps(meta, indent=2))
    return p


def read_cache(symbol: str, interval: str, dest_dir: Path) -> pl.DataFrame:
    p = _cache_path(symbol, interval, dest_dir)
    meta = _json.loads(p.with_suffix(".meta.json").read_text())
    if meta.get("version") != AGG_VERSION:
        raise ValueError(
            f"cache version mismatch for {p.name}: meta={meta.get('version')} "
            f"expected={AGG_VERSION} — re-run aggregation"
        )
    return pl.read_parquet(p)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_orderflow.py -k cache -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow.py research/tests/test_orderflow.py
git commit -m "feat(orderflow): versioned parquet cache with meta + version guard"
```

---

## Task 5: `orderflow_factors()` feature family

**Files:**
- Create: `research/lib/orderflow_factors.py`
- Test: `research/tests/test_orderflow_factors.py`

**Output Series (aligned to candle_idx, pandas):**
- `trade_imbalance` = (buy_vol − sell_vol) / total_vol, NaN when total_vol == 0
- `trade_count_imbalance` = (buy_count − sell_count) / (buy_count + sell_count), NaN when 0
- `large_trade_ratio` = (sum of `LARGE_BUCKETS`) / total_vol, NaN when total_vol == 0. Default `LARGE_BUCKETS = ("vol_50_200k", "vol_gt200k")` → "large" = trades > $50k USD notional.
- `price_impact` = (close − open) / total_vol, NaN when total_vol == 0

(All four are already stationary/bounded-ish; no z-score needed for the imbalance/ratio. `price_impact` may be z-scored by the IC-eval transform layer if needed — keep raw here.)

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_orderflow_factors.py
import numpy as np
import pandas as pd
import pytest

from lib.orderflow_factors import orderflow_factors


def _of_df(ts, **cols):
    return pd.DataFrame({"ts": ts, **cols}).set_index("ts")


def test_trade_imbalance_basic():
    idx = pd.to_datetime(["2025-01-01 00:00", "2025-01-01 00:30"], utc=True)
    of = _of_df(idx, buy_vol=[3.0, 1.0], sell_vol=[1.0, 1.0],
               buy_count=[2, 1], sell_count=[1, 1],
               total_vol=[4.0, 2.0],
               vol_lt10k=[2.0, 2.0], vol_10_50k=[0.0, 0.0],
               vol_50_200k=[2.0, 0.0], vol_gt200k=[0.0, 0.0],
               open=[100.0, 101.0], close=[101.0, 101.0])
    out = orderflow_factors(of, idx, "30m")
    assert out["trade_imbalance"].iloc[0] == pytest.approx(0.5)   # (3-1)/4
    assert out["trade_count_imbalance"].iloc[0] == pytest.approx(1/3)  # (2-1)/3
    assert out["large_trade_ratio"].iloc[0] == pytest.approx(0.5)  # (2+0)/4, edge >$50k
    assert out["price_impact"].iloc[0] == pytest.approx(0.25)      # (101-100)/4


def test_zero_volume_bar_yields_nan_not_inf():
    idx = pd.to_datetime(["2025-01-01 00:00"], utc=True)
    of = _of_df(idx, buy_vol=[0.0], sell_vol=[0.0], buy_count=[0], sell_count=[0],
               total_vol=[0.0], vol_lt10k=[0.0], vol_10_50k=[0.0],
               vol_50_200k=[0.0], vol_gt200k=[0.0], open=[100.0], close=[100.0])
    out = orderflow_factors(of, idx, "30m")
    assert np.isnan(out["trade_imbalance"].iloc[0])
    assert np.isnan(out["trade_count_imbalance"].iloc[0])
    assert not np.isinf(out["price_impact"].iloc[0])


def test_reindexes_to_candle_index():
    of_idx = pd.to_datetime(["2025-01-01 00:00"], utc=True)
    of = _of_df(of_idx, buy_vol=[2.0], sell_vol=[0.0], buy_count=[1], sell_count=[0],
               total_vol=[2.0], vol_lt10k=[2.0], vol_10_50k=[0.0],
               vol_50_200k=[0.0], vol_gt200k=[0.0], open=[100.0], close=[100.0])
    candle_idx = pd.to_datetime(
        ["2025-01-01 00:00", "2025-01-01 00:30"], utc=True
    )  # second bar absent in `of`
    out = orderflow_factors(of, candle_idx, "30m")
    assert list(out["trade_imbalance"].index) == list(candle_idx)
    assert np.isnan(out["trade_imbalance"].iloc[1])  # missing bar -> NaN, no ffill
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_orderflow_factors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.orderflow_factors'`.

- [ ] **Step 3: Write minimal implementation**

```python
# research/lib/orderflow_factors.py
"""Order-flow feature family: per-bar primitives -> named factor Series.

Mirrors funding_factors / oi_factors. Pure pandas, aligned to candle index.
Intraday-native: every factor is computed from within-bar trades, so it is
genuinely aligned with no ffill (unlike funding/stablecoin).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# Default "large trade" = > $50k USD notional == these two bucket columns.
LARGE_BUCKETS = ("vol_50_200k", "vol_gt200k")


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den.where(den != 0, np.nan)


def orderflow_factors(
    of_df: pd.DataFrame, candle_idx: pd.DatetimeIndex, interval: str,
    large_buckets: tuple[str, ...] = LARGE_BUCKETS,
) -> dict[str, pd.Series]:
    """of_df indexed by bar-start UTC with columns buy_vol/sell_vol/buy_count/
    sell_count/total_vol/open/close + USD-notional bucket volumes
    (vol_lt10k/vol_10_50k/vol_50_200k/vol_gt200k). Returns 4 factor Series
    reindexed to candle_idx (missing bars -> NaN, never ffill). `large_buckets`
    selects which buckets count as "large" (default > $50k)."""
    df = of_df.reindex(candle_idx)

    buy_v, sell_v = df["buy_vol"], df["sell_vol"]
    buy_c, sell_c = df["buy_count"], df["sell_count"]
    total = df["total_vol"]
    large_v = df[list(large_buckets)].sum(axis=1)

    feats = {
        "trade_imbalance": _safe_div(buy_v - sell_v, total),
        "trade_count_imbalance": _safe_div(buy_c - sell_c, buy_c + sell_c),
        "large_trade_ratio": _safe_div(large_v, total),
        "price_impact": _safe_div(df["close"] - df["open"], total),
    }
    for name, s in feats.items():
        s.name = name
    return feats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_orderflow_factors.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add research/lib/orderflow_factors.py research/tests/test_orderflow_factors.py
git commit -m "feat(orderflow): orderflow_factors feature family"
```

---

## Task 6: Wire into stage0a `build_feature_dict` + integration test

**Files:**
- Modify: `research/pipeline/stage0a_features.py` (`build_feature_dict` signature + body; `main` loader)
- Test: `research/tests/test_stage0a_orderflow_integration.py`

- [ ] **Step 1: Write the failing integration test**

```python
# research/tests/test_stage0a_orderflow_integration.py
import pandas as pd
import pytest

from pipeline.stage0a_features import build_feature_dict, compute_evidence_entries
from pipeline.config import load_config


def _candles(n=200):
    idx = pd.date_range("2025-01-01", periods=n, freq="30min", tz="UTC")
    price = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    return pd.DataFrame(
        {"open": price, "high": price + 1, "low": price - 1,
         "close": price, "volume": 10.0}, index=idx
    )


def _orderflow_df(idx):
    # alternating imbalance so the factor has variance
    import numpy as np
    sign = np.where(np.arange(len(idx)) % 2 == 0, 1.0, -1.0)
    return pd.DataFrame(
        {
            "buy_vol": 5.0 + sign, "sell_vol": 5.0 - sign,
            "buy_count": 5, "sell_count": 5,
            "total_vol": 10.0,
            "vol_lt10k": 8.0, "vol_10_50k": 0.0,
            "vol_50_200k": 2.0, "vol_gt200k": 0.0,
            "open": 100.0, "close": 100.0 + sign,
        },
        index=idx,
    )


def test_orderflow_features_enter_feature_dict_and_get_ic():
    candles = _candles()
    of = _orderflow_df(candles.index)
    cfg = load_config()  # default config; interval label irrelevant for the dict
    feats = build_feature_dict(candles, cfg, orderflow_df=of)
    assert "trade_imbalance" in feats
    assert "price_impact" in feats

    entries = compute_evidence_entries(
        candles, feats, horizons_h=(1, 2, 4), interval="30m"
    )
    keys = {e["feature_key"] for e in entries}
    assert "trade_imbalance" in keys  # got IC-evaluated by the existing machinery
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd research && python -m pytest tests/test_stage0a_orderflow_integration.py -v`
Expected: FAIL — `TypeError: build_feature_dict() got an unexpected keyword argument 'orderflow_df'`.

- [ ] **Step 3: Modify `build_feature_dict`**

In `research/pipeline/stage0a_features.py`, add the param to the signature:

```python
def build_feature_dict(
    candles: pd.DataFrame,
    config: ResearchConfig,
    funding_df: pd.DataFrame | None = None,
    oi_df: pd.DataFrame | None = None,
    stablecoin_df: pd.DataFrame | None = None,
    spot_close: pd.Series | None = None,
    orderflow_df: pd.DataFrame | None = None,
) -> dict[str, pd.Series]:
```

Add the import near the other family imports at the top of the file:

```python
from lib.orderflow_factors import orderflow_factors
```

Inside the function, just before `return features`, add:

```python
    # ── Order-flow factor family (intraday-native, no ffill) ─────────────────
    if orderflow_df is not None and not orderflow_df.empty:
        features.update(orderflow_factors(orderflow_df, candle_idx, config.interval))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd research && python -m pytest tests/test_stage0a_orderflow_integration.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Wire the loader into stage0a `main`**

In `stage0a_features.py` `main` (near where `funding_df`/`oi_df` are loaded before the `build_feature_dict(...)` call around line 501), load the cached order-flow parquet when present and pass it. Add:

```python
    # Order-flow cache (POC): research/data/orderflow/of_<sym>_<iv>_vN.parquet
    orderflow_df = None
    try:
        from lib import orderflow as _of
        of_dir = _REPO_ROOT / "research" / "data" / "orderflow"
        of_pl = _of.read_cache(config.symbol_key_or_name, config.interval, of_dir)  # adapt to actual symbol accessor
        orderflow_df = of_pl.to_pandas().set_index("ts")
    except (FileNotFoundError, ValueError):
        orderflow_df = None  # absent cache -> feature simply not present (1H runs unaffected)
```

Then pass `orderflow_df=orderflow_df` into the `build_feature_dict(...)` call.

Note: replace `config.symbol_key_or_name` with the actual symbol accessor used elsewhere in `main` (check how `funding_df` keys its symbol). Confirm `_REPO_ROOT` is the module's existing repo-root constant (it is referenced at ~line 559 per spec context).

- [ ] **Step 6: Run the full stage0a test module to confirm no regression**

Run: `cd research && python -m pytest tests/test_stage0a_features.py tests/test_stage0a_orderflow_integration.py -v`
Expected: PASS (all existing stage0a tests + the new integration test). 1H runs are unaffected because the cache is absent → `orderflow_df` stays None.

- [ ] **Step 7: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_orderflow_integration.py
git commit -m "feat(orderflow): feed order-flow family into stage0a build_feature_dict"
```

---

## Task 7: End-to-end POC run + go/no-go readout (runbook, manual)

**Files:** none (operational). Run on a machine with disk for the raw dumps (tens of GB).

- [ ] **Step 1: Download 12 months of ETH aggTrades**

```bash
cd research && python -c "
from pathlib import Path
from lib import binance_dump
dest = Path('data/raw/binance')
for y, m in [(2025, mm) for mm in range(6, 13)] + [(2026, mm) for mm in range(1, 6)]:
    print('downloading', y, m); binance_dump.download_month('ETHUSDT', y, m, dest)
"
```
Expected: 12 zip files in `research/data/raw/binance/`. (Adjust the year/month list to the actual trailing 12 months.)

- [ ] **Step 2: Aggregate to the bar-level cache (chunked)**

```bash
cd research && python -c "
import zipfile, polars as pl
from pathlib import Path
from lib import orderflow
raw = sorted(Path('data/raw/binance').glob('ETHUSDT-aggTrades-*.zip'))
frames = []
for z in raw:
    zf = zipfile.ZipFile(z); name = zf.namelist()[0]
    # has_header: confirmed in Task 2 Step 6; set accordingly
    df = pl.read_csv(zf.open(name), has_header=True,
                     columns=['transact_time','price','quantity','is_buyer_maker','agg_trade_id'])
    frames.append(orderflow.aggregate(df, '30m'))
bars = pl.concat(frames).unique(subset=['ts'], keep='last').sort('ts')
orderflow.write_cache(bars, 'eth', '30m', Path('data/orderflow'))
print('rows', bars.height)
"
```
Expected: ~17000 rows (12mo × ~48 bars/day). Cache + meta written to `research/data/orderflow/`.
Note: if a month is large, aggregate per-zip (as above) rather than concatenating raw — each `aggregate` call collapses a month to ~1440 bars, so the concat is tiny.

- [ ] **Step 3: Run stage0a + stage1 at 30m for ETH**

Via the dashboard pipeline (UI or curl), or CLI:

```bash
cd .. && RESEARCH_INTERVAL=30m RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.stage0a_features
RESEARCH_INTERVAL=30m RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.stage0_discovery
RESEARCH_INTERVAL=30m RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.stage1_factors
```
Expected: `research/manifests/30m/factor_eth.json` regenerated, now including the order-flow factors.

- [ ] **Step 4: Read the go/no-go**

```bash
curl -s 'localhost/api/factor-analysis?interval=30m' | python -m json.tool
```
Or open the dashboard Factors page → interval 30m → ETH.

**Decision:**
- **Go**: any order-flow factor has `|IC| >= 0.03` with cross-regime not flipping sign (verdict `single_use`/`ensemble_only`). → proceed to robust ingestion / 15m / strategy stages / evaluate paid L2.
- **No-Go**: all order-flow factors `reject`. → stop; intraday microstructure at the trades-only level has no alpha; do not buy L2.

- [ ] **Step 5: Sanity — collinearity check (before trusting IC)**

```bash
cd research && python -c "
import polars as pl
from pathlib import Path
from lib import orderflow
from lib.orderflow_factors import orderflow_factors
of = orderflow.read_cache('eth','30m',Path('data/orderflow')).to_pandas().set_index('ts')
feats = orderflow_factors(of, of.index, '30m')
import pandas as pd
print(pd.DataFrame(feats).corr())
"
```
Expected: pairwise |corr| < 0.85. If two factors are collinear, drop one before interpreting IC.

---

## Plan Self-Review Notes

- **Spec coverage:** §2 architecture → Tasks 3–6; §3 components → Tasks 2–6; §4 factors → Task 5; §5 storage/versioning → Tasks 1,4; §6 tests → every task; §7 engineering pitfalls (memory/tz/dedup/empty-bar) → Tasks 3,4; §8 go/no-go → Task 7. Component 5 (sources.py registry entry) is **deliberately omitted** — spec §3 marks it POC-optional (features get IC'd via the dict, not via stage0 candidates); add it only when promoting to discovery.
- **Resolved decision (user + gemini, 2026-06-16):** `large_trade_ratio` uses **USD-notional bar-level binning**, not a rolling per-trade quantile (spec §4 original) nor a fixed ETH-count threshold. Aggregator emits four single-pass, look-ahead-safe USD buckets (`<$10k / $10-50k / $50-200k / >$200k`, edges `BUCKET_EDGES_USD`); the family composes "large" by summing buckets above a chosen edge, default **> $50k** (`LARGE_BUCKETS = ("vol_50_200k","vol_gt200k")`). This re-tunes the large threshold without re-scanning raw, and USD-denominates it so it doesn't drift with ETH price. Rolling-quantile remains a possible later refinement.
- **Unverified externals (confirm during Task 2 Step 6):** exact aggTrades CSV column order + header-row presence; Polars `group_by` ordering for `open`/`close` (mitigated by the `sort` note in Task 3 Step 3).
