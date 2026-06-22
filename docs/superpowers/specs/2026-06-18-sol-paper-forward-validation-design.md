# SOL `sol_s1` — Live Paper-Forward Validation Design

**Date:** 2026-06-18
**Status:** Draft for review (Gemini-reviewed v2)
**Depends on:** `f8bbd3b`→`649349b` (positioning factors + OI archive wired into stage0a),
  eth_s5 paper-trade runbook (`runs/eth_s5_half_size_oos/code/signal_engine.py`,
  `scripts/refresh_factors.sh`, `dashboard/trader/loop.py --mode paper`)
**Memory:** [[project_positioning_strategy_autobuild]], [[project_mainnet_paper_dryrun]],
  [[deploy_dashboard_testnet_runbook]], [[deploy_curated_strategies_gitignored]],
  [[project_orderflow_poc_results]] (entry-lag-audit-first lesson)

## Goal

Honestly forward-validate `sol_s1_single_factor_regime` (ls_divergence contrarian +
regime overlay) on **data unseen at selection time**, to decide whether its OOS sharpe
1.79 is real alpha or an artifact of OOS pollution.

The selection was **iterated against the holdout** (size grid shrunk to `[0.3,0.6,0.1]`
only after seeing full-size OOS DD fail) — so 1.79 is holdout-in-sample and optimistic.
The **only** honest gate is forward on never-seen bars. **This is a validation, not a
deployment.** A forward result that *deflates* 1.79 is a successful outcome.

**Forward is judged against a lag-realistic baseline (Phase 0), not against 1.79.**

## Why this needs new code (the architecture gap)

The live signal path feeds the engine **OHLCV only**:
`dashboard/trader/signal.py::compute_signal` → `_fetch_ohlcv(exchange, …)` →
`data_map = {symbol: ohlcv_df}` → `engine.generate(data_map)`. Non-price factors
(funding / OI / L-S / stablecoin) are **never passed in**. A `ls_divergence`-driven
strategy cannot compute its signal from the live path alone.

Proven workaround = the **eth_s5 curated-engine pattern**: the engine reads its non-price
factors itself from the refreshed factor store, and a daily cron keeps that store fresh.
We replicate it for SOL.

---

## Phase 0 — Lag-realistic backtest gate (do this FIRST, before any deploy)

**Rationale (Gemini, and our own [[project_orderflow_poc_results]] entry-lag lesson):**
the backtest used **lag-free, perfectly-aligned** `ls_divergence`. Live, the Binance
daily-metrics archive publishes ~T+1, so the freshest factor is ~24h stale. Trading on
yesterday's positioning is a *different, weaker* information set. Before spending months
of calendar time, measure that gap cheaply.

- Re-run the `sol_s1` backtest (train **and** OOS) with `ls_divergence` shifted forward
  by the **realistic live-availability lag** (≥24h; pin the exact lag from the archive's
  observed publish delay) — every other factor/price unchanged.
- This **lagged baseline sharpe** is the number the live forward will be judged against,
  **not** 1.79.
- **Go/No-Go:** if the lagged-baseline OOS sharpe is already below the deploy threshold
  (~1.0), the live edge is likely gone to latency. **Do not deploy** — either solve the
  data-latency problem first (faster OI/L-S source) or abandon. Months saved.
- Run at `size_mult = 1.0` (alpha is size-invariant; see below).

Phase 0 is a few hours of backtest work and can kill the whole effort before any infra
is built. It gates Phase 1.

**Phase 0 result (2026-06-18):** `baseline_lag0_sharpe=1.659`, `lagged_24h_sharpe=0.785`,
`threshold=1.0` → **NO-GO**. The 24h lag cuts sharpe from 1.659 → 0.785, below the 1.0
deploy threshold. Phase 1 does not proceed until the data-latency problem is resolved
(faster OI/L-S source) or a lower-lag variant of ls_divergence is found.
Result archived at `runs/sol_s1_single_factor_regime_paper_laggate_result.json`.

---

## Phase 1 — Paper-forward harness (only if Phase 0 passes)

### ① Curated signal engine (parameterized)
`runs/sol_s1_single_factor_regime_paper/code/signal_engine.py`, modeled on
`runs/eth_s5_half_size_oos/code/signal_engine.py`:

- Line 1 `# manual: do-not-overwrite` → stage2b skips it → **immune to pipeline
  overwrite** (resolves the "manual patch evaporates on stage2 rerun" blocker).
- `generate(data_map)`: price from `data_map[SYMBOL]`;
  `from lib.factor_io import load_factor_values` → `load_factor_values("sol")`, read
  `ls_divergence`, strip tz, `reindex(ohlcv.index, method="ffill")`, smooth; percentile
  transform; **contrarian** entry (negative IC → long at low pct, short at high);
  regime overlay via `_load_regime_series("sol", …)` (bear→no long, bull→no short);
  exit state machine (time / TP / SL / signal-invalidation neutral band);
  `signal = position * SIZE_MULT`.
- **Params hard-coded and frozen** from local reconstruction train-best (see below).
- **`SIZE_MULT = 1.0` for the validation run** — the alpha verdict (sharpe) is
  size-invariant; `size_mult` is *not* a tested parameter, it is a post-hoc risk lever
  (see protocol). Keep it a single named constant so the harness stays generic and a
  second strategy is a cheap copy (⑤ decision: build generic, run sol_s1 only).
- Hourly bars (compiled-pipeline convention — see technical notes below).

### ② Factor refresh cron — extend to SOL + OI + a health guard
`scripts/refresh_factors.sh` currently refreshes **btc + eth**. For SOL:

- Add a `python -m dump_oi` (or equivalent) step **before** `stage0a_features` so
  `research/data/oi/oi_SOLUSDT_1H.parquet` is current; else `ls_divergence` freezes.
- Extend stage0a/stage1/stage2.5 to cover SOL (writes `factor_values_sol.parquet` +
  `regime_sol.json`).
- **Cron-health guard (Gemini ③, corrected).** The factor is **inherently ~24h lagged
  by nature** (daily archive) — so a naive "pause if factor older than 12h" check is
  WRONG (it would pause permanently). The right guard watches **refresh recency**: alert
  / pause if `factor_values_sol.parquet` has not been **rewritten** in > ~26–30h (i.e.
  the cron itself stalled), distinct from the data being one day old normally. The
  existing `FACTOR_MAX_AGE_DAYS = 2` window must stay ≥ the worst-case archive publish
  lag so a healthy daily run is never misflagged stale.

### ③ Paper deployment (server)
Same as the eth_s5 runbook ([[deploy_dashboard_testnet_runbook]]):

- `loop.py --mode paper` → `PaperBroker` on Bybit mainnet public data, virtual fills
  (taker 0.055% + 5 bps slippage + 8h funding), `testnet_id = sol_s1_…_paper`.
- `--symbol SOL/USDT:USDT`, `--interval 1H`, run-dir = the curated engine dir.
- `KILL_TERMINATE_DD` set **above** full-size expected DD (full size ≈ the lagged
  baseline's DD) so normal drawdown does not auto-terminate the forward run.
- Monitored via the existing dashboard Testnet tab (equity / trades / vs-backtest).
- Server, not local: forward needs 24/7 for months.

## Two technical notes

**The strategy runs on HOURLY bars, not 8h** (verified against the compiled pipeline).
The signal compiler hardcodes `rolling(lookback_days*24, …)` (1 bar/hour) and the compiled
`sol_s2` engine iterates the hourly OHLCV index — so the yaml's `timeframe_signal: 8h` is
**nominal metadata**, and `sol_s1` was in fact backtested at 1H. The live curated engine
therefore mirrors the eth_s5 / compiled-`sol_s2` hourly pattern exactly (factor reindexed
`ffill` to the hourly bar index, percentile over `lookback_days*24`). **No 8h resample;
no Bybit-timeframe problem** — `--interval 1H` is natively supported. (Funding is applied
per-8h by the engine/PaperBroker regardless of bar grid, consistent with the backtest.)

**Spec source = local reconstruction.** The server winner carries the manual size patch;
the local copy is the `atr_volatility` false-negative. Local materials are complete
(`oi_SOLUSDT_1H.parquet` present, positioning wiring pushed). Re-run
`RESEARCH_ONLY_SYMBOL=sol` stage0a→stage4 with `--force`, take **train-best** entry / SL /
TP / lookback. **OOS is never consulted** for any param. Self-contained, deterministic.

## Honest-forward protocol (the core discipline)

1. **Size is decoupled from the alpha verdict (Gemini ①).** Validate at `size_mult = 1.0`.
   Memory shows sharpe is ~size-invariant (1.797→1.789) and `size_mult` only scales DD —
   so it is **not** a parameter that needs OOS to choose. Pick `size_mult` *after* the
   forward DD is known, purely to hit a DD target. The earlier "pin 0.6" was an OOS-
   informed leak; this removes it.
2. **Freeze params now; never re-tune on forward data.** A bad-looking forward is not
   license to re-touch the grid — that is the exact error that polluted the OOS.
3. **Verdict is probationary, not one-shot (Gemini ④).** 30 trades on an SR≈1 estimate
   has a large standard error — too few to *conclude* alpha. So:
   - **Continue-gate** (~30 trades / 3–5 months): forward sharpe **≈ the Phase-0 lagged
     baseline** (within its CI) AND full-size DD controllable to < 15% by a sane
     `size_mult` (≥ ~0.4) → **extend** the probation toward 100+ trades.
   - **Discard** if forward sharpe falls clearly below the lagged baseline / below ~0.5,
     or DD is uncontrollable. Failure is terminal; passing is provisional.
4. **Single-look discipline on iteration.** Interim monitoring = liveness only
   ("is it trading / did the cron break"), never parameter iteration on forward numbers.
5. **Calendar cost:** 8h, ~80 trades/yr ≈ 1.5/week → ~3–5 months to the first gate, more
   to a firm verdict. No shortcut.

## Testing

- Curated engine unit test: synthetic OHLCV + synthetic `factor_values_sol` parquet →
  `generate()` returns a signal series of `{-1.0, 0, +1.0}` (size 1.0); contrarian
  direction (low-percentile `ls_divergence` → long); regime mask zeroes disallowed bars.
- Hourly convention: engine percentile windows use `lookback_days*24` and iterate the
  hourly OHLCV index (matches compiled `sol_s2`); factor reindexed `ffill` to hourly.
- Refresh-cron dry-run: SOL path produces `factor_values_sol.parquet` containing
  `ls_divergence` with `index_end` inside the freshness window; cron-health guard fires
  when the parquet mtime is artificially aged > 30h.
- Reuse existing `trader/` tests (loop args, broker factory, freshness, paper broker).
- Research vs dashboard/agent suites run **separately** (sys.path conflict).

## Acceptance

- **Phase 0:** a lag-shifted (`≥24h`) `sol_s1` backtest produces a lagged-baseline sharpe;
  the deploy decision is explicitly gated on it (no deploy if < ~1.0).
- Local reconstruction reproduces `ls_divergence` as SOL's top-|IC| single factor with
  the screened **negative** sign at 72h/168h (sanity vs [[project_ls_positioning_recon]]).
- Curated engine loaded by `compute_signal` on live 1H SOL data emits a non-stale signal
  in `{-1, 0, +1}`; the trader places paper orders (not stuck "paused").
- `factor_values_sol.parquet` refreshes daily within the 2-day window; the cron-health
  guard distinguishes a stalled cron from normal 1-day data age.
- Frozen params + the pre-registered verdict (incl. the Phase-0 baseline) are written to
  a `forward_protocol.md` in the run dir **before** the first live bar — no post-hoc edits.

## Non-goals (explicit)

- **No production deployment.** Forward validation only; deploy is a separate decision
  *after* the verdict.
- **No curated-freeze-for-deploy** (permanent eth_s5-style fork). The
  `# manual: do-not-overwrite` engine here makes the *paper run* stable, not a maintained
  deployed fork ([[project_positioning_strategy_autobuild]] flagged that as maintenance debt).
- **No multi-strategy portfolio now (⑤).** The harness is built generic so a 2nd/3rd
  strategy is a cheap copy, but this change runs **sol_s1 only**. (Other SOL variants did
  not pass selection; low forward value.)
- **No BTC/ETH positioning overlay.** Separate follow-on research track.
- **No stage4 DD-aware selection rework.** The systemic fix for the size-grid pollution
  root cause is out of scope (`stage4_optimize.py::rank_combos` ranks train-sharpe only).
- **No new dashboard features.** The existing Testnet tab is sufficient.
