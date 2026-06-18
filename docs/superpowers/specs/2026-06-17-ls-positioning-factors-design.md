# L/S Positioning Factor Family — Wiring Design

**Date:** 2026-06-17
**Status:** Draft for review (Gemini-reviewed v2)
**Depends on:** `488c54f` (Binance OI/L-S daily-metrics ETL → `research/data/oi/oi_<SYM>USDT_1H.parquet`, >99.9% OI coverage)
**Memory:** [[project_ls_positioning_recon]], [[project_oi_data_source_binance_archive]]

## Goal

Wire the long/short **positioning** factor family into the research pipeline so
stage-0a/stage-1 can discover it per symbol. The orthogonality screen
(`scripts/oi_orthogonality.py` + an independent cross-check) confirmed these are
genuine alpha **orthogonal to `funding_z`** (retained ~100% incremental IC),
refuting the "funding clone" hypothesis. They are **ensemble-tier** (|IC|
0.03–0.10, peak at 72–168h) — ensemble members / gates alongside funding +
stablecoin, **not** standalone strategies.

## Factors

Computed from the cached Binance OI/L-S parquet columns (already 1H, NaN-not-ffilled):

| factor | source column | transform | screen IC (best, signed) |
|---|---|---|---|
| `global_ls_acct_z` | `global_ls_accounts` | 30d rolling z | BTC −0.048@72h · SOL −0.051@168h · ETH ~0 (dead) |
| `toptrader_ls_z` | `toptrader_ls_positions` | 30d rolling z | ETH −0.080@168h · SOL +0.060@168h |
| `ls_divergence` | `global_ls_acct_z − toptrader_ls_z` | difference of the two z's | BTC −0.048@72h · ETH +0.070@168h · SOL −0.090@168h |

**Dropped:** `taker_buysell_z` — dead on all 3 symbols (order-flow-adjacent, as predicted).

**Factor independence (Gemini):** `ls_divergence` is a linear combination of the
other two → the trio is **rank-2**. This is fine for stage-1, which evaluates
**univariate** IC per factor and per symbol (the strongest single factor differs
by symbol: `global` on BTC, `toptrader` on ETH, `divergence` on SOL — so all
three must be discoverable). It is **NOT** fine to linearly combine all three in
a downstream ensemble — that double-counts. **Constraint for the (separate)
strategy spec:** any ensemble uses a non-collinear subset (≤2 of the 3, or
`divergence` as a substitute for the `global`+`toptrader` pair, never all three).

Sign and per-symbol selection are handled downstream: stage-0/1 pick candidates
by per-symbol IC; the IC sign sets signal direction. No special-casing needed.

## Wiring

Mirror the existing `oi_factors` / `orderflow_df` integration.

1. **Symbol mapping (do first).** Add the Binance USDT-perp ticker to the symbol
   config rather than inlining `f"{sym}USDT"` at the load site — a centralized
   map prevents divergence across loaders. Either a `binance_usdt` field on
   `SymbolConfig` (preferred) or a single helper in `lib/`; one source of truth.

2. **`research/lib/derived_factors.py`** — add:
   ```python
   def positioning_factors(oi: pd.DataFrame) -> dict[str, pd.Series]:
       # global_ls_acct_z, toptrader_ls_z via _rolling_z(·, SCREEN_ZSCORE_DAYS*24);
       # ls_divergence = global_ls_acct_z - toptrader_ls_z. Guard each column.
   ```
   `_rolling_z` already uses `min_periods = window//2` (= 360 of 720h) → stable
   z, NaN-propagating at series start; identical hygiene to `funding_z`.

3. **`research/pipeline/stage0a_features.py`**
   - Load the OI/L-S parquet **graceful-absent** exactly like `orderflow_df`:
     missing parquet ⇒ factors simply absent ⇒ **a 1H run without the parquet is
     byte-for-byte unchanged** (zero regression). Use the symbol map from (1).
   - `build_feature_dict`: `features.update(positioning_factors(oi_parquet_df))`.
   - `_FACTOR_SOURCE` map: add the 3 names → `"positioning"`.
   - No IC-eval transform entry (already z-scored / stationary / 1H-native).

**NaN-gap note (Gemini).** Gaps stay NaN (never ffilled); IC uses pairwise drop.
Survivorship/IC-inflation risk is bounded because archive OI coverage is >99.9%
(488c54f) — gaps are <0.1% of hours, not systematic stress-period blackouts.

## Testing

- `test_derived_factors.py`: `positioning_factors` emits the 3 expected columns;
  z-score + `divergence = global − toptrader` math; missing-column guard returns
  only present factors.
- `test_stage0a_features.py`: with a synthetic OI/L-S parquet, `build_feature_dict`
  emits the 3 positioning factors; **without** it, the feature dict is unchanged
  (1H regression guard).
- Symbol-map unit test (btc→BTCUSDT etc.).
- Research suite only (`research/tests`), run separately from dashboard/agent.

## Acceptance (quantified — Gemini)

- `RESEARCH_ONLY_SYMBOL=<s> python -m research.pipeline.stage0a_features` then
  `…stage1_factors` reproduces, per symbol, the screen's **known-strong** factor
  with the **screened sign** and **|IC| ≥ 0.03** at 72h or 168h:
  BTC `global_ls_acct_z` (−), ETH `toptrader_ls_z` (−), SOL `ls_divergence` (−).
- A 1H run with **no** OI/L-S parquet present is **byte-for-byte unchanged**.
- Research tests green.

## Non-goals (explicit)

- **OI-factor revival is OUT of scope (Gemini: avoid conflating two factor
  classes' regressions).** Switching `oi_factors()` from the dead Bybit 7-day
  OI to the archive `oi` column is a **separate fast-follow change** — the
  parquet-load plumbing landed here makes it a small diff. Not in this change.
- No strategy build. Ensemble/gate/archetype/symbol choice is decided **after**
  stage-1 confirms candidates — a separate spec (must honor the rank-2 constraint).
- No dashboard/trader changes.
- No intraday: positioning is a 1H+ signal (peak 72–168h); the OHLCV intraday
  class is dead ([[project_intraday_ohlcv_class_dead]]).
