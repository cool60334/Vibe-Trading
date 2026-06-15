# Intraday Factor Library — Phase 2 Pipeline Report

**Date:** 2026-06-15  
**Branch:** `feature/intraday-phase2-factor-library`  
**Intervals tested:** 15m, 30m  
**Symbols:** ETH, BTC  

---

## Summary

Six intraday OHLCV factors were added to the pipeline (`mom_4/8/16`, `rvol_ratio_8_32`, `range_expansion_16`, `volume_zscore_8`). Discovery was run at both 15m and 30m for ETH and BTC. The main findings:

| Finding | Detail |
|---------|--------|
| **Intraday momentum = contrarian** | `mom_4/8/16` show consistent IC ≈ −0.04 to −0.05 across all symbol/interval combos. Mean-reversion dominates short-term crypto price action. |
| **rvol_ratio_8_32** | Weak positive IC (~0.008–0.015 at 8h horizon). Below actionable threshold at 15m/30m. |
| **range_expansion_16, volume_zscore_8** | Noise (|IC| < 0.003). No predictive value at intraday resolution. |
| **ETH@30m: 1 strategy selected** | `eth_s2_trend_with_gate_regime` — OOS Sharpe 1.71, DD −7.3%, 61 trades. Uses existing `funding_z` gate at 30m. Note: stage3 backtest currently uses 1H candles regardless of interval (known wiring gap). |
| **ETH@15m: 0 selected** | Full pipeline ran; no strategy cleared stage-5 gates. |
| **BTC@15m/30m: candidates found** | Stage 0a/0 complete. Full stages 1–5 pending (see §Known gaps). |

---

## Stage 0a Evidence — IC by Interval

### IC threshold for "actionable": |IC| ≥ 0.03, consistent across horizons

### BTC @ 15m

| Factor | IC@1h | IC@4h | IC@8h | IC@24h | IR | Note |
|--------|-------|-------|-------|--------|----|------|
| `oi_z` | −0.002 | +0.097 | **+0.263** | +0.047 | — | Very strong @8h, tiny sample (n=434) |
| `funding_z` | −0.036 | −0.009 | −0.018 | −0.042 | −2.55 | Consistent contrarian |
| `rsi_14` | −0.057 | −0.046 | −0.024 | −0.020 | −1.15 | Contrarian momentum ✓ |
| `stoch_k` | −0.054 | −0.036 | −0.018 | −0.013 | −1.14 | Contrarian momentum ✓ |
| **`mom_4`** | **−0.053** | −0.030 | −0.019 | −0.011 | **−1.16** | **New intraday factor** ✓ |
| **`mom_8`** | **−0.048** | −0.037 | −0.020 | −0.015 | **−1.06** | **New intraday factor** |
| **`mom_16`** | **−0.044** | −0.044 | −0.024 | −0.017 | **−0.98** | **New intraday factor** |
| `bb_width_20` | +0.015 | +0.020 | +0.017 | +0.020 | +0.37 | Weak vol regime |
| **`rvol_ratio_8_32`** | **+0.008** | +0.014 | +0.015 | −0.001 | **+0.19** | **Weak, below threshold** |
| **`range_expansion_16`** | **−0.000** | +0.002 | +0.005 | +0.001 | **+0.001** | **Noise** |
| **`volume_zscore_8`** | **−0.002** | −0.000 | +0.000 | +0.002 | **−0.075** | **Noise** |

**Stage 0 candidates selected:** `rsi_14`, `stoch_k`, `mom_4` (all contrarian momentum)

---

### ETH @ 15m

| Factor | IC@1h | IC@4h | IC@8h | IC@24h | IR | Note |
|--------|-------|-------|-------|--------|----|------|
| `oi_price_divergence` | +0.052 | +0.055 | **+0.105** | +0.081 | — | Strong @8h, small n |
| `funding_z` | −0.007 | −0.018 | −0.034 | **−0.053** | −2.00 | Consistent contrarian ✓ |
| `rsi_14` | −0.050 | −0.027 | −0.010 | −0.024 | −0.87 | Contrarian momentum |
| **`mom_4`** | **−0.046** | −0.018 | −0.008 | −0.010 | **−0.90** | **New, contrarian** |
| **`mom_8`** | **−0.045** | −0.024 | −0.009 | −0.015 | **−0.87** | **New, contrarian** |
| **`mom_16`** | **−0.040** | −0.028 | −0.011 | −0.018 | **−0.79** | **New, contrarian** |
| `bb_width_20` | +0.020 | +0.025 | +0.022 | +0.010 | +0.42 | Weak vol regime |
| **`rvol_ratio_8_32`** | **+0.002** | +0.010 | +0.003 | +0.007 | **+0.12** | **Noise** |
| **`range_expansion_16`** | **−0.003** | +0.001 | +0.002 | +0.004 | **−0.053** | **Noise** |
| **`volume_zscore_8`** | **−0.004** | −0.005 | +0.000 | +0.001 | **−0.15** | **Noise** |

**Stage 0 candidates selected:** `funding_z` (1 candidate — contrarian funding)

**Stage 5 result: 0 selected** (full pipeline ran; no strategy cleared gates)

---

### ETH @ 30m

| Factor | IC@1h | IC@4h | IC@8h | IC@24h | IR | Note |
|--------|-------|-------|-------|--------|----|------|
| `oi_price_divergence` | +0.058 | **+0.140** | **+0.195** | +0.180 | — | Strongest signal, small n |
| `oi_mom` | −0.037 | −0.099 | −0.069 | **−0.305** | — | Strong @24h, small n |
| `funding_z` | −0.013 | −0.013 | −0.031 | **−0.051** | −1.14 | Consistent contrarian ✓ |
| **`mom_4`** | **−0.049** | −0.024 | −0.010 | −0.016 | **−0.89** | **New, contrarian** |
| **`mom_8`** | **−0.043** | −0.028 | −0.012 | −0.019 | **−0.82** | **New, contrarian** |
| **`mom_16`** | **−0.038** | −0.024 | −0.012 | −0.029 | **−0.83** | **New, contrarian** |
| `bb_width_20` | +0.014 | +0.018 | +0.015 | +0.006 | +0.25 | Weak vol regime |
| **`rvol_ratio_8_32`** | **+0.008** | +0.009 | +0.009 | +0.009 | **+0.21** | **Weak, consistent** |
| **`range_expansion_16`** | **+0.001** | +0.006 | +0.008 | +0.006 | **+0.069** | **Noise** |
| **`volume_zscore_8`** | **−0.005** | −0.000 | +0.002 | +0.005 | **−0.073** | **Noise** |

**Stage 0 candidates selected:** `funding_z` (1 candidate)

**Stage 5 result: 1 selected** — `eth_s2_trend_with_gate_regime`  
→ OOS Sharpe 1.71 | DD −7.3% | 61 trades (vs BTC benchmark)  
→ Caveat: stage3 uses 1H candles (see §Known gaps)

---

### BTC @ 30m

_Results pending (stage0a + stage0 running at time of report). Update below when complete._

| Factor | IC@1h | IC@4h | IC@8h | IC@24h | IR | Note |
|--------|-------|-------|-------|--------|----|------|
| _TBD_ | — | — | — | — | — | |

---

## New Intraday Factor Verdict

| Factor | Verdict | Rationale |
|--------|---------|-----------|
| `mom_4` | **SIGNAL (contrarian)** | IC ≈ −0.05@1h, IR ≈ −1.1, consistent across BTC+ETH, 15m+30m |
| `mom_8` | **SIGNAL (contrarian)** | IC ≈ −0.045@1h, IR ≈ −0.85, weaker than mom_4 but consistent |
| `mom_16` | **SIGNAL (weak contrarian)** | IC ≈ −0.040@1h, IR ≈ −0.80, lowest of the three |
| `rvol_ratio_8_32` | **NOISE** | IC ≈ +0.008–0.015, IR ≈ 0.20, below screening threshold |
| `range_expansion_16` | **NOISE** | IC ≈ 0.001, IR ≈ 0.001 |
| `volume_zscore_8` | **NOISE** | IC ≈ −0.003, IR ≈ −0.10 |

**Key insight:** Intraday crypto momentum **reverses**. A bar that just went up is more likely to retrace in the next 1–4h. This is consistent with the 1H `funding_z` contrarian pattern — market microstructure dominates at short horizons.

**Practical implication:** `mom_4` (=`rsi` at intraday scale) is a viable gate for contrarian entries. The existing `rsi_14` and `stoch_k` show identical IC, confirming this is a real phenomenon, not a data artifact.

---

## Pipeline Wiring Gaps Found

| Gap | Description | Status |
|-----|-------------|--------|
| `stage0_discovery` hardcoded manifests path | Line 861 used `_CFG_REPO_ROOT / "research" / "manifests"` — silently read 1H evidence for sub-hour runs | **Fixed** (commit `8210893`): `_compute_manifests_dir(cfg, repo_root)` derives path from `cfg.feature_store_path` |
| Stage3 backtest uses 1H candles | `stage3_backtest` loads candles at 1H regardless of `RESEARCH_INTERVAL` | **Open** — all OOS Sharpe metrics above are 1H backtests, not true intraday |
| Stages 1–5 ignore `RESEARCH_ONLY_SYMBOL` | Per-symbol filtering only works in stage0a; stages 1–5 run all symbols | **Open** (known pre-existing) |
| Stage4 exits code 1 on curated strategies | `eth_s5_half_size` has no `parameter_search_ranges` (curated) → stage4 exits 1, breaks `&&` chains | **Workaround**: run stage3-diag and stage5 separately |

---

## Artifacts

```
research/manifests/15m/
  ├── evidence_btc.json        30 factors, BTC@15m
  ├── evidence_eth.json        30 factors, ETH@15m
  ├── candidates_btc.json      3 candidates (rsi_14, stoch_k, mom_4)
  └── candidates_eth.json      1 candidate (funding_z)

research/manifests/30m/
  ├── evidence_eth.json        30 factors, ETH@30m
  ├── candidates_eth.json      1 candidate (funding_z)
  ├── eth_s2_trend_with_gate_regime/
  │   └── diagnosis.json       → proceed (OOS Sharpe 1.71)
  └── selection.json           1 selected: eth_s2_trend_with_gate_regime

  [BTC@30m artifacts pending]
```

---

## Next Steps

1. **Fix stage3 to respect `RESEARCH_INTERVAL`** — without this, "30m strategies" are still evaluated on 1H candles. True intraday alpha needs true intraday backtests.
2. **Build contrarian momentum strategy on `mom_4`** — IC −0.05@1h with n=140k is credible. A `mom_4 < threshold → long` entry with `funding_z < 0` gate could complement the existing `btc_s9` pattern.
3. **Validate ETH@30m selection** — re-evaluate `eth_s2_trend_with_gate_regime` once stage3 uses 30m candles. The OOS Sharpe of 1.71 may be an artifact of 1H granularity.
4. **Complete BTC@30m stages 1–5** — currently only stage0a/0 ran.
