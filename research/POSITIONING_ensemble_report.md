# BTC / ETH Positioning-Ensemble Pipeline Report

**Date:** 2026-06-22
**Goal:** Run the full alpha pipeline (stage 0a→5) for BTC & ETH using the Binance
OI / Long-Short positioning factors (`global_ls_acct_z`, `toptrader_ls_z`,
`ls_divergence`) wired into stage 0a (commit `649f6e6`). Question: do positioning
factors yield a deployable strategy on BTC/ETH the way `ls_divergence` did on SOL
(`sol_s1_single_factor_regime`, OOS sharpe 1.79)?

**Verdict: NO promotable strategy on either symbol.** Positioning alpha on BTC/ETH
is real-but-thin and is outranked by incumbent factors; the one ETH strategy that
carried a positioning factor peaked at **OOS sharpe 0.19**, far below the 1.0
promote gate.

---

## BTC — positioning sub-threshold, no strategy built

stage 0a rebuilt evidence (27 factors). Positioning factor ranks by top |IC|:

| factor | top \|IC\| | rank | IR | passes \|IC\|≥0.05 gate? |
|---|---|---|---|---|
| `global_ls_acct_z` | −0.0399 @72h | #9 | +0.402 | ❌ |
| `ls_divergence` | −0.0324 @72h | #12 | +0.357 | ❌ |
| `toptrader_ls_z` | −0.0250 @168h | #20 | −0.112 | ❌ |

All three **clear the IR gate but fail the |IC|≥0.05 gate**, and are buried under
8 stronger incumbents (`stablecoin_supply_z` 0.083, `basis_rel` 0.059, `funding_z`
0.059, `atr_14`, `oi_mom`, `obv`, `oi_z`, `sma_cross`). Default deterministic
selector top-6 = `[stablecoin_supply_z, basis_rel, funding_z]` — **positioning never
enters the candidate set.** Forcing it in would require dropping `min_abs_ic` to
~0.03 **and** raising `max_candidates` to ≥12 (a 12-way noise soup), which is
iterating-against-evidence. Per honest-gate principle: **reported negative, BTC
strategy build skipped.**

> Matches prior recon: BTC positioning is "ensemble_only grade, a gate not a
> standalone" — and here it cannot even earn a candidate slot vs incumbents.

---

## ETH — positioning qualifies, but no deployable alpha

stage 0a evidence: positioning factors **do** clear the gate and land in the
default top-6:

| factor | top \|IC\| | rank | passes gate? | regime IC (bull / bear) |
|---|---|---|---|---|
| `toptrader_ls_z` | −0.0882 @168h | #3 | ✅ | bull −0.161 / bear +0.021 |
| `ls_divergence` | +0.0773 @168h | #4 | ✅ | bull +0.153 / bear ~0 |
| `global_ls_acct_z` | +0.0069 @8h | #27 | ❌ (dead) | — |

ETH candidates (6) = `atr_14, stablecoin_supply_z, toptrader_ls_z, ls_divergence,
funding_z, oi_mom` — **all verdict `ensemble_only`** (conditional stability). The
positioning IC is **bull-dependent** (strong in bull, flips/vanishes in bear) — a
red flag for the 2025-26 OOS window which contains bear stretches.

### Strategies the deterministic router built

- `eth_s1_single_factor` = **`atr_14`** (top |IC|; volatility, *not* positioning)
- `eth_s2_trend_with_gate` = **`ls_divergence`** (trend) gated by **`atr_14`**
- + `_regime` variants of each
- `consensus_all` not emitted (router only fires it for 2–3 factors; here n=6)

> **Structural caveat:** the strongest ETH positioning factor `toptrader_ls_z`
> (−0.088) was **never tested** — `trend_with_gate` picks one best-positive +
> one best-negative factor, and `atr_14` (also ≈−0.088, but volatility) won the
> single gate slot. So only `ls_divergence` reached a strategy.

### Walk-forward OOS (2025-01-01 .. 2026-06-22)

| strategy | train best sharpe | **OOS sharpe** | OOS ret | OOS DD | OOS trades | stage3-diag | selected |
|---|---|---|---|---|---|---|---|
| `eth_s2_trend_with_gate_regime` (ls_div+atr+regime) | +0.327 | **+0.19** | +1.7% | −9.0% | 33 | back_to_stage_4 | ❌ |
| `eth_s2_trend_with_gate` (ls_div+atr) | −0.095 | **−0.04** | −4.1% | −23.8% | 49 | back_to_stage_2 | ❌ |
| `eth_s1_single_factor` (atr) | −1.567 | n/a (not optimized) | — | — | — | back_to_stage_2 | ❌ |

Regime overlay is what lifts the positioning strategy from negative to +0.19 train→OOS;
even so it sits well under the 1.0 OOS gate. Stage 5: **0 new strategies selected.**
Only the pre-existing `eth_s5_half_size` (OOS 1.02) remains selected — unchanged.

---

## Why SOL worked and BTC/ETH didn't

`ls_divergence` on **SOL** was IC −0.10 (single_use, rank #1) → drove `sol_s1` to OOS
1.79. On **ETH** the same factor is +0.077 (#4, ensemble_only) and bull-dependent; on
**BTC** −0.032 (#12, below gate). Positioning is a genuine but **symbol-specific and
thin** signal — strong enough to lead on SOL, only a minor gate on ETH, sub-threshold
on BTC.

## Caveats / not-exhausted

- `toptrader_ls_z` (ETH's strongest positioning factor) was never given a standalone
  or gate test by the auto-router (lost the slot to `atr_14`). A hand-built
  `toptrader` contrarian single_factor was **not** attempted (would be curation; and
  its bull-dependence argues against OOS survival). If revisited, that is the one
  untested angle.
- OI parquet tail ends 2026-06-16 (~6 days stale); immaterial to a 1.4yr OOS verdict.
- BTC was deliberately not forced past the candidate gate (honest-gate principle).

## Artifacts touched (uncommitted)

- `research/manifests/{evidence,candidates,features}_{btc,eth}.*` regenerated
- `research/strategies/strategy_eth_s{1,2}_*.yaml` + compiled `signal_engine.py`
- `research/manifests/eth_s{1,2}_*/{generation,diagnosis,optimization,manifest}.json`
- `research/strategy_runs.json` (auto-registered new eth strategies + sweep/walk_forward runs)
- `research/manifests/selection.json`
- new dep installed into `.venv`: `polars` (stage 0a orderflow import; was missing)
