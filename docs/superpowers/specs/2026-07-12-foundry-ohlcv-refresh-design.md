# Talos Foundry — OHLCV Refresh for Real Runs

**Date:** 2026-07-12
**Status:** design approved, pending implementation
**Scope:** a reusable, cron-able script that fetches full-span OHLCV per configured
symbol and writes `ohlcv_<sym>.parquet` next to the feature store, so a Foundry run
can execute against the **real** `research/manifests/` (not a scratch slice).

---

## Problem

`run_foundry` reads a price table it does **not** fetch itself
(`features_<sym>.parquet` deliberately holds no OHLCV — orchestrator.py:246), and
`_align_ohlcv` (orchestrator.py:243) refuses a table that covers < 95% of the
**full** feature index. `features_eth` spans 2022-06 → 2026-06, but the only
committed OHLCV is the e2e fixture (2024-H2, ~23% coverage). So a real Foundry run
against real manifests cannot align its prices and fails before evaluating anything.

Verified: OKX `history-candles` reaches back to 2021-12 for `ETH-USDT-SWAP` 1H
(probed the earliest page), and stage0a already builds features from this same
source (`lib.okx_data.fetch_candles`). The data exists; it just was never saved.

---

## Non-goals

- Incremental / append-only fetch (full re-fetch each run is idempotent and
  stateless; a few minutes per coin is fine for a nightly cadence).
- A staleness gate on the Foundry side (the run's own `_align_ohlcv` already
  rejects a non-covering table; freshness policy is a separate concern).
- Any new data source (OKX `fetch_candles` is confirmed sufficient).
- Sub-hour intervals as a new capability (the script honors `RESEARCH_INTERVAL`
  like the rest of the pipeline, but 1H is the floor per the constitution).

---

## Approved decisions

- **D1** — a reusable script (`python -m research.pipeline.refresh_ohlcv` + a thin
  `scripts/refresh_foundry_ohlcv.sh` cron wrapper), mirroring `refresh_factors`.
- **D2** — covers all configured symbols (`btc/eth/sol`), honoring
  `RESEARCH_ONLY_SYMBOL` for a single coin.
- **D3** — full re-fetch each run; per-symbol independence (one coin's failure
  does not block the others); exit non-zero if any failed.
- **D4** — fetch depth is derived from each symbol's `features_<sym>.parquet`
  earliest bar (coverage guaranteed by construction), not a magic constant.

---

## Adversarial review (agy, folder mode against the code) — dispositions

Verified against `config.py`/`okx_data.py`/`factor_io.py`/`orchestrator.py` and adopted:

- **Accepted (critical) — tz-aware subtraction.** `datetime.utcnow()` is naive but
  the feature index is UTC-aware; `naive - aware` raises `TypeError`. Use
  `datetime.now(timezone.utc)`.
- **Accepted — interval-namespaced path.** `load_features` reads the
  interval-aware dir (`.../15m` under `RESEARCH_INTERVAL`); the OHLCV must be
  written to the **same** resolved dir, or a sub-hour run writes prices where the
  run won't look. Resolve once, read and write there.
- **Accepted — reuse `_align_ohlcv` for the coverage gate** instead of a hand-
  rolled 95% check, so the refresh and the run can never drift apart on what
  "covered" means.
- **Accepted — guard empty features** (`.index.min()` → `NaT`) before computing
  depth.
- **Accepted — filename uses `_symbol_short`** (the undefined `short` in the
  first draft), matching `features_<short>.parquet`.
- **OK — no change:** `fetch_candles`/`_atomic_to_parquet` signatures, `bar`
  format (`"1H"` passed straight through), same-dir `mkstemp` (no cross-device
  `os.replace`), bash `set -e` vs. Python continue-on-error (a single `python -m`
  call, exit code passed through), and UTC-hourly index alignment.

---

## Section 1 — Architecture & data flow

```
python -m research.pipeline.refresh_ohlcv [--manifests-dir ...]
  cfg = load_config()                         # honors RESEARCH_ONLY_SYMBOL / RESEARCH_INTERVAL
  mdir = resolved once (default: _REPO_ROOT / cfg.feature_store_path, which
         _apply_interval_override already namespaces as .../15m under RESEARCH_INTERVAL;
         or the explicit --manifests-dir). READ and WRITE use this SAME dir.
  for sym_cfg in cfg.symbols:                 # btc/eth/sol (or the single --only symbol)
    feats = load_features(sym_cfg.name, mdir) # raises FileNotFoundError if absent
    if feats.empty:  FAIL this symbol         # .index.min() would be NaT
    days  = (datetime.now(timezone.utc) - feats.index.min()).days + BUFFER_DAYS  # tz-AWARE both sides
    ohlcv = fetch_candles(sym_cfg.okx_swap, days, bar=cfg.interval)   # OKX, deep endpoint
    try:
        _align_ohlcv(ohlcv, feats.index)      # REUSE the run's own gate (close >=95%);
    except ValueError:                         # a ValueError == not covered -> fail loud
        FAIL this symbol (do not write a gappy parquet)
    short = _symbol_short(sym_cfg.name)        # matches features_<short>.parquet naming
    _atomic_to_parquet(ohlcv, mdir / f"ohlcv_{short}.parquet")        # temp + os.replace, same dir
  exit 0 if every symbol ok else 1            # old parquets left intact on failure
```

### N1 — `research/pipeline/refresh_ohlcv.py`

One module, one job: for each configured symbol, fetch full-span OKX candles,
validate coverage of that symbol's feature index, and atomically write
`ohlcv_<sym>.parquet` into the same (interval-aware) manifests directory
`load_features` reads from — so the file the Foundry aligns against sits next to
the features it must cover.

- **Same dir for read and write.** Resolve the manifests dir once — the default
  is `_REPO_ROOT / cfg.feature_store_path`, which `_apply_interval_override`
  already namespaces (`.../15m` under `RESEARCH_INTERVAL`), matching what
  `load_features` reads. The OHLCV is written into that same dir, so under any
  interval the file the Foundry aligns against sits beside the features it covers.
  Filename `ohlcv_<short>.parquet` via `_symbol_short` (matching
  `features_<short>.parquet`).
- Depth from features: `days = (datetime.now(timezone.utc) - features.index.min()).days
  + BUFFER_DAYS` (`BUFFER_DAYS = 3`). Both sides are tz-aware (the feature index is
  UTC-aware; use `datetime.now(timezone.utc)`, **not** naive `utcnow()`, which would
  raise `TypeError` on the subtraction). An empty features frame (`.index.min()` →
  `NaT`) fails the symbol before any fetch.
- Coverage gate: **reuse the run's own `_align_ohlcv(ohlcv, features.index)`**
  (import it from `research.hermes.orchestrator`). A returned frame means covered;
  a raised `ValueError` means not, and the symbol fails loud. Reusing it — rather
  than re-implementing `close` non-NaN ≥ 0.95 — keeps one source of truth, so a
  future threshold change can't let the refresh pass a table the run rejects.
- Atomic write via `_atomic_to_parquet` (temp file in the same dir + `os.replace`),
  so a Foundry run reading `ohlcv_<sym>.parquet` concurrently never sees a
  half-written file (same single-writer/atomic convention as the candidate parquet,
  design C-8).
- Per-symbol independence: catch a symbol's failure, log it, continue to the next;
  return exit 1 if any failed (mirrors stage0a's `compute_exit_code`), so a nightly
  cron surfaces the failure while still refreshing the coins that could.

### N2 — `scripts/refresh_foundry_ohlcv.sh`

A thin bash wrapper mirroring `scripts/refresh_factors.sh`: optionally activate
`RESEARCH_VENV`, `cd research`, run `python -m pipeline.refresh_ohlcv`, pass the
exit code through. This is the cron entry point.

---

## Section 2 — Components & files

| File | Responsibility |
|---|---|
| `research/pipeline/refresh_ohlcv.py` (create) | fetch + coverage-validate + atomic-write `ohlcv_<sym>.parquet` per configured symbol; `main()` loops symbols and returns 0/1 |
| `scripts/refresh_foundry_ohlcv.sh` (create) | thin cron wrapper (venv + `python -m`, exit-code passthrough) |
| `research/tests/test_refresh_ohlcv.py` (create) | unit tests (mock `fetch_candles`; no network) |

Reused as-is: `pipeline.config.load_config` (+ its `RESEARCH_ONLY_SYMBOL` /
`RESEARCH_INTERVAL` handling), `lib.okx_data.fetch_candles`,
`lib.factor_io.load_features` + `_atomic_to_parquet` + `_symbol_short`, and
`hermes.orchestrator._align_ohlcv` (the coverage gate — imported so refresh and
run share one definition of "covered"; the leading-underscore coupling is
intentional here, not a smell).

---

## Section 3 — Error handling & honest boundaries

| situation | behaviour |
|---|---|
| `features_<sym>.parquet` absent | `load_features` raises `FileNotFoundError`; that symbol fails; others continue; exit 1 |
| `features_<sym>` present but empty | fail that symbol before fetch (`.index.min()` would be `NaT`); others continue; exit 1 |
| OKX fetch raises (network/API) | that symbol fails, logged; old `ohlcv_<sym>.parquet` intact (never written); other symbols continue; exit 1 |
| coverage insufficient (`_align_ohlcv` raises `ValueError`) | that symbol fails loud (OKX gap); no parquet written; exit 1 |
| all symbols succeed | each `ohlcv_<sym>.parquet` atomically replaced; exit 0 |
| `RESEARCH_ONLY_SYMBOL=eth` | only eth is fetched (config filter); exit reflects that one |

**Honest boundaries:**

1. **Full re-fetch is slow.** ~350 requests/coin for ~4 years of 1H bars; 1–3 min
   per coin against the live OKX API. Fine nightly, not instant.
2. **Coverage uses `close` non-NaN**, identical to `_align_ohlcv`, so passing this
   gate is exactly what lets the Foundry run align — no second, divergent notion of
   "covered".
3. **Freshness is not gated here.** The script produces the latest data each run;
   whether a *stale* parquet (old cron) is acceptable is the operator's call. The
   Foundry evaluates only the pre-OOS window, so a stale tail past `oos_start` does
   not change a verdict — unlike the trader, which is why this needs no
   `FACTOR_MAX_AGE_DAYS` equivalent.
4. **1H floor.** Honors `RESEARCH_INTERVAL` for parity with the pipeline, but the
   constitution's sub-1H ban is unchanged; this spec adds no intraday capability.

---

## Section 4 — Test matrix (no network)

`research/tests/test_refresh_ohlcv.py` — every test monkeypatches `fetch_candles`:

| test | assertion |
|---|---|
| `test_writes_ohlcv_parquet_next_to_features` | mock returns a full-span frame → `ohlcv_eth.parquet` written in the **same dir** `load_features` read `features_eth` from, columns `open..volume`, UTC index |
| `test_depth_derives_from_features_earliest` | `fetch_candles` called with `days >= (now_utc - features.index.min()).days`; the subtraction uses tz-aware `datetime.now(timezone.utc)` (no `TypeError`) |
| `test_gappy_fetch_fails_via_align_ohlcv` | mock returns a frame covering < 95% → `_align_ohlcv` raises → that symbol fails, **no parquet written**, exit 1 |
| `test_missing_features_fails_that_symbol` | no `features_<sym>` → `FileNotFoundError` for that symbol, others still processed, exit 1 |
| `test_empty_features_fails_before_fetch` | an empty `features_<sym>` → symbol fails, `fetch_candles` **not called** for it, exit 1 |
| `test_atomic_write_used` | the write goes through `_atomic_to_parquet` (a pre-existing `ohlcv_<sym>.parquet` is replaced, never truncated in place) |
| `test_only_symbol_env_limits_fetch` | `RESEARCH_ONLY_SYMBOL=eth` → only eth fetched |
| `test_one_symbol_failure_does_not_block_others` | btc fetch raises, eth succeeds → eth parquet written, exit 1 |

### Honestly-recorded gap

- The **live OKX endpoint** is exercised only when the script is actually run
  (manually or by cron); tests monkeypatch `fetch_candles`, so they prove the
  fetch→validate→write→exit logic, not OKX connectivity. The one-time real fetch
  that produces the committed/served parquets is the connectivity proof.

---

## Runbook (enabling a real Foundry run)

```bash
# 1) refresh OHLCV for all configured coins (writes research/manifests/ohlcv_<sym>.parquet)
bash scripts/refresh_foundry_ohlcv.sh            # or: RESEARCH_ONLY_SYMBOL=eth python -m research.pipeline.refresh_ohlcv

# 2) enqueue a foundry job against the real manifests
python -m research.hermes.foundry_runner enqueue --symbol eth --runs-dir runs \
  --oos-start 2025-01-01 --ohlcv-path research/manifests/ohlcv_eth.parquet

# 3) run under the spend gate + daily ceiling (real LLM)
python -m research.hermes.foundry_runner run --runs-dir runs \
  --manifests-dir research/manifests --zoo-dir agent/src/factors/zoo \
  --image talos-sandbox:test --llm openai --model gpt-4o-mini \
  --i-will-spend-real-money --batch-max-llm-calls 6 --daily-max-llm-calls 40
```

---

## Self-review

- **Placeholders:** none. `BUFFER_DAYS = 3` and the 95% threshold are concrete;
  symbols come from config.
- **Consistency:** the coverage gate is the run's own `_align_ohlcv` (imported,
  not re-implemented), so the file that passes here is by definition what the run
  accepts. Per-symbol-independent exit 0/1 matches stage0a's convention. Read and
  write use one resolved (interval-aware) manifests dir.
- **Scope:** one plan — a fetch/validate/write module + a cron wrapper + tests. No
  new subsystem, no changes to `orchestrator`/`foundry_runner` (only an import of
  `_align_ohlcv`).
- **Ambiguity:** depth is `datetime.now(timezone.utc) - features.index.min() + 3d`;
  failure is per-symbol with a non-zero aggregate exit; the output path is the same
  interval-aware manifests dir `load_features` reads, `ohlcv_<short>.parquet`.
