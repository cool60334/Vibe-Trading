# Live L/S Endpoint Freshness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Source `ls_divergence`'s two inputs from Binance live REST L/S endpoints and overlay a minute-fresh tail onto the archive OI parquet, so the live trader escapes the ~24h archive lag that made the Phase-0 gate NO-GO.

**Architecture:** The archive ETL stays the system of record for history. Add a pandas-free network primitive in `lib/binance_dump.py` (live JSON fetch), build the 2-column live frame + merge + reconciliation in `lib/oi_metrics.py`, and expose a `--live` flag in `dump_oi.py` that overlays the fresh tail. Validation is a freshness + reconciliation check (not a re-backtest — the decay sweep already served as the re-gate).

**Tech Stack:** Python, pandas, `urllib` (stdlib — no `requests` dep, matching the existing module), pytest. Binance `fapi.binance.com/futures/data/*` public endpoints (no key).

**Spec:** [`docs/superpowers/specs/2026-06-22-live-ls-endpoint-freshness-design.md`](../specs/2026-06-22-live-ls-endpoint-freshness-design.md)

---

## Conventions

- Run from `research/`. Research tests run **separately** from dashboard/agent.
- `urllib` only (the module is deliberately `requests`-free). New network calls reuse the
  `_fetch`-style timeout/retry idiom and stay monkeypatch-friendly (tests patch the fetch).
- Commits staged for the engineer; the user commits on explicit request.

## File Structure

| File | Responsibility |
|---|---|
| `research/lib/binance_dump.py` (modify) | **Network only, pandas-free.** Add `_fetch_json(url)`, `live_ls_url(...)`, and `fetch_live_ls_raw(symbol, endpoint_path, period, limit) -> list[dict]`. (Spec said `fetch_live_ls_ratios` here; the DataFrame build moves to `oi_metrics` to keep this module pandas-free — network vs parsing separation.) |
| `research/lib/oi_metrics.py` (modify) | Parsing/aggregation. Add `fetch_live_ls_ratios(symbol, ...) -> DataFrame` (2 cols), `merge_live_tail(archive_hourly, live_5min)`, `reconcile_live_archive(archive, live, ...)`. |
| `research/dump_oi.py` (modify) | Add `--live` flag → fetch + merge + reconcile before writing the parquet. |
| `research/tests/test_binance_dump.py` (modify) | Tests for `_fetch_json` / `live_ls_url` / `fetch_live_ls_raw` (mocked network). |
| `research/tests/test_oi_metrics.py` (modify) | Tests for `fetch_live_ls_ratios` / `merge_live_tail` / `reconcile_live_archive`. |

---

### Task 1: Live JSON network primitive in `binance_dump.py` (TDD)

**Files:**
- Modify: `research/lib/binance_dump.py`
- Test: `research/tests/test_binance_dump.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_binance_dump.py`:
```python
import json
from lib import binance_dump


def test_live_ls_url_maps_position_vs_account():
    acct = binance_dump.live_ls_url("SOLUSDT", binance_dump.GLOBAL_LS_ACCOUNT_PATH, "5m", 500)
    pos = binance_dump.live_ls_url("SOLUSDT", binance_dump.TOPTRADER_LS_POSITION_PATH, "5m", 500)
    assert "globalLongShortAccountRatio" in acct and "symbol=SOLUSDT" in acct and "period=5m" in acct
    # MUST be the POSITION variant for toptrader (not the account variant).
    assert "topLongShortPositionRatio" in pos
    assert "topLongShortAccountRatio" not in pos


def test_fetch_live_ls_raw_parses_json(monkeypatch):
    payload = [
        {"symbol": "SOLUSDT", "longShortRatio": "1.50", "timestamp": 1718900000000},
        {"symbol": "SOLUSDT", "longShortRatio": "1.40", "timestamp": 1718900300000},
    ]
    monkeypatch.setattr(binance_dump, "_fetch_json", lambda url, **kw: payload)
    rows = binance_dump.fetch_live_ls_raw("SOLUSDT", binance_dump.GLOBAL_LS_ACCOUNT_PATH)
    assert rows == payload
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd research && python -m pytest tests/test_binance_dump.py -k "live" -v`
Expected: FAIL — `AttributeError: module 'lib.binance_dump' has no attribute 'live_ls_url'`.

- [ ] **Step 3: Implement the network primitives**

Add to `research/lib/binance_dump.py` (add `import json` at top with the other stdlib imports):
```python
_FAPI_BASE = "https://fapi.binance.com"
GLOBAL_LS_ACCOUNT_PATH = "/futures/data/globalLongShortAccountRatio"   # → global_ls_accounts
TOPTRADER_LS_POSITION_PATH = "/futures/data/topLongShortPositionRatio"  # → toptrader_ls_positions


def live_ls_url(symbol: str, endpoint_path: str, period: str = "5m", limit: int = 500) -> str:
    """Build a Binance futures-data L/S ratio URL (public, no key)."""
    return f"{_FAPI_BASE}{endpoint_path}?symbol={symbol}&period={period}&limit={limit}"


def _fetch_json(url: str, timeout: float = _FETCH_TIMEOUT, retries: int = 3):
    """GET `url` and parse JSON, retrying transient errors (mirrors `_fetch`).

    Isolated for test monkeypatching. HTTPError reraises immediately; timeouts /
    connection drops retry with backoff.
    """
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode())
        except HTTPError:
            raise
        except (URLError, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def fetch_live_ls_raw(
    symbol: str, endpoint_path: str, period: str = "5m", limit: int = 500
) -> list[dict]:
    """Return the raw JSON rows for one live L/S endpoint (list of dicts)."""
    return _fetch_json(live_ls_url(symbol, endpoint_path, period, limit))
```

- [ ] **Step 4: Run to verify pass**

Run: `cd research && python -m pytest tests/test_binance_dump.py -k "live" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add research/lib/binance_dump.py research/tests/test_binance_dump.py
git commit -m "feat(oi): live L/S JSON fetch primitive in binance_dump"
```

---

### Task 2: `fetch_live_ls_ratios` + `merge_live_tail` in `oi_metrics.py` (TDD)

**Files:**
- Modify: `research/lib/oi_metrics.py`
- Test: `research/tests/test_oi_metrics.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_oi_metrics.py`:
```python
import pandas as pd
from lib import oi_metrics, binance_dump


def test_fetch_live_ls_ratios_builds_two_col_frame(monkeypatch):
    def fake_raw(symbol, endpoint_path, period="5m", limit=500):
        base = 1718900000000
        if endpoint_path == binance_dump.GLOBAL_LS_ACCOUNT_PATH:
            return [{"longShortRatio": "1.5", "timestamp": base},
                    {"longShortRatio": "1.6", "timestamp": base + 300000}]
        return [{"longShortRatio": "2.5", "timestamp": base},
                {"longShortRatio": "2.6", "timestamp": base + 300000}]
    monkeypatch.setattr(binance_dump, "fetch_live_ls_raw", fake_raw)

    df = oi_metrics.fetch_live_ls_ratios("SOLUSDT")
    assert list(df.columns) == ["global_ls_accounts", "toptrader_ls_positions"]
    assert df["global_ls_accounts"].iloc[0] == 1.5
    assert df["toptrader_ls_positions"].iloc[0] == 2.5
    assert df.index.tz is not None  # UTC


def test_merge_live_tail_live_precedence_and_append():
    idx = pd.date_range("2026-06-20", periods=4, freq="1h", tz="UTC")
    archive = pd.DataFrame({
        "oi": [10.0, 11, 12, 13],
        "global_ls_accounts": [1.0, 1.0, 1.0, 1.0],
        "toptrader_ls_positions": [2.0, 2.0, 2.0, 2.0],
    }, index=idx)
    # Live 5-min frame spanning the last archive hour + one new hour.
    live_idx = pd.date_range("2026-06-20 03:00", periods=24, freq="5min", tz="UTC")
    live = pd.DataFrame({
        "global_ls_accounts": [9.0] * 24,
        "toptrader_ls_positions": [8.0] * 24,
    }, index=live_idx)

    out = oi_metrics.merge_live_tail(archive, live)
    # Overlap hour 03:00 overwritten by live (precedence)…
    assert out.loc[idx[3], "global_ls_accounts"] == 9.0
    # …new hour 04:00 appended…
    assert out.loc[pd.Timestamp("2026-06-20 04:00", tz="UTC"), "toptrader_ls_positions"] == 8.0
    # …oi (not an L/S col) untouched on overlap, NaN on appended row.
    assert out.loc[idx[3], "oi"] == 13.0
    assert pd.isna(out.loc[pd.Timestamp("2026-06-20 04:00", tz="UTC"), "oi"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "live or merge_live" -v`
Expected: FAIL — `AttributeError: module 'lib.oi_metrics' has no attribute 'fetch_live_ls_ratios'`.

- [ ] **Step 3: Implement**

Add to `research/lib/oi_metrics.py`:
```python
_LIVE_LS_COLS = ("global_ls_accounts", "toptrader_ls_positions")


def fetch_live_ls_ratios(symbol: str, period: str = "5m", limit: int = 500) -> pd.DataFrame:
    """Fetch the two live L/S ratios into a 5-min DataFrame [global_ls_accounts,
    toptrader_ls_positions], indexed by UTC timestamp."""
    paths = {
        "global_ls_accounts": binance_dump.GLOBAL_LS_ACCOUNT_PATH,
        "toptrader_ls_positions": binance_dump.TOPTRADER_LS_POSITION_PATH,
    }
    cols: dict[str, pd.Series] = {}
    for col, path in paths.items():
        rows = binance_dump.fetch_live_ls_raw(symbol, path, period, limit)
        s = pd.Series(
            {pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True): float(r["longShortRatio"])
             for r in rows}
        ).sort_index()
        cols[col] = s
    df = pd.DataFrame(cols)
    df.index.name = "time"
    return df


def merge_live_tail(archive_hourly: pd.DataFrame, live_5min: pd.DataFrame) -> pd.DataFrame:
    """Overlay the live L/S tail onto the archive hourly frame.

    Live 5-min is aggregated to 1H with the SAME convention as `aggregate_to_hourly`
    (snapshot=last). Only the two L/S columns are patched (live precedence on overlap,
    new hours appended); oi/oi_usd/taker keep their archive values (NaN on appended rows).
    """
    live_hourly = aggregate_to_hourly(live_5min)  # snapshot=last on the present cols
    out = archive_hourly.reindex(archive_hourly.index.union(live_hourly.index))
    for col in _LIVE_LS_COLS:
        if col in live_hourly.columns:
            out.loc[live_hourly.index, col] = live_hourly[col]
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "live or merge_live" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add research/lib/oi_metrics.py research/tests/test_oi_metrics.py
git commit -m "feat(oi): live L/S frame build + archive tail merge"
```

---

### Task 3: `reconcile_live_archive` correctness gate (TDD)

**Files:**
- Modify: `research/lib/oi_metrics.py`
- Test: `research/tests/test_oi_metrics.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_oi_metrics.py`:
```python
import numpy as np
import pytest


def _hourly_ls(values_g, values_t, start="2026-06-20"):
    idx = pd.date_range(start, periods=len(values_g), freq="1h", tz="UTC")
    return pd.DataFrame({"global_ls_accounts": values_g,
                         "toptrader_ls_positions": values_t}, index=idx)


def test_reconcile_passes_when_live_matches_archive():
    n = 48
    g = np.linspace(1.0, 2.0, n)
    t = np.linspace(2.0, 3.0, n)
    archive = _hourly_ls(g, t)
    live = _hourly_ls(g * 1.01, t * 0.99)   # within 5% / high corr
    oi_metrics.reconcile_live_archive(archive, live)  # must not raise


def test_reconcile_fails_on_account_vs_position_swap():
    n = 48
    g = np.linspace(1.0, 2.0, n)
    t = np.linspace(2.0, 3.0, n)
    archive = _hourly_ls(g, t)
    # toptrader_ls_positions accidentally fed the account-ratio series (different scale/shape).
    live = _hourly_ls(g, np.linspace(0.5, 0.6, n))
    with pytest.raises(ValueError, match="reconcile"):
        oi_metrics.reconcile_live_archive(archive, live)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "reconcile" -v`
Expected: FAIL — `AttributeError: … 'reconcile_live_archive'`.

- [ ] **Step 3: Implement**

Add to `research/lib/oi_metrics.py`:
```python
def reconcile_live_archive(
    archive: pd.DataFrame,
    live: pd.DataFrame,
    min_corr: float = 0.95,
    max_med_rel_err: float = 0.05,
    min_overlap: int = 12,
) -> None:
    """Raise ValueError unless each live L/S column matches archive in the overlap.

    Guards against a position-vs-account endpoint mix-up (the variants diverge far
    more than the tolerance) and unit/scale errors. Tolerance, not exact equality —
    the two series are computed independently. `live` is the 1H-aggregated tail.
    """
    overlap = archive.index.intersection(live.index)
    if len(overlap) < min_overlap:
        raise ValueError(f"reconcile: insufficient overlap ({len(overlap)} < {min_overlap} hrs)")
    for col in _LIVE_LS_COLS:
        a = archive.loc[overlap, col]
        b = live.loc[overlap, col]
        mask = a.notna() & b.notna()
        if int(mask.sum()) < min_overlap:
            raise ValueError(f"reconcile {col}: insufficient non-NaN overlap ({int(mask.sum())})")
        corr = float(a[mask].corr(b[mask]))
        med_rel = float(((b[mask] - a[mask]).abs() / a[mask].abs().replace(0, np.nan)).median())
        if corr < min_corr or med_rel > max_med_rel_err:
            raise ValueError(
                f"reconcile {col} fail: corr={corr:.3f} (min {min_corr}), "
                f"med_rel_err={med_rel:.3f} (max {max_med_rel_err})"
            )
```
Add `import numpy as np` at the top of `oi_metrics.py` if not present.

- [ ] **Step 4: Run to verify pass**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "reconcile" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add research/lib/oi_metrics.py research/tests/test_oi_metrics.py
git commit -m "feat(oi): reconciliation gate (live≈archive, catches position/account mixup)"
```

---

### Task 4: `dump_oi.py --live` flag (TDD on the wiring)

**Files:**
- Modify: `research/dump_oi.py`
- Test: `research/tests/test_oi_metrics.py` (integration of dump_symbol with live)

- [ ] **Step 1: Write the failing test**

Add to `research/tests/test_oi_metrics.py`:
```python
def test_dump_symbol_live_advances_index_end(monkeypatch, tmp_path):
    import dump_oi
    from datetime import date

    # Archive returns 3 hourly rows ending 02:00.
    arch_idx = pd.date_range("2026-06-20", periods=3, freq="1h", tz="UTC")
    archive = pd.DataFrame({
        "oi": [1.0, 2, 3], "oi_usd": [1.0, 2, 3],
        "toptrader_ls_accounts": [1.0, 1, 1],
        "toptrader_ls_positions": [2.0, 2, 2],
        "global_ls_accounts": [1.0, 1, 1],
        "taker_buysell_ratio": [1.0, 1, 1],
    }, index=arch_idx)
    monkeypatch.setattr(oi_metrics, "load_oi_range", lambda *a, **k: archive)

    # Live 5-min frame extends two hours past the archive end.
    live_idx = pd.date_range("2026-06-20 02:00", periods=36, freq="5min", tz="UTC")
    live = pd.DataFrame({"global_ls_accounts": [1.0] * 36,
                         "toptrader_ls_positions": [2.0] * 36}, index=live_idx)
    monkeypatch.setattr(oi_metrics, "fetch_live_ls_ratios", lambda *a, **k: live)
    monkeypatch.setattr(oi_metrics, "reconcile_live_archive", lambda *a, **k: None)

    path = dump_oi.dump_symbol("SOLUSDT", date(2026, 6, 18), date(2026, 6, 20),
                               cache_dir=tmp_path, verify=False, live=True)
    out = pd.read_parquet(path)
    assert out.index.max() >= pd.Timestamp("2026-06-20 04:00", tz="UTC")  # advanced past archive
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "dump_symbol_live" -v`
Expected: FAIL — `dump_symbol() got an unexpected keyword argument 'live'`.

- [ ] **Step 3: Implement the flag + wiring**

In `research/dump_oi.py`, extend `dump_symbol` with a `live` param and add the merge/reconcile step:
```python
def dump_symbol(
    symbol: str,
    start: date,
    end: date,
    cache_dir: Path,
    verify: bool = True,
    live: bool = False,
) -> Path | None:
    """Download + aggregate + cache one symbol's hourly OI for ``[start, end]``.

    When ``live=True``, overlay a fresh L/S tail from the Binance live endpoints
    (reconciled against the archive overlap) so the parquet's index_end is current.
    """
    cache_dir = Path(cache_dir)
    raw_dir = cache_dir / "_raw"
    print(f"[dump_oi] {symbol}: {start.isoformat()}..{end.isoformat()} ...")
    df = oi_metrics.load_oi_range(symbol, start, end, cache_dir=raw_dir, verify=verify)
    if df.empty:
        print(f"[dump_oi] {symbol}: no data in range — skipped")
        return None

    if live:
        try:
            live_5min = oi_metrics.fetch_live_ls_ratios(symbol)
            live_hourly = oi_metrics.aggregate_to_hourly(live_5min)
            oi_metrics.reconcile_live_archive(df, live_hourly)
            df = oi_metrics.merge_live_tail(df, live_5min)
            print(f"[dump_oi] {symbol}: live tail merged → index_end {df.index[-1]}")
        except Exception as exc:  # noqa: BLE001
            print(f"[dump_oi] {symbol}: live fetch failed ({exc}) — archive-only")

    path = oi_metrics.dump_oi_parquet(symbol, df, cache_dir=cache_dir)
    nan_oi = int(df["oi"].isna().sum())
    print(
        f"[dump_oi] {symbol}: {len(df)} hourly rows "
        f"{df.index[0].date()}..{df.index[-1].date()} "
        f"({nan_oi} NaN-OI gap hours) -> {path.name}"
    )
    return path
```
Then add the CLI flag in `main()` and pass it through:
```python
    ap.add_argument("--live", action="store_true",
                    help="overlay a fresh L/S tail from Binance live endpoints")
    ...
    for sym in args.symbols:
        start = args.start or EARLIEST.get(sym, _FALLBACK_START)
        dump_symbol(sym, start, end, args.cache_dir, verify=not args.no_verify, live=args.live)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd research && python -m pytest tests/test_oi_metrics.py -k "dump_symbol_live" -v`
Expected: PASS.

- [ ] **Step 5: Run the full affected test modules (no regression)**

Run: `cd research && python -m pytest tests/test_oi_metrics.py tests/test_binance_dump.py -v`
Expected: all pass (new + pre-existing).

- [ ] **Step 6: Commit**

```bash
git add research/dump_oi.py research/tests/test_oi_metrics.py
git commit -m "feat(oi): dump_oi --live overlays fresh L/S tail (reconciled, archive fallback)"
```

---

### Task 5: Live validation run (network — DECISION/verify)

**Files:** none (runs the pipeline; reads real Binance data)

- [ ] **Step 1: Fetch a live tail and confirm freshness**

Run: `cd research && python dump_oi.py --symbols SOLUSDT --live --no-verify`
Expected: prints `live tail merged → index_end <within ~1h of now UTC>`. Confirm:
```bash
cd research && python -c "import json; m=json.load(open('data/oi/oi_SOLUSDT_1H.meta.json')); print('index_end', m['index_end'])"
```
The `index_end` must be within ~1h of now. **If it is hours stale, the endpoint itself is delayed — STOP and reconsider** (the whole fix rests on the endpoint being fresh).

- [ ] **Step 2: Confirm the reconciliation gate passed on real data**

The run in Step 1 must NOT print "live fetch failed". If it does with a reconcile error, the
position-vs-account mapping or units are wrong — fix before proceeding (do not deploy on
mis-mapped data). Verify the variant manually if needed.

- [ ] **Step 3: Propagate to the factor store + confirm fresh `ls_divergence`**

Run:
```bash
cd research && RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage0a_features && RESEARCH_ONLY_SYMBOL=sol python -m pipeline.stage1_factors
python -c "import pandas as pd; df=pd.read_parquet('manifests/factor_values_sol.parquet'); print('ls_divergence tail end:', df['ls_divergence'].dropna().index.max())"
```
Expected: the `ls_divergence` tail end is within ~1h of now (no longer ~T-1).

- [ ] **Step 4: Report the freshness result**

Report `index_end`, reconcile pass/fail, and the factor-store tail freshness. If all fresh →
the data path is proven; Phase-1 deploy can proceed (with the ≥hourly cron, below). If the
endpoint is delayed → the fix doesn't hold; surface to the user.

---

## Phase-1 handoff (the 🚩 dependency — not built here)

This change delivers the fresh data path. For it to reach the live trader, the Phase-1
refresh cron must run **≥ hourly** with `dump_oi --live` as its first step, because
factor-store freshness = **min(data-source freshness, recompute cadence)**. A daily cron
wastes the live endpoint. The cron-health guard ([[deploy plan Task 5-7]]) then tightens
from ~30h toward ~2h. Re-confirm the paper-deploy steps from
`docs/superpowers/plans/2026-06-18-sol-paper-forward-validation.md` Tasks 5-8, with the
cron cadence changed from daily to hourly.

---

## Self-Review

- **Spec coverage:** ① fetch (binance_dump primitive + oi_metrics frame) → Tasks 1–2;
  ② merge_live_tail → Task 2; ③ dump_oi --live → Task 4; ④ reconciliation gate → Task 3;
  validation reframe (freshness + reconcile, not re-backtest) → Task 5; 🚩 ≥hourly cron
  dependency → Phase-1 handoff. position-vs-account mapping asserted in Task 1 + Task 3.
  All spec sections map to a task.
- **Placeholders:** none — every code/command step is concrete. (The Phase-1 handoff
  intentionally defers deploy to the existing 2026-06-18 plan, not a placeholder.)
- **Type/name consistency:** `GLOBAL_LS_ACCOUNT_PATH` / `TOPTRADER_LS_POSITION_PATH`,
  `live_ls_url`, `_fetch_json`, `fetch_live_ls_raw` (binance_dump) and
  `fetch_live_ls_ratios`, `merge_live_tail`, `reconcile_live_archive`, `_LIVE_LS_COLS`
  (oi_metrics) are used identically across tasks and tests; `dump_symbol(..., live=...)`
  signature matches its test and `main()` call.

---

## Execution Handoff

Plan complete. Two execution options:
1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.
2. **Inline Execution** — execute in this session with checkpoints.

**Note:** Task 5 touches the real Binance network and is a verify/decision gate (a delayed
endpoint there means the fix doesn't hold) — pause for the user there regardless of mode.
