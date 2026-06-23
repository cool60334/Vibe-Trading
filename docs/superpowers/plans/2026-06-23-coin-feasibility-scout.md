# Coin Feasibility Scout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic CLI that probes free-data-source coverage (OKX OHLCV, OKX funding, Binance daily-metrics archive) for candidate coins and emits a GO/PARTIAL/NO_GO report, so only viable coins get onboarded to `research_config.yaml`.

**Architecture:** Pure scoring/serialization logic + thin network probes in `research/lib/coin_scout.py`; one-line HEAD existence helper added to `research/lib/binance_dump.py`; thin argparse CLI in `research/pipeline/coin_scout.py`. Network lives only in the `probe_*` functions and `metrics_day_exists`; everything else is pure and unit-tested with mocks.

**Tech Stack:** Python 3, pandas, urllib (binance_dump), requests (okx_data), pytest + monkeypatch. No new dependencies.

---

## Spec

`docs/superpowers/specs/2026-06-23-coin-feasibility-scout-design.md`

## Deviation from spec (intentional, DRY)

Spec's `SourceCoverage.ok` field is **dropped**. `available` already means "probe succeeded and data present"; a failed probe sets `available=False` + `error`. Keeping a separate `ok` would duplicate `available`. Fields: `available`, `earliest`, `depth_days`, `error`.

## Post-implementation correction (2026-06-23)

Live smoke found all coins PARTIAL because the funding probe used `okx_data.fetch_funding_history`, which the OKX public endpoint caps at ~99 days. The pipeline (stage0a) ingests funding from **ccxt Binance** (multi-year). Fix: `probe_okx_funding(okx_swap)` → `probe_funding(ccxt_symbol)` calling `ccxt_data.fetch_funding_rate_history_ccxt(exchange_name="binance", symbol=ccxt_symbol)`; `CoinVerdict.okx_funding` field + JSON key + table column renamed to `funding`. Task 5 code below shows the original OKX version — see `research/lib/coin_scout.py` for the shipped ccxt version.

## Conventions (read before starting)

- Research lib modules import siblings as `from lib import okx_data` (see `research/lib/oi_metrics.py`).
- A `python -m research.pipeline.<module>` entrypoint must bootstrap `research/` onto `sys.path` (see `research/pipeline/build_dashboard_manifest.py:33-37`).
- **Run research tests from the `research/` dir** so CWD lands on `sys.path`:
  `cd research && python -m pytest tests/<file> -v`
  (Repo-root `pyproject.toml` pytest config targets `agent/` only; research + dashboard suites must run separately — sys.path conflict.)
- Commit message trailer (every commit): `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

## File Structure

| File | Responsibility |
|---|---|
| `research/lib/binance_dump.py` (modify) | add `metrics_day_exists()` HEAD probe next to `metrics_url` |
| `research/lib/coin_scout.py` (create) | dataclasses, `tickers_for`, `score_coin`, `_earliest_archive_day`, `probe_*`, `scout_coins`, serializers |
| `research/pipeline/coin_scout.py` (create) | argparse CLI: parse `--coins/--out`, call `scout_coins`, write JSON, print table + GO YAML |
| `research/tests/test_binance_dump.py` (modify) | tests for `metrics_day_exists` |
| `research/tests/test_coin_scout.py` (create) | tests for lib + CLI |

---

### Task 1: `metrics_day_exists` HEAD probe (binance_dump)

**Files:**
- Modify: `research/lib/binance_dump.py` (add after `metrics_url`, ~line 97)
- Test: `research/tests/test_binance_dump.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `research/tests/test_binance_dump.py`:

```python
def test_metrics_day_exists_true(monkeypatch):
    """HEAD 200 -> the day's metrics zip exists upstream."""
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        return FakeResp()
    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    assert binance_dump.metrics_day_exists("BTCUSDT", date(2022, 1, 1)) is True
    assert captured["method"] == "HEAD"  # cheap: no body download


def test_metrics_day_exists_false_on_404(monkeypatch):
    """A 404 means Binance has not published that day/symbol -> False (not an error)."""
    def fake_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    assert binance_dump.metrics_day_exists("ZZZUSDT", date(2022, 1, 1)) is False


def test_metrics_day_exists_reraises_non_404(monkeypatch):
    """A 500 is a probe failure, not 'absent' -> propagate so callers can tell them apart."""
    def fake_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 500, "Server Error", {}, None)
    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(HTTPError):
        binance_dump.metrics_day_exists("BTCUSDT", date(2022, 1, 1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_binance_dump.py -k metrics_day_exists -v`
Expected: FAIL — `AttributeError: module 'lib.binance_dump' has no attribute 'metrics_day_exists'`

- [ ] **Step 3: Implement `metrics_day_exists`**

In `research/lib/binance_dump.py`, add immediately after `metrics_url` (after line 97):

```python
def metrics_day_exists(symbol: str, day: date, timeout: float = _FETCH_TIMEOUT) -> bool:
    """True if the daily-metrics zip for `symbol` on `day` exists upstream.

    Cheap HEAD probe (no body download) for the feasibility scout. A 404 means
    Binance has not published that day/symbol and returns False; any other HTTP
    or network error propagates so callers can distinguish 'absent' from
    'probe failed'. Isolated for test monkeypatching (mirrors :func:`_fetch`).
    """
    req = urllib.request.Request(metrics_url(symbol, day), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status == 200
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_binance_dump.py -k metrics_day_exists -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/binance_dump.py research/tests/test_binance_dump.py
git commit -m "feat(binance-dump): metrics_day_exists HEAD probe

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: coin_scout module skeleton — constants, `SourceCoverage`, `tickers_for`

**Files:**
- Create: `research/lib/coin_scout.py`
- Test: `research/tests/test_coin_scout.py`

- [ ] **Step 1: Write the failing tests**

Create `research/tests/test_coin_scout.py`:

```python
# research/tests/test_coin_scout.py
from datetime import date

from lib import coin_scout
from lib.coin_scout import SourceCoverage


def test_tickers_for_derives_all_three():
    assert coin_scout.tickers_for("bnb") == (
        "BNB-USDT-SWAP", "BNB/USDT:USDT", "BNBUSDT",
    )


def test_tickers_for_is_case_insensitive():
    assert coin_scout.tickers_for("XrP") == (
        "XRP-USDT-SWAP", "XRP/USDT:USDT", "XRPUSDT",
    )


def test_source_coverage_holds_fields():
    c = SourceCoverage(available=True, earliest=date(2022, 1, 1), depth_days=900, error=None)
    assert c.available and c.depth_days == 900 and c.error is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_coin_scout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.coin_scout'`

- [ ] **Step 3: Create the module skeleton**

Create `research/lib/coin_scout.py`:

```python
"""Coin feasibility scout: probe free data-source coverage before onboarding a
coin into the research pipeline.

Probes three sources the pipeline depends on — OKX OHLCV, OKX funding, and the
Binance daily-metrics archive (OI + positioning factors) — for earliest date and
depth, then scores each coin GO / PARTIAL / NO_GO. Read-only and cheap (existence
+ earliest-date only). Network lives in the ``probe_*`` functions (which call
``lib.okx_data`` / ``lib.binance_dump``); scoring and serialization are pure.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

from lib import binance_dump, okx_data

# ── thresholds (module constants; tune after the first live run) ─────────────
MIN_OHLCV_DAYS = 365       # OKX OHLCV depth below this → NO_GO (can't even backtest)
TARGET_DEPTH_DAYS = 730    # all three sources ≥ this → GO
CONFIG_PERIOD_DAYS = 1460  # informational: existing coins carry ~4yr history

# Binance USDT-perp futures metrics archive does not predate this.
_ARCHIVE_FLOOR = date(2020, 1, 1)


@dataclasses.dataclass(frozen=True)
class SourceCoverage:
    """Coverage of one data source for one coin. `available` False ⇒ data absent
    or probe failed (see `error`); `earliest`/`depth_days` are then None."""
    available: bool
    earliest: date | None
    depth_days: int | None
    error: str | None = None


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def tickers_for(name: str) -> tuple[str, str, str]:
    """(okx_swap, ccxt_bybit, binance_usdt) derived from a coin short name.

    Matches research.pipeline.config.SymbolConfig conventions.
    """
    up = name.upper()
    return f"{up}-USDT-SWAP", f"{up}/USDT:USDT", f"{up}USDT"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_coin_scout.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/coin_scout.py research/tests/test_coin_scout.py
git commit -m "feat(scout): coin_scout module skeleton (constants, SourceCoverage, tickers_for)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `score_coin` verdict logic (pure)

**Files:**
- Modify: `research/lib/coin_scout.py`
- Test: `research/tests/test_coin_scout.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `research/tests/test_coin_scout.py`:

```python
def _cov(available=True, depth=1000):
    return SourceCoverage(
        available=available,
        earliest=date(2022, 1, 1) if available else None,
        depth_days=depth if available else None,
        error=None,
    )


def test_score_go_when_all_three_deep():
    v, reasons = coin_scout.score_coin(_cov(depth=1500), _cov(depth=1500), _cov(depth=1400))
    assert v == "GO"
    assert reasons


def test_score_nogo_when_ohlcv_missing():
    v, _ = coin_scout.score_coin(_cov(available=False), _cov(), _cov())
    assert v == "NO_GO"


def test_score_nogo_when_ohlcv_below_floor():
    v, reasons = coin_scout.score_coin(_cov(depth=300), _cov(), _cov())
    assert v == "NO_GO"
    assert any("365" in r for r in reasons)


def test_score_partial_when_archive_missing_flags_positioning():
    v, reasons = coin_scout.score_coin(_cov(depth=1500), _cov(depth=1500), _cov(available=False))
    assert v == "PARTIAL"
    assert any("positioning" in r for r in reasons)


def test_score_partial_when_archive_shallow():
    v, _ = coin_scout.score_coin(_cov(depth=1500), _cov(depth=1500), _cov(depth=400))
    assert v == "PARTIAL"


def test_score_go_at_exact_target_boundary():
    v, _ = coin_scout.score_coin(_cov(depth=730), _cov(depth=730), _cov(depth=730))
    assert v == "GO"


def test_score_partial_at_ohlcv_floor_but_below_target():
    # 365 is not < 365 → not NO_GO; but < 730 target → PARTIAL
    v, _ = coin_scout.score_coin(_cov(depth=365), _cov(depth=1500), _cov(depth=1500))
    assert v == "PARTIAL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k score -v`
Expected: FAIL — `AttributeError: module 'lib.coin_scout' has no attribute 'score_coin'`

- [ ] **Step 3: Implement `score_coin`**

Append to `research/lib/coin_scout.py`:

```python
def score_coin(
    ohlcv: SourceCoverage,
    funding: SourceCoverage,
    archive: SourceCoverage,
    min_ohlcv_days: int = MIN_OHLCV_DAYS,
    target_days: int = TARGET_DEPTH_DAYS,
) -> tuple[str, list[str]]:
    """Pure verdict: GO / PARTIAL / NO_GO + human reasons.

    NO_GO   : OKX OHLCV missing or shallower than `min_ohlcv_days`.
    GO      : all three sources available and depth ≥ `target_days`.
    PARTIAL : runnable but a source is missing/shallow (archive gap = no
              positioning factors, the proven edge).
    """
    reasons: list[str] = []
    o_depth = ohlcv.depth_days or 0

    if not ohlcv.available:
        return "NO_GO", ["okx_ohlcv unavailable"]
    if o_depth < min_ohlcv_days:
        return "NO_GO", [f"okx_ohlcv depth {o_depth}d < floor {min_ohlcv_days}d"]

    f_ok = funding.available and (funding.depth_days or 0) >= target_days
    a_ok = archive.available and (archive.depth_days or 0) >= target_days
    o_ok = o_depth >= target_days

    if o_ok and f_ok and a_ok:
        return "GO", [
            f"all sources >= {target_days}d "
            f"(ohlcv {o_depth}d, funding {funding.depth_days}d, archive {archive.depth_days}d)"
        ]

    if not archive.available:
        reasons.append("binance_archive missing -> no positioning factors")
    elif (archive.depth_days or 0) < target_days:
        reasons.append(f"binance_archive depth {archive.depth_days}d < target {target_days}d")
    if not funding.available:
        reasons.append("okx_funding missing")
    elif (funding.depth_days or 0) < target_days:
        reasons.append(f"okx_funding depth {funding.depth_days}d < target {target_days}d")
    if not o_ok:
        reasons.append(f"okx_ohlcv depth {o_depth}d < target {target_days}d")
    return "PARTIAL", reasons
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k score -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/coin_scout.py research/tests/test_coin_scout.py
git commit -m "feat(scout): score_coin GO/PARTIAL/NO_GO verdict logic

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `_earliest_archive_day` binary search

**Files:**
- Modify: `research/lib/coin_scout.py`
- Test: `research/tests/test_coin_scout.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `research/tests/test_coin_scout.py`:

```python
from datetime import timedelta  # add near top imports if not present


def test_earliest_archive_day_finds_boundary():
    start = date(2021, 3, 1)
    today = date(2026, 6, 23)
    got = coin_scout._earliest_archive_day(
        "BNBUSDT", today=today, exists=lambda s, d: d >= start
    )
    assert got == start


def test_earliest_archive_day_all_absent_returns_none():
    got = coin_scout._earliest_archive_day(
        "ZZZUSDT", today=date(2026, 6, 23), exists=lambda s, d: False
    )
    assert got is None


def test_earliest_archive_day_skips_tail_soft_gap():
    start = date(2022, 1, 1)
    today = date(2026, 6, 23)
    gap = today - timedelta(days=2)  # the first anchor probe day is a soft gap
    got = coin_scout._earliest_archive_day(
        "BNBUSDT", today=today, exists=lambda s, d: d >= start and d != gap
    )
    assert got == start


def test_earliest_archive_day_floor_clamped():
    # exists from before the floor → boundary is the floor itself
    got = coin_scout._earliest_archive_day(
        "BTCUSDT", today=date(2026, 6, 23), exists=lambda s, d: True
    )
    assert got == coin_scout._ARCHIVE_FLOOR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k earliest -v`
Expected: FAIL — `AttributeError: module 'lib.coin_scout' has no attribute '_earliest_archive_day'`

- [ ] **Step 3: Implement `_earliest_archive_day`**

Append to `research/lib/coin_scout.py`:

```python
def _earliest_archive_day(
    symbol: str,
    *,
    floor: date = _ARCHIVE_FLOOR,
    today: date | None = None,
    exists=None,
) -> date | None:
    """Earliest day with an available daily-metrics zip, via binary search.

    `exists(symbol, day) -> bool` is injected for testing (defaults to
    binance_dump.metrics_day_exists). Upper bound is `today - 2d` to dodge the
    T+1 unpublished tail; a short tail soft-gap is skipped by stepping back up to
    7 days to find an anchor that exists. Returns None if no day in range exists.

    A rare mid-range soft gap can over-estimate `earliest` by a few days — fine
    for a feasibility scout (depth is a coverage signal, not an exact count).
    """
    exists = exists or binance_dump.metrics_day_exists
    hi = (today or _utc_today()) - timedelta(days=2)
    lo = floor
    if hi < lo:
        return None

    # Anchor: first existing day at/just-before hi (skip a short tail gap).
    anchor = None
    probe = hi
    for _ in range(7):
        if probe < lo:
            break
        if exists(symbol, probe):
            anchor = probe
            break
        probe -= timedelta(days=1)
    if anchor is None:
        return None

    # Bisect [lo, anchor] for the smallest day where exists() is True.
    hi = anchor
    while lo < hi:
        mid = lo + (hi - lo) // 2
        if exists(symbol, mid):
            hi = mid
        else:
            lo = mid + timedelta(days=1)
    return lo
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k earliest -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/coin_scout.py research/tests/test_coin_scout.py
git commit -m "feat(scout): _earliest_archive_day binary search (tail-gap tolerant)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: probe functions (OKX OHLCV, OKX funding, Binance archive)

**Files:**
- Modify: `research/lib/coin_scout.py`
- Test: `research/tests/test_coin_scout.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `research/tests/test_coin_scout.py`:

```python
import pandas as pd  # add near top imports if not present


def test_probe_okx_ohlcv_available(monkeypatch):
    idx = pd.to_datetime(["2021-01-01", "2026-06-20"], utc=True)
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    monkeypatch.setattr(coin_scout.okx_data, "fetch_candles", lambda *a, **k: df)
    cov = coin_scout.probe_okx_ohlcv("BNB-USDT-SWAP")
    assert cov.available is True
    assert cov.earliest == date(2021, 1, 1)
    assert cov.depth_days >= 1800


def test_probe_okx_ohlcv_unknown_symbol_is_unavailable(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history-candles error: 51001 unknown instId")
    monkeypatch.setattr(coin_scout.okx_data, "fetch_candles", boom)
    cov = coin_scout.probe_okx_ohlcv("ZZZ-USDT-SWAP")
    assert cov.available is False
    assert cov.error is not None


def test_probe_okx_ohlcv_empty_frame_is_unavailable(monkeypatch):
    monkeypatch.setattr(coin_scout.okx_data, "fetch_candles", lambda *a, **k: pd.DataFrame())
    cov = coin_scout.probe_okx_ohlcv("NEW-USDT-SWAP")
    assert cov.available is False
    assert cov.error is None


def test_probe_okx_funding_available(monkeypatch):
    idx = pd.to_datetime(["2021-06-01", "2026-06-20"], utc=True)
    df = pd.DataFrame({"funding_rate": [0.0001, 0.0001]}, index=idx)
    monkeypatch.setattr(coin_scout.okx_data, "fetch_funding_history", lambda *a, **k: df)
    cov = coin_scout.probe_okx_funding("BNB-USDT-SWAP")
    assert cov.available is True
    assert cov.earliest == date(2021, 6, 1)


def test_probe_binance_archive_available(monkeypatch):
    monkeypatch.setattr(
        coin_scout, "_earliest_archive_day", lambda *a, **k: date(2021, 2, 10)
    )
    cov = coin_scout.probe_binance_archive("BNBUSDT")
    assert cov.available is True
    assert cov.earliest == date(2021, 2, 10)
    assert cov.depth_days >= 1800


def test_probe_binance_archive_absent(monkeypatch):
    monkeypatch.setattr(coin_scout, "_earliest_archive_day", lambda *a, **k: None)
    cov = coin_scout.probe_binance_archive("ZZZUSDT")
    assert cov.available is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k probe -v`
Expected: FAIL — `AttributeError: module 'lib.coin_scout' has no attribute 'probe_okx_ohlcv'`

- [ ] **Step 3: Implement the three probes**

Append to `research/lib/coin_scout.py`:

```python
def _coverage_from_index(idx) -> SourceCoverage:
    """Build a SourceCoverage from a non-empty UTC DatetimeIndex."""
    earliest = idx.min().date()
    return SourceCoverage(
        available=True,
        earliest=earliest,
        depth_days=(_utc_today() - earliest).days,
        error=None,
    )


def probe_okx_ohlcv(okx_swap: str, lookback_days: int = 2000) -> SourceCoverage:
    """Earliest available 1H candle for `okx_swap` (deep history request)."""
    try:
        df = okx_data.fetch_candles(okx_swap, days=lookback_days, bar="1H")
    except Exception as exc:  # network / okx code!=0 / unknown instId — never crash the run
        return SourceCoverage(False, None, None, str(exc)[:200])
    if df is None or df.empty:
        return SourceCoverage(False, None, None, None)
    return _coverage_from_index(df.index)


def probe_okx_funding(okx_swap: str, lookback_days: int = 2000) -> SourceCoverage:
    """Earliest available funding rate for `okx_swap`."""
    try:
        df = okx_data.fetch_funding_history(okx_swap, lookback_days)
    except Exception as exc:
        return SourceCoverage(False, None, None, str(exc)[:200])
    if df is None or df.empty:
        return SourceCoverage(False, None, None, None)
    return _coverage_from_index(df.index)


def probe_binance_archive(binance_usdt: str) -> SourceCoverage:
    """Earliest available daily-metrics archive day for `binance_usdt`."""
    try:
        earliest = _earliest_archive_day(binance_usdt)
    except Exception as exc:
        return SourceCoverage(False, None, None, str(exc)[:200])
    if earliest is None:
        return SourceCoverage(False, None, None, None)
    return SourceCoverage(
        available=True,
        earliest=earliest,
        depth_days=(_utc_today() - earliest).days,
        error=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k probe -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add research/lib/coin_scout.py research/tests/test_coin_scout.py
git commit -m "feat(scout): okx ohlcv/funding + binance archive coverage probes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: `scout_coins` orchestrator + serializers

**Files:**
- Modify: `research/lib/coin_scout.py`
- Test: `research/tests/test_coin_scout.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `research/tests/test_coin_scout.py`:

```python
def _go_verdict():
    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    return coin_scout.CoinVerdict(
        "bnb", "BNB-USDT-SWAP", "BNB/USDT:USDT", "BNBUSDT",
        c, c, c, "GO", ["all sources >= 730d"],
    )


def test_scout_coins_runs_each_coin(monkeypatch):
    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    monkeypatch.setattr(coin_scout, "probe_okx_ohlcv", lambda s: c)
    monkeypatch.setattr(coin_scout, "probe_okx_funding", lambda s: c)
    monkeypatch.setattr(coin_scout, "probe_binance_archive", lambda s: c)
    rep = coin_scout.scout_coins(["bnb", "XRP", ""])  # blank skipped
    assert [v.name for v in rep.coins] == ["bnb", "xrp"]
    assert rep.coins[0].verdict == "GO"
    assert rep.thresholds["min_ohlcv_days"] == coin_scout.MIN_OHLCV_DAYS


def test_report_to_dict_serializes_dates():
    rep = coin_scout.ScoutReport("2026-06-23T00:00:00+00:00", {"min_ohlcv_days": 365}, [_go_verdict()])
    d = coin_scout.report_to_dict(rep)
    assert d["coins"][0]["okx_ohlcv"]["earliest"] == "2021-01-01"
    assert d["coins"][0]["verdict"] == "GO"


def test_format_table_lists_coin_and_verdict():
    rep = coin_scout.ScoutReport("t", {}, [_go_verdict()])
    table = coin_scout.format_table(rep)
    assert "bnb" in table and "GO" in table


def test_go_coins_yaml_only_includes_go():
    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    partial = coin_scout.CoinVerdict(
        "xrp", "XRP-USDT-SWAP", "XRP/USDT:USDT", "XRPUSDT", c, c, c, "PARTIAL", [],
    )
    rep = coin_scout.ScoutReport("t", {}, [_go_verdict(), partial])
    y = coin_scout.go_coins_yaml(rep)
    assert "name: bnb" in y and "BNB-USDT-SWAP" in y
    assert "xrp" not in y
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k "scout_coins or report_to_dict or format_table or go_coins_yaml" -v`
Expected: FAIL — `AttributeError: module 'lib.coin_scout' has no attribute 'CoinVerdict'`

- [ ] **Step 3: Implement dataclasses, orchestrator, serializers**

Append to `research/lib/coin_scout.py`:

```python
@dataclasses.dataclass(frozen=True)
class CoinVerdict:
    name: str
    okx_swap: str
    ccxt_bybit: str
    binance_usdt: str
    okx_ohlcv: SourceCoverage
    okx_funding: SourceCoverage
    binance_archive: SourceCoverage
    verdict: str
    reasons: list[str]


@dataclasses.dataclass(frozen=True)
class ScoutReport:
    generated_at: str
    thresholds: dict
    coins: list[CoinVerdict]


def scout_coins(
    names,
    *,
    min_ohlcv_days: int = MIN_OHLCV_DAYS,
    target_days: int = TARGET_DEPTH_DAYS,
) -> ScoutReport:
    """Probe every coin name and assemble a ScoutReport (blank names skipped)."""
    coins: list[CoinVerdict] = []
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        okx_swap, ccxt_bybit, binance_usdt = tickers_for(name)
        ohlcv = probe_okx_ohlcv(okx_swap)
        funding = probe_okx_funding(okx_swap)
        archive = probe_binance_archive(binance_usdt)
        verdict, reasons = score_coin(ohlcv, funding, archive, min_ohlcv_days, target_days)
        coins.append(CoinVerdict(
            name, okx_swap, ccxt_bybit, binance_usdt,
            ohlcv, funding, archive, verdict, reasons,
        ))
    return ScoutReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        thresholds={
            "min_ohlcv_days": min_ohlcv_days,
            "target_depth_days": target_days,
            "config_period_days": CONFIG_PERIOD_DAYS,
        },
        coins=coins,
    )


def _cov_to_dict(c: SourceCoverage) -> dict:
    return {
        "available": c.available,
        "earliest": c.earliest.isoformat() if c.earliest else None,
        "depth_days": c.depth_days,
        "error": c.error,
    }


def report_to_dict(report: ScoutReport) -> dict:
    return {
        "generated_at": report.generated_at,
        "thresholds": report.thresholds,
        "coins": [
            {
                "name": v.name,
                "okx_swap": v.okx_swap,
                "ccxt_bybit": v.ccxt_bybit,
                "binance_usdt": v.binance_usdt,
                "okx_ohlcv": _cov_to_dict(v.okx_ohlcv),
                "okx_funding": _cov_to_dict(v.okx_funding),
                "binance_archive": _cov_to_dict(v.binance_archive),
                "verdict": v.verdict,
                "reasons": v.reasons,
            }
            for v in report.coins
        ],
    }


def _depth_cell(c: SourceCoverage) -> str:
    return f"{c.depth_days}d" if c.available else "—"


def format_table(report: ScoutReport) -> str:
    lines = [f"{'coin':<7} {'okx_ohlcv':<10} {'okx_funding':<12} {'binance_archive':<16} verdict"]
    for v in report.coins:
        lines.append(
            f"{v.name:<7} {_depth_cell(v.okx_ohlcv):<10} {_depth_cell(v.okx_funding):<12} "
            f"{_depth_cell(v.binance_archive):<16} {v.verdict}"
        )
    return "\n".join(lines)


def go_coins_yaml(report: ScoutReport) -> str:
    """Paste-ready research_config.yaml `symbols:` blocks for GO coins."""
    blocks = [
        f'- name: {v.name}\n  okx_swap: "{v.okx_swap}"\n  ccxt_bybit: "{v.ccxt_bybit}"'
        for v in report.coins
        if v.verdict == "GO"
    ]
    return "\n".join(blocks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_coin_scout.py -v`
Expected: PASS (all tests so far green)

- [ ] **Step 5: Commit**

```bash
git add research/lib/coin_scout.py research/tests/test_coin_scout.py
git commit -m "feat(scout): scout_coins orchestrator + json/table/yaml serializers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: CLI driver `research/pipeline/coin_scout.py`

**Files:**
- Create: `research/pipeline/coin_scout.py`
- Test: `research/tests/test_coin_scout.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `research/tests/test_coin_scout.py`:

```python
import json


def test_cli_writes_report_and_returns_zero(tmp_path, monkeypatch):
    from pipeline import coin_scout as cli

    c = SourceCoverage(True, date(2021, 1, 1), 1500, None)
    rep = cli.ScoutReport(
        "t", {"min_ohlcv_days": 365},
        [cli.CoinVerdict("bnb", "BNB-USDT-SWAP", "BNB/USDT:USDT", "BNBUSDT",
                         c, c, c, "GO", ["ok"])],
    )
    monkeypatch.setattr(cli, "scout_coins", lambda names: rep)
    out = tmp_path / "scout.json"
    rc = cli.main(["--coins", "bnb", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["coins"][0]["name"] == "bnb"


def test_cli_uses_default_candidates_when_no_coins(tmp_path, monkeypatch):
    from pipeline import coin_scout as cli

    captured = {}
    def fake_scout(names):
        captured["names"] = names
        return cli.ScoutReport("t", {}, [])
    monkeypatch.setattr(cli, "scout_coins", fake_scout)
    cli.main(["--out", str(tmp_path / "s.json")])
    assert captured["names"] == cli.DEFAULT_CANDIDATES
    assert "bnb" in cli.DEFAULT_CANDIDATES and "bch" in cli.DEFAULT_CANDIDATES
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k cli -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.coin_scout'`

- [ ] **Step 3: Create the CLI module**

Create `research/pipeline/coin_scout.py`:

```python
"""CLI: probe data-source coverage for candidate coins.

    python -m research.pipeline.coin_scout                 # default candidate set
    python -m research.pipeline.coin_scout --coins bnb,xrp

Writes research/manifests/coin_scout.json and prints a verdict table plus
paste-ready research_config.yaml blocks for GO coins.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Bootstrap research/ onto sys.path so ``lib.*`` imports resolve from any CWD.
_PIPELINE_DIR = Path(__file__).resolve().parent
_RESEARCH_DIR = _PIPELINE_DIR.parent
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.coin_scout import (  # noqa: E402
    CoinVerdict,
    ScoutReport,
    format_table,
    go_coins_yaml,
    report_to_dict,
    scout_coins,
)

_REPO_ROOT = _RESEARCH_DIR.parent
DEFAULT_CANDIDATES = ["bnb", "xrp", "doge", "ada", "ltc", "bch"]
_DEFAULT_OUT = _REPO_ROOT / "research" / "manifests" / "coin_scout.json"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Coin feasibility scout")
    p.add_argument("--coins", default="", help="comma-separated coin names; empty = default set")
    p.add_argument("--out", default=str(_DEFAULT_OUT), help="output JSON path")
    args = p.parse_args(argv)

    names = [c for c in args.coins.split(",") if c.strip()] or DEFAULT_CANDIDATES
    report = scout_coins(names)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report_to_dict(report), indent=2, ensure_ascii=False), encoding="utf-8")

    print(format_table(report))
    go_yaml = go_coins_yaml(report)
    if go_yaml:
        print("\n# GO coins — paste into research_config.yaml under symbols:\n")
        print(go_yaml)
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd research && python -m pytest tests/test_coin_scout.py -k cli -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/coin_scout.py research/tests/test_coin_scout.py
git commit -m "feat(scout): coin_scout CLI driver (json + table + GO yaml)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Full suite green + live smoke + wrap-up

**Files:** none (verification only)

- [ ] **Step 1: Run the full coin_scout + binance_dump test files**

Run: `cd research && python -m pytest tests/test_coin_scout.py tests/test_binance_dump.py -v`
Expected: PASS (all green, zero real network — every probe is mocked)

- [ ] **Step 2: Run the broader research suite to confirm no regression**

Run: `cd research && python -m pytest tests/ -q`
Expected: PASS for everything that passed before this change. (Pre-existing failures unrelated to scout — e.g. Windows-env oauth/packaging — are out of scope; confirm the count matches the pre-change baseline and no *new* failures reference `coin_scout`.)

- [ ] **Step 3: Live smoke (manual, hits real network — run once)**

Run: `python -m research.pipeline.coin_scout`
Expected: a 6-row table (bnb/xrp/doge/ada/ltc/bch). Sanity-check: established coins (BNB, XRP) show multi-year depths and `GO`; `research/manifests/coin_scout.json` is written and valid JSON. Eyeball that earliest dates are plausible (not future, not before 2019).

- [ ] **Step 4: Final wrap commit (if smoke surfaced any tweak)**

Only if Step 3 required a threshold/format tweak:

```bash
git add -A research/
git commit -m "chore(scout): threshold/format tweak after live smoke

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> Do **not** commit `research/manifests/coin_scout.json` — it is a generated artifact (manifests are gitignored except curated entries). Confirm `git status` does not stage it.

---

## Self-Review

**Spec coverage:**
- Module split (lib + CLI) → Tasks 2–7 ✓
- Probe mechanics (OKX deep fetch, archive HEAD + binary search) → Tasks 1, 4, 5 ✓
- Verdict thresholds (365 / 730 / 1460) → Task 3 ✓
- Output (json + table + GO yaml) → Task 6 (serializers), Task 7 (CLI writes file) ✓
- Edge cases (probe failure isolation, T+1 tail, tail soft-gap, case-insensitive names) → Tasks 4, 5, 6 ✓
- Testing (score boundaries, binary-search edges, mocked probes, CLI) → Tasks 3–7 ✓
- "Does not auto-edit config" → CLI only prints YAML (Task 7) ✓
- Out-of-scope items (Bybit probe, cross-coin ranking, auto-edit, full backfill) → not built ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every run step shows command + expected outcome. ✓

**Type consistency:** `SourceCoverage(available, earliest, depth_days, error)` used identically across Tasks 2/3/5/6. `score_coin(ohlcv, funding, archive, min_ohlcv_days, target_days) -> (str, list)` consistent (Task 3 def, Task 6 call). `_earliest_archive_day(symbol, *, floor, today, exists)` consistent (Task 4 def, Task 5 call/mocks). `CoinVerdict` / `ScoutReport` field names match between Task 6 def and Task 7 CLI usage. `scout_coins`, `report_to_dict`, `format_table`, `go_coins_yaml` names consistent across lib + CLI. ✓
