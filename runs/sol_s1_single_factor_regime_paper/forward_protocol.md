# sol_s1 paper-forward protocol (frozen 2026-06-22)

Pre-registered **before** the first live paper bar. Numbers and verdict thresholds
below are FROZEN — moving them after seeing forward data voids the honest gate.

## Strategy (frozen)
`sol_s1_single_factor_regime` — `ls_divergence` contrarian single-factor + regime overlay.
- `SIZE_MULT = 1.0` (alpha verdict is size-invariant; size is a post-hoc risk lever, not tuned here).
- stage-4 TRAIN-best params (match `manifests/sol_s1_single_factor_regime/optimization.json`):
  `LOOKBACK_DAYS=60, ENTRY_LOW_PCT=15, ENTRY_HIGH_PCT=90, HOLD_MAX_HOURS=96, SL_PCT=4.0, TP_PCT=5.5, PERSIST_K=5, PERSIST_M=2`.
- Engine file is `# manual: do-not-overwrite` (stage2b skips it); run-dir copy at `code/signal_engine.py`.

## Baselines
| Measurement | OOS sharpe | Note |
|---|---|---|
| Lag-0 (no freshness penalty) | **1.659** | `laggate_result.json`; optimistic ceiling, OOS-polluted (see below) |
| Live freshness (lag ≈ 1h) | **1.643** | decay sweep 2026-06-22 (commits 27949c9..3d09e45); ≈ lag-0 — the hourly live cron yardstick |
| Archive lag-24h | 0.785 | original NO-GO — **a freshness artifact (T+1 ZIP lag), NOT edge decay** |
| Edge crossover | — | sharpe drops below 1.0 at ~16–18h of factor staleness |

**OOS-pollution caveat (Gemini-confirmed):** the size_mult grid was narrowed *after* seeing
the full-size OOS DD fail — i.e. iterated against the holdout. The 1.6–1.79 ceiling is therefore
holdout in-sample and optimistic. True forward expected **~1.0–1.5**.

## Live-data freshness requirement
The edge survives only while factors stay fresher than the ~16–18h crossover. The hourly
live-endpoint cron (`dump_oi --live` + stage0a + stage1) keeps `index_end` within ~1–2h.
- Deploy with **`FACTOR_MAX_AGE_DAYS=0.5`** (12h) — tighter than the 2d default. Live endpoint
  makes `index_end` genuinely fresh, so a tight guard is safe AND necessary: if the cron dies,
  trading must PAUSE before staleness crosses ~16h, or the forward record is corrupted by
  dead-edge trades.

## Pre-registered verdict (DO NOT MOVE)
First gate at ~30 trades / 3–5 months:
- **CONTINUE** → extend toward 100+ trades, if BOTH:
  - forward sharpe **≥ 1.0** (clears the gate threshold and the honest-discount floor), AND
  - full-size DD controllable **< 15%** via `size_mult ≥ 0.4`.
- **DISCARD** if: forward sharpe clearly **< 0.5**, OR DD uncontrollable, OR persistent stale-factor pauses.
- Iteration on forward numbers is FORBIDDEN. Monitoring = liveness/freshness only.

## Deploy facts
- mode=paper (mainnet public data + virtual fills, no keys), symbol `SOL/USDT:USDT`, interval 1H.
- `KILL_TERMINATE_DD=0.20` — above full-size expected DD so normal drawdown does not auto-kill the run.
- testnet_id `sol_s1_single_factor_regime_paper`.
