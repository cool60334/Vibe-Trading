# Order-Flow Maker-Aware Bookend Backtest — Design Spec

- **Date**: 2026-06-16
- **Status**: Approved (brainstorming), pending implementation plan
- **Scope**: Task 4c of the order-flow economic validation. A **bookend** net-of-fee backtest (taker lower bound / optimistic-maker upper bound) to decide whether an ETH 15m order-flow strategy is tradeable after fees — WITHOUT first building a high-fidelity limit-order fill model.
- **Branch**: quant-trading-dashboard
- **Predecessors**: `docs/superpowers/specs|plans/2026-06-16-intraday-orderflow-poc*` (factors + cache), `docs/superpowers/plans/2026-06-16-orderflow-economic-validation.md` (decay/quantile/incremental — all gates passed).

## 1. Motivation

Economic validation (Tasks 1–3) passed all three gates with clean data:
- **Decay**: `price_impact` / `trade_count_imbalance` peak ~−0.05 IC at 15m-forward (above the 0.03 gate), half-life ~4–5 bars (~1h). The pipeline's hour-based evidence horizons missed this 15m-forward peak.
- **Quantile**: `trade_count_imbalance` near-monotone deciles; `large_trade_ratio` top-1% +3 bps nonlinear tail (orthogonal).
- **Incremental**: order-flow factors fully orthogonal to `funding_z`.

But the per-trade edge is thin (~1–3 bps) and the maker round-trip cost is ~4 bps (2 × 0.02% maker), so the strategy is **only plausibly tradeable as a maker** (limit entries earning/saving fees), and even then it is borderline. The half-life (~1h) is the reason maker is plausible at all — a limit order has time to fill before the edge decays.

This spec defines the cheapest decisive test of "does it survive fees": a two-regime bookend backtest, not a full fill model.

### Decisions (brainstorming, validated by 3 gemini reviews)

| Decision | Choice |
|---|---|
| Fidelity | **Bookends first** (taker lower / optimistic-maker upper); build a realistic fill model only if the result lands in the ambiguous middle |
| Fill model | None yet — bookends bracket the truth |
| Strategy source | **Hand-curated yaml** (not stage0/2 auto-discovery — keeps alpha interpretable, avoids overfit) |
| Execution knob | A single boolean `entry_is_maker` on the engine (no `ExecutionModel` abstraction — YAGNI for one strategy class) |
| Maker-rate exploration | **Analytic breakeven** (the maker rate at which net profit = 0), not a discrete sweep |
| Fill realism probe | **Breakeven fill-rate** back-out in the reporter (at what fill ratio does the edge die) |

## 2. The two bookend regimes + decision rule

**Lower bound — Taker (pessimistic)**: current engine behavior — opens pay taker (0.055%) + slippage (0.05%), closes pay maker (0.02%). Represents "signal so fast we must cross the spread."

**Upper bound — Optimistic Maker**: opens also pay maker, **no slippage, 100% fill, no adverse selection**. Represents perfect limit-order execution. This is a **veto / necessary-not-sufficient** bound: passing it only means "worth a realistic fill model next," not "tradeable" — because 100% fill ignores adverse selection (see §4).

**Decision rule** (gemini: `Sharpe>0` is dangerously loose for high-turnover intraday):

```
Taker lower bound:  net Sharpe > 1.5  AND  avg-net-profit / round-trip-cost > 1.0
                    → robustly alive → promote to paper
Optimistic maker:   breakeven maker_rate worse than achievable (net Sharpe ≤ 0 or
                    profit/cost < 1.0 even at a plausible rebate)
                    → robustly dead → stop; do NOT build a fill model
Middle (maker passes, taker fails)
                    → execution-dependent → build the fill-gated + adverse-selection
                      model in a follow-up round
```

Two metrics, both required for the "alive" verdict:
1. **net Sharpe > 1.5** (risk-adjusted, after all fees + funding).
2. **avg net profit per round trip / round-trip cost > 1.0** — if net profit < total fees, you are working for the exchange with zero margin of safety; intraday strategies in that regime do not survive live.

Both regimes run stage3's existing train + OOS + cost-stress multipliers.

## 3. Components

| # | Component | Change | Size |
|---|---|---|---|
| 1 | **Engine execution knob** — `agent/backtest/engines/crypto.py` | Add config `entry_is_maker: bool = False` (default = current behavior, zero regression). `calc_commission`: opens use `maker_rate` when `entry_is_maker`. `apply_slippage`: maker entries skip slippage. | small, backward-compatible |
| 2 | **stage3 exec-regime** — `research/pipeline/stage3_backtest.py` | `--exec-regime taker\|maker` flag threading `entry_is_maker` + the no-slippage behavior into the engine config; register runs via the existing stress-run writer. | small |
| 3 | **Component 5** — `research/lib/sources.py` | `binance_orderflow` SourceSpec (`available`) + `FactorCandidate.category` Literal gains `"orderflow"`. Needed only if the strategy is wired through factor metadata; confirm during planning whether the hand-curated path needs it. | small / maybe-skip |
| 4 | **OF strategy yaml** — `research/strategies/` | Hand-author a curated strategy (pattern: existing `eth_s5`): short-horizon **contrarian** on `price_impact` / `trade_count_imbalance`, entry on extreme factor z-score, exit via `signal_invalidation` or a fixed ~4-bar (~1h) hold. | medium |
| 5 | **Decision reporter** — `research/scripts/of_backtest_report.py` (or a diagnostic) | Read both regime runs → compute net Sharpe, avg-net-profit/round-trip-cost, **breakeven maker_rate** (analytic), **breakeven fill-rate** (back-out) → apply the §2 decision rule and print the verdict. | small |

### Key wiring decisions

- **Hand-curated strategy, not auto-discovery** — controllable, interpretable, avoids overfit (gemini-confirmed).
- **Critical prerequisite**: the order-flow factor values must be available to the backtest. stage0a computes them in the feature dict; the compiled strategy / stage3 reads `factor_values_*.parquet`. **The plan must verify order-flow features land in the parquet the backtest reads** (at the `15m` namespace) — this is the main wiring unknown.
- **Real funding required** — the engine's `on_bar` already applies 8h funding (00/08/16 UTC); stage3 @15m must feed the real funding series (not a fixed rate), and ~1h holds that cross a settlement must incur it. ETH funding is volatile enough that one crossed settlement (~1–3 bps) can wipe a trade's edge.

## 4. Lookahead bias — the #1 correctness gate

(gemini: the biggest risk is not parquet landing, it is lookahead.)

Order-flow factors aggregate the **whole bar's** trades (`[T, T+15m)`), so `factor[T]` is only known at the bar's **close** (`T+15m`). The backtest must therefore:

- **Never** enter at bar T using `factor[T]` at a price earlier than T's close. Entry fills no earlier than `T+15m` (i.e., act on `factor[T]` at the next bar's open / T-close price).
- Ensure the **factor timestamp strictly leads execution** in the compiled signal path.

Verification obligations in the plan:
1. Confirm the engine/signal path does not execute same-bar on a bar-complete factor.
2. **Re-audit the IC analyses** (decay/incremental) for the same 1-bar lookahead: the IC base price must be the factor's known-time, not the bar-open. The POC reconciliation showed pipeline and `of_decay` agree at −0.027 (1h), but both may share the same alignment convention — confirm the candle close-timestamp convention so the −0.05 @15m figure is lookahead-free. If a 1-bar shift is needed, the headline IC may move.

## 5. Adverse selection, queue, fill — explicitly out of scope (this round)

The optimistic-maker bound deliberately ignores:
- **Adverse selection** — OF signals trigger during volatility; a limit fill there often means price is about to pierce the order (toxic flow). This makes 100% fill *optimistic beyond realism*, which is exactly why passing the upper bound is necessary-not-sufficient.
- **Queue position** — deep ETH book; price touching the limit ≠ a fill.

These are the substance of the **follow-up fill-gated round**, triggered only if the bookends land in the ambiguous middle. The reporter's breakeven fill-rate gives an early read on how sensitive the result is to non-fills.

## 6. Testing

| Test | Focus |
|---|---|
| Engine `entry_is_maker=False` unchanged | zero-regression: existing backtests identical |
| Engine `entry_is_maker=True` | opens charged `maker_rate`, no slippage on entry |
| Engine close still maker | closes unchanged |
| stage3 `--exec-regime` both values | taker run == legacy; maker run flips the knob |
| Lookahead guard | a unit test asserting a bar-complete factor cannot fill same-bar (entry price ≥ bar close time) |
| Reporter metrics | net Sharpe, profit/cost ratio, breakeven maker_rate (analytic), breakeven fill-rate on a fixture run |
| Real-funding crossing | a ~1h hold crossing 08:00 UTC incurs funding |

research and dashboard pytest run **separately** (existing convention).

## 7. Success criteria / exit

- **Alive**: taker lower bound passes `Sharpe>1.5 AND profit/cost>1.0` → promote to paper trading.
- **Dead**: optimistic maker fails even at a plausible best maker/rebate rate → stop; trades-only order-flow has no fee-survivable edge; do not buy L2.
- **Ambiguous** (maker passes, taker fails): build the realistic fill-gated + adverse-selection model (separate spec).

**Out of scope**: realistic limit-order fill simulation, adverse-selection modeling, queue position, L2 order-book data, multi-symbol, live trading wiring.

## 8. Change list

- Modify `agent/backtest/engines/crypto.py` (`entry_is_maker` knob).
- Modify `research/pipeline/stage3_backtest.py` (`--exec-regime` + run registration).
- Modify `research/lib/sources.py` + `FactorCandidate.category` (component 5 — confirm necessity).
- New `research/strategies/<eth_of_*>.yaml` (+ compiled signal engine, mirroring curated-strategy convention).
- New `research/scripts/of_backtest_report.py` (decision reporter).
- New tests per §6.
- Audit (no code unless a bug found): IC-analysis lookahead alignment (§4.2).
