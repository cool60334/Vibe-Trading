# Live L/S Endpoint — `ls_divergence` Freshness Fix (Design)

**Date:** 2026-06-22
**Status:** Draft for review (self-reviewed; Gemini CLI unavailable — free-tier deprecated [[reference_gemini_cli_tier_deprecated]])
**Depends on:** `1ed43ae` (Phase-0 frozen engine + lag gate), `488c54f` (OI/L-S archive ETL)
**Memory:** [[project_sol_paper_forward_phase0]], [[project_oi_data_source_binance_archive]], [[project_ls_positioning_recon]]

## Goal

Source `ls_divergence`'s two inputs (`global_ls_accounts`, `toptrader_ls_positions`)
from Binance's **live** REST L/S endpoints so the live trader sees minute-fresh
positioning data, instead of the daily-archive's ~24h-lagged data which the Phase-0
gate proved kills the edge.

## Background — why (Phase-0 result + decay sweep)

The Phase-0 lag gate was **NO-GO**: lag-0 OOS sharpe 1.659 → lag-24h 0.785 (< 1.0).
The 2026-06-22 decay sweep showed the drop is **gradual, not a cliff**, with the
threshold crossover near 16–18h:

| lag | 0 | 1h | 2h | 4h | 8h | 12h | 24h |
|---|---|---|---|---|---|---|---|
| OOS sharpe | 1.659 | 1.643 | 1.561 | 1.432 | 1.229 | 1.147 | 0.785 |

So the edge is **real**; the NO-GO was purely the archive's ~24h publish lag. Any data
source fresher than ~12h clears the threshold; a ~5min live source recovers ≈1.64.
The fix is a freshness upgrade, not a strategy change.

## Architecture / Components

The archive ETL stays the system of record for history. A live tail is fetched and
overlaid onto the recent end of the same hourly parquet. Three small units + one gate:

### ① `lib/binance_dump.py` — `fetch_live_ls_ratios(symbol, period="5m", limit=500)`
The network layer (today: archive ZIPs) gains a live JSON fetcher. Two public, key-free
GETs against `https://fapi.binance.com`:
- `/futures/data/globalLongShortAccountRatio?symbol=<S>&period=<p>&limit=<n>`
  → `longShortRatio` ⇒ **`global_ls_accounts`** (account/count-based — matches archive
  `count_long_short_ratio`).
- `/futures/data/topLongShortPositionRatio?symbol=<S>&period=<p>&limit=<n>`
  → `longShortRatio` ⇒ **`toptrader_ls_positions`** (position/notional-based — matches
  archive `sum_toptrader_long_short_ratio`). **Not** the `…AccountRatio` variant.

Returns a 5-min DataFrame indexed by UTC `timestamp` (ms→datetime), two columns. Network
errors raise (caller decides fallback).

### ② `lib/oi_metrics.py` — `merge_live_tail(archive_hourly, live_5min)`
- Aggregate the live 5-min frame to 1H with the **same convention** as
  `aggregate_to_hourly` (`resample("1h", label="left", closed="left")`, snapshot `last`).
- **Overlay** the two L/S columns onto `archive_hourly`, **live precedence** on the
  overlapping hours; rows newer than the archive's end are appended.
- **Only** `global_ls_accounts` + `toptrader_ls_positions` are patched. `oi`, `oi_usd`,
  `taker_buysell_ratio` keep their archive values (NaN in any not-yet-published tail) —
  `sol_s1`/`ls_divergence` do not use them. (Documented limitation, not a bug.)

### ③ `dump_oi.py` — `--live` flag
After the archive `load_oi_range`, if `--live`: `fetch_live_ls_ratios` → `merge_live_tail`
→ `dump_oi_parquet`. The written parquet's `index_end` (and meta `generated_at`) now
reflect the live tail. Without `--live`, behaviour is byte-for-byte unchanged.

### ④ Reconciliation gate (correctness)
In the archive∩live overlap window, assert each live column matches its archive
counterpart: **Pearson corr ≥ 0.95 AND median |relative error| ≤ ~5%** (tolerance, not
exact equality — the two are computed independently). This catches a position-vs-account
mix-up (the variants diverge far more than 5%) and any unit/scale error. Run it once at
build time and in a test; surface a hard error on failure.

## Merge semantics — the traps (self-review)

- **Partial current hour:** the newest 1H bucket holds only the elapsed 5-min snapshots;
  `last` takes the most recent value — correct for a live "latest known" signal, but the
  bar is incomplete. Acceptable (we want freshest); note it.
- **Timestamp convention:** verify the live endpoint's `timestamp` is the **bucket start**
  so it aligns with the archive's left-closed hourly label. The reconciliation overlap
  check fails loudly if it's off by a bucket.
- **Coverage:** `limit=500 @ 5m ≈ 41h`. If the archive end is >41h stale, a small mid-tail
  gap stays NaN — harmless (the freshest hours, which drive live trading, are covered). If
  needed, raise `period`/issue a second call; not required for the daily cron.

## Validation (reframed — self-review)

**Re-running the lag-gate backtest does NOT validate this fix.** The backtest is dominated
by multi-year archive history; the live tail (~30d) barely touches the OOS window, so the
lag-0 backtest stays ≈1.659 regardless of wiring. The **decay sweep already served as the
re-gate** (lag-1h = 1.643 ⇒ GO once data is fresh). Therefore validation here is:

1. **Freshness measurement:** after `dump_oi --live`, the merged parquet's `index_end` is
   within minutes of now (not ~T-1). This confirms the endpoint actually delivers fresh
   data — the one assumption the whole effort rests on.
2. **Reconciliation gate** (④) passes.

The **real alpha proof remains the Phase-1 live forward**, judged against the lagged
baseline per [[project_sol_paper_forward_phase0]].

## 🚩 Critical dependency for Phase 1 (self-review)

Factor-store freshness = **min(data-source freshness, recompute cadence)**. This change
fixes the data source, but `factor_values_sol.parquet` is only as fresh as the
`stage0a → stage1` recompute that produces it. **If the refresh cron runs daily, the live
endpoint is wasted** — the factor store would still be ~daily-stale. So Phase 1's refresh
cron must run **≥ hourly** (and `dump_oi --live` must be its first step). This is a Phase-1
cadence requirement that this change creates; it is called out here so the Phase-1 plan
honours it. (The cron-health guard then tightens from ~30h toward ~2h.)

## Testing

- `fetch_live_ls_ratios`: parse a mocked JSON payload → two correctly-named columns, UTC
  index, ms→datetime; the position vs account endpoint mapping is asserted by URL.
- `merge_live_tail`: live precedence on overlap; rows appended past archive end; only the
  2 L/S columns change (oi/taker untouched); 5min→1H aggregation matches `aggregate_to_hourly`.
- Reconciliation: synthetic aligned series pass; an account-vs-position swap (divergent
  series) fails the corr/tolerance gate.
- `dump_oi --live` integration (mocked network): parquet `index_end` advances past the
  archive-only end; without `--live`, output unchanged.
- Research suite, run separately from dashboard/agent.

## Acceptance

- `python dump_oi.py --symbols SOLUSDT --live` produces `oi_SOLUSDT_1H.parquet` whose
  `index_end` is within ~1h of now, with `global_ls_accounts` + `toptrader_ls_positions`
  populated in the fresh tail.
- Reconciliation gate passes on real data (live ≈ archive in the overlap window).
- A run **without** `--live` is byte-for-byte unchanged (zero regression to the archive path).
- After `dump_oi --live` then `RESEARCH_ONLY_SYMBOL=sol stage0a→stage1`,
  `factor_values_sol.parquet`'s `ls_divergence` tail is fresh (index_end within ~1h).

## Non-goals (explicit)

- **No re-backtest as validation** (it can't test live freshness; see Validation).
- **No live sourcing for `oi`/`oi_usd`/`taker`** — `sol_s1` doesn't use them; archive-only
  is fine. (A later change can add them if an OI strategy goes live.)
- **No Phase-1 deploy / cron-cadence change here** — this change delivers the data path;
  the ≥hourly cron + guard tightening + paper deploy are the Phase-1 plan (which this
  change's 🚩 dependency feeds into).
- **SOL only.** `fetch_live_ls_ratios` is generic per-symbol, but only SOL is wired/run now.
- **No BTC/ETH positioning overlay** (separate follow-on).
