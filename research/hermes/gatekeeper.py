# research/hermes/gatekeeper.py
"""Talos Factor Foundry statistical gatekeeper (Phase 1A, agy-v2).

Deterministic pass/fail on a sandbox-computed factor. Signal quality = Gross IC
(spearman vs raw h-horizon return); cost gating = Net IR/Sharpe from a correctly
aligned 1-period rebalanced net-return stream (NOT overlapping h-period returns).
Turnover from position weights with an absolute ceiling; entry lag enforced
INSIDE evaluate (never trust the caller); regime labels ffill'd from daily;
DSR trial distribution restricted to homogeneous same-interval ledger trials.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from research.lib.deflated_sharpe import deflated_sharpe, bars_per_year
from research.lib.factor_metrics import add_forward_returns
from research.lib.research_ledger import read_events
from research.lib.timeframe import bars_per_hour


def factor_to_weights(factor: pd.Series, span: int = 168) -> pd.Series:
    """Map factor -> target weights in [-1,1] via causal EMA z-score.

    EMA (not SMA) softens window-start instability (agy #5). std==0 runs (a
    constant/discrete signal) are flat by definition — no variance to measure
    extremity against — so z is set to 0 (not NaN) for the duration of the run."""
    mu = factor.ewm(span=span, adjust=False, min_periods=span // 4).mean()
    sd = factor.ewm(span=span, adjust=False, min_periods=span // 4).std(bias=True)
    z = (factor - mu) / sd
    # agy-3 #5-1: replace ONLY the exactly-flat (sd==0) runs with 0.0; warmup
    # NaN (sd is NaN there) must stay NaN — .where(sd>0,0) wrongly zeroed warmup,
    # handing the strategy a premature 0 position + a fake turnover spike.
    z = z.mask(sd == 0, 0.0)
    return z.clip(-1.0, 1.0)


def turnover_of(weights: pd.Series) -> pd.Series:
    """Per-bar turnover = |w_t - w_{t-1}|. NaN weights (EMA warmup / flat gaps)
    are treated as flat (0.0) BEFORE diff so the first real entry from a NaN
    warmup is charged turnover instead of being a free position (agy-3 #1)."""
    return weights.fillna(0.0).diff().abs()


def gross_ic(factor: pd.Series, fwd_ret: pd.Series) -> float:
    """Spearman IC of the factor vs RAW forward return (drops NaN pairs)."""
    paired = pd.concat([factor, fwd_ret], axis=1).dropna()
    if len(paired) < 20:
        return float("nan")
    return float(spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]).statistic)


def net_ir(weights: pd.Series, ret_1period: pd.Series, cost_frac: float) -> float:
    """Per-bar IR/Sharpe of the net return of a 1-period rebalanced position.

    strat_ret_t = weights_t * ret1_{t+1} - turnover_t * cost_frac.
    ret_1period MUST be a TRAILING single-bar return (e.g. close.pct_change(),
    NOT pre-shifted forward) — this function shifts it forward internally.
    Passing an already-forward return double-shifts and misaligns by one bar.
    A single-bar series also avoids h-period overlap (agy #1c: overlap would
    autocorrelate and inflate Sharpe). Cost is paid when the position is set
    at t; the position earns the next bar's return."""
    fwd1 = ret_1period.shift(-1)                       # weights_t earn ret_{t+1}
    tau = turnover_of(weights)
    strat = (weights * fwd1) - tau.fillna(0.0) * cost_frac
    strat = strat.dropna()
    sd = strat.std(ddof=0)
    if len(strat) < 20 or sd <= 0:
        return float("nan")
    return float(strat.mean() / sd)


def nonoverlap_ic(factor: pd.Series, fwd_ret: pd.Series, horizon_bars: int) -> float:
    """IC on non-overlapping subsample (every horizon_bars-th row) so a long
    horizon's overlapping windows don't inflate significance (agy C-4).

    Threshold is 10, not gross_ic's 20: plan spec's own required test
    (n=300, horizon_bars=24) only survives striding with 13 rows, so 20 is
    infeasible here. 10 is the practical floor below which Spearman's
    significance cutoff is so high that |rho| near 1 is expected from pure
    chance rather than signal (agy consult, 2026-07-09).

    Strides BEFORE dropna, not after (post-implementation review fix,
    2026-07-09): factor/fwd_ret share a common regular calendar grid, so
    striding the raw frame by row position is striding by calendar time.
    Dropping NaN first would shrink the frame and shift row positions,
    so a later `.iloc[::horizon_bars]` no longer lands on true
    horizon_bars-apart timestamps whenever NaN is scattered mid-series
    (not just a contiguous EMA-warmup block) -- silently reintroducing the
    overlapping-window autocorrelation this function exists to avoid."""
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be >= 1")
    paired = pd.concat([factor, fwd_ret], axis=1).iloc[::horizon_bars].dropna()
    if len(paired) < 10:
        return float("nan")
    return float(spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]).statistic)


def regime_ic(factor: pd.Series, fwd_ret: pd.Series, daily_regime: pd.Series) -> dict:
    """IC within each regime. daily_regime is DAILY (compute_regime output); it is
    ffill'd onto the factor index so hourly factors keep all rows (agy #2)."""
    labels = daily_regime.reindex(factor.index, method="ffill")
    out: dict = {}
    for label in ("bull", "bear", "neutral"):
        mask = labels == label
        if mask.sum() >= 20:
            out[label] = gross_ic(factor[mask], fwd_ret[mask])
    return out


def yearly_ic(factor: pd.Series, fwd_ret: pd.Series) -> dict:
    out: dict = {}
    for year, idx in factor.groupby(factor.index.year).groups.items():
        if len(idx) >= 20:
            out[str(year)] = gross_ic(factor.loc[idx], fwd_ret.reindex(idx))
    return out


def nearest_correlate(factor: pd.Series, others: pd.DataFrame) -> tuple:
    """(column_name, max_abs_spearman) of the most-correlated existing/dead factor.
    O(N*K) via corrwith — NOT .corr() (agy-3 #4: .corr() builds the full
    (K+1)x(K+1) all-to-all matrix; at K=5000 that is ~25M pairs and an OOM bomb
    when we only need base-vs-each). Rank per column (NaN-preserving) then
    corrwith aligns pairwise. abs() catches an inverse factor (agy C-2). Returns
    (None, 0.0) when nothing to compare / nothing overlaps."""
    if others.shape[1] == 0:
        return (None, 0.0)
    ranked_factor = factor.rank()
    ranked_others = others.rank()                       # per-column, keeps NaN
    corr = ranked_others.corrwith(ranked_factor).dropna()   # O(N*K), pairwise
    if corr.empty:
        return (None, 0.0)
    abs_corr = corr.abs()
    top = abs_corr.idxmax()
    return (str(top), float(abs_corr.loc[top]))


def foundry_dsr(best_sr_per_bar: float, symbol: str, interval: str,
                manifests_dir, T: int) -> float:
    """Deflated Sharpe using the symbol's HISTORICAL trials at the SAME interval
    (agy #3/P2): count comes from the ledger (multiple-testing debt persists
    across nights), but the trial SR distribution is kept homogeneous — mixing
    different-T / different-interval trials breaks the DSR variance math."""
    trials = [
        e["detail"]["sr_per_bar"]
        for e in read_events(manifests_dir)
        if e.get("kind") == "factor_trial" and e.get("symbol") == symbol
        and isinstance(e.get("detail"), dict)
        and e["detail"].get("interval") == interval
        and "sr_per_bar" in e["detail"]
    ]
    # agy-3 #3: the current factor is NOT yet in the ledger; include it so the
    # trial population N and its variance are complete for the multiple-testing
    # correction (otherwise N is short by 1 and the current sample is missing).
    trials.append(best_sr_per_bar)
    return deflated_sharpe(best_sr_per_bar, trials, T=T)


@dataclass(frozen=True)
class GateConfig:
    interval: str                       # "1H" / "1D"
    horizon_h: int                      # forward-return horizon (hours)
    entry_lag: int = 1                  # bars to shift the factor before eval (C-7)
    cost_frac: float = 0.0006           # per-unit-turnover cost (taker+slippage)
    gross_ic_min: float = 0.03
    dsr_min: float = 0.5
    redundant_abs_spearman: float = 0.7
    max_turnover: float = 0.5           # mean per-bar turnover ceiling (untradeable above)


@dataclass(frozen=True)
class GatekeeperResult:
    passed: bool
    metrics: dict
    rejection_reason: str = ""


def evaluate(factor, ohlcv, daily_regime, existing_and_dead, symbol,
             manifests_dir, cfg: GateConfig) -> GatekeeperResult:
    factor = factor.shift(cfg.entry_lag)               # agy #6c: gate self-enforces lag
    ret_col = f"ret_{cfg.horizon_h}h"
    if cfg.interval == "1D":
        # Task 7 deviation (see self-review): research.lib.timeframe.bars_per_hour
        # only supports 15m/30m/1H by contract (SUPPORTED_INTERVALS is asserted
        # == {"15m","30m","1H"} in test_timeframe.py) — it cannot represent a 1D
        # candle's 1/24 bars-per-hour without breaking that contract, so
        # add_forward_returns(interval="1D") raises ValueError. 1D forward
        # returns/horizon-bars are computed directly here instead; the formula
        # matches add_forward_returns' internal one exactly for the 1H/sub-hour
        # path below.
        if cfg.horizon_h % 24 != 0:
            raise ValueError(f"horizon_h={cfg.horizon_h} must be a multiple of 24 for a 1D interval")
        horizon_bars = cfg.horizon_h // 24
        fwd = ohlcv["close"].shift(-horizon_bars) / ohlcv["close"] - 1
    else:
        fwd = add_forward_returns(ohlcv[["close"]], "close", [cfg.horizon_h],
                                  interval=cfg.interval)[ret_col]
        horizon_bars = cfg.horizon_h * bars_per_hour(cfg.interval)
    ret1 = ohlcv["close"].pct_change()
    weights = factor_to_weights(factor)
    mean_turnover = float(turnover_of(weights).fillna(0.0).mean())
    sr_bar = net_ir(weights, ret1, cfg.cost_frac)
    nearest, absrho = nearest_correlate(factor, existing_and_dead)

    metrics = {
        "gross_ic": gross_ic(factor, fwd),
        "ic_nonoverlap": nonoverlap_ic(  # agy-3 #5-2: horizon_h is HOURS -> bars
            factor, fwd, horizon_bars=max(1, horizon_bars)),
        "ir": sr_bar,
        "dsr": foundry_dsr(sr_bar if np.isfinite(sr_bar) else 0.0, symbol,
                           cfg.interval, manifests_dir, T=bars_per_year(cfg.interval)),
        "pbo": None,                    # reserved; CPCV-based PBO is a later task
        "turnover": mean_turnover,
        "n_samples": int(pd.concat([factor, fwd], axis=1).dropna().shape[0]),
        "regime_ic": regime_ic(factor, fwd, daily_regime),
        "yearly_ic": yearly_ic(factor, fwd),
        "nearest_factor": nearest,
        "nearest_abs_spearman": absrho,
    }
    gic = metrics["gross_ic"]
    if absrho >= cfg.redundant_abs_spearman:
        return GatekeeperResult(False, metrics, f"redundant: abs_spearman {absrho:.2f} vs {nearest}")
    if mean_turnover > cfg.max_turnover:
        return GatekeeperResult(False, metrics, f"turnover {mean_turnover:.2f} > {cfg.max_turnover}")
    if np.isnan(gic) or abs(gic) < cfg.gross_ic_min:
        return GatekeeperResult(False, metrics, f"weak gross_ic {gic:.4f} < {cfg.gross_ic_min}")
    if metrics["dsr"] < cfg.dsr_min:
        return GatekeeperResult(False, metrics, f"DSR {metrics['dsr']:.2f} < {cfg.dsr_min}")
    return GatekeeperResult(True, metrics, "")
