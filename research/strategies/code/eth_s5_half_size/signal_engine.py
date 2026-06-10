# manual: do-not-overwrite
"""
eth_s5_half_size signal_engine
───────────────────────────────
Iteration #4. Same entry/exit logic as eth_s4_regime_filtered but outputs
±0.5 signal magnitude instead of ±1.0 — equivalent to halving position
size (engine computes ``target_notional = |signal| × equity × leverage``).
Live-exchange-friendly substitute for "leverage 0.5x" when min leverage = 1x.

Expected: per-bar P&L halves → DD halves (s4 OOS -19% → s5 OOS ~-10%),
annual return halves (20% → ~10%), but **sharpe unchanged** (risk-adjusted
ratio is scale-invariant). Target: pass stage 5 DD gate (10%).

Inherits regime overlay from s4 (bear no-long / bull no-short):

  - 不在 bear regime 做多
  - 不在 bull regime 做空
  - neutral regime: 兩邊都允許

Regime labels come from research/manifests/regime_eth.json (daily bull/bear/
neutral, computed by stage 2.5). Daily labels are reindexed to hourly bars
with forward-fill so each backtest bar inherits the daily regime.

Parameters are hard-coded to s3 sweep_092 best:
  lookback_days=90, entry_high=70, entry_low=15, hold=168h, sl=3%, tp=8.5%,
  signal_invalidation between 40-60 on both factors.

This file has the `# manual: do-not-overwrite` marker on line 1 so stage 2b
will skip it. To re-enable yaml-driven compilation, delete this file and
remove the marker from the yaml-compiled version.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


def _ensure_research_on_syspath() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    research_dir = repo_root / "research"
    sp = str(research_dir)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _load_regime_series(symbol_short: str, target_index: pd.DatetimeIndex) -> pd.Series:
    """Load daily regime labels from regime_<sym>.json, reindex to target.

    Returns a Series of strings ('bull'/'bear'/'neutral'), aligned to
    ``target_index`` via daily forward-fill. Empty / unreadable JSON falls
    back to all-'neutral' so the strategy degrades to s3 behaviour (no
    masking).
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    regime_path = repo_root / "research" / "manifests" / f"regime_{symbol_short}.json"
    if not regime_path.exists():
        return pd.Series("neutral", index=target_index)

    try:
        payload = json.loads(regime_path.read_text(encoding="utf-8"))
    except Exception:
        return pd.Series("neutral", index=target_index)

    breakdown = payload.get("breakdown") or []
    if not breakdown:
        return pd.Series("neutral", index=target_index)

    df = pd.DataFrame(breakdown)
    df["ts"] = pd.to_datetime(df["date"])
    df = df.set_index("ts").sort_index()
    # Strip tz from target if present (ohlcv index is naive).
    daily = df["regime"].astype(str)
    # Reindex to hourly via ffill: each hour inherits the day's regime.
    out = daily.reindex(target_index.normalize(), method="ffill")
    out.index = target_index
    return out.fillna("neutral")


class SignalEngine:
    SYMBOL = "ETH-USDT-SWAP"
    SYMBOL_SHORT = "eth"

    # Hard-coded best params from eth_s3_dynamic_exit_sweep_092.
    LOOKBACK_DAYS = 90
    ENTRY_HIGH_PCT = 70.0
    ENTRY_LOW_PCT = 15.0
    HOLD_MAX_HOURS = 168
    SL_PCT = 3.0
    TP_PCT = 8.5
    PERSIST_K = 3
    PERSIST_M = 2

    # The s5 contribution: ~half size. Signal output magnitude. Set to 0.45
    # (not flat 0.5) to leave a buffer below the stage 5 DD<=10% gate (s5
    # at 0.5 hit OOS DD=10.1% — exactly at the boundary).
    SIZE_MULT = 0.45

    def generate(self, data_map: dict) -> dict:
        _ensure_research_on_syspath()
        from lib.factor_io import load_factor_values

        symbol = self.SYMBOL
        ohlcv = data_map[symbol]

        # ── factor values ──
        _factors = load_factor_values(symbol)
        if _factors.index.tz is not None:
            _factors.index = _factors.index.tz_localize(None)

        stablecoin_supply_z = _factors["stablecoin_supply_z"]
        stablecoin_supply_z = stablecoin_supply_z.reindex(ohlcv.index, method="ffill")
        stablecoin_supply_z = stablecoin_supply_z.rolling(3, min_periods=1).mean()

        funding_z = _factors["funding_z"]
        funding_z = funding_z.reindex(ohlcv.index, method="ffill")
        funding_z = funding_z.rolling(3, min_periods=1).mean()

        # ── percentile transforms (lookback_days * 24 bars/day) ──
        win = self.LOOKBACK_DAYS * 24
        half = max(1, win // 2)
        sc_pct = stablecoin_supply_z.rolling(win, min_periods=half).rank(pct=True) * 100
        fd_pct = funding_z.rolling(win, min_periods=half).rank(pct=True) * 100

        # ── entry signals (with persistence k/K) ──
        cond_long_sc = (sc_pct >= self.ENTRY_HIGH_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M
        cond_long_fd = (fd_pct <= self.ENTRY_LOW_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M
        entry_long = cond_long_sc & cond_long_fd

        cond_short_sc = (sc_pct <= self.ENTRY_LOW_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M
        cond_short_fd = (fd_pct >= self.ENTRY_HIGH_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M
        entry_short = cond_short_sc & cond_short_fd

        # ── REGIME OVERLAY (the s4 contribution) ──
        regime = _load_regime_series(self.SYMBOL_SHORT, ohlcv.index)
        regime_allows_long = regime != "bear"     # bull or neutral
        regime_allows_short = regime != "bull"    # bear or neutral

        entry_long = entry_long & regime_allows_long
        entry_short = entry_short & regime_allows_short

        # ── exit state machine (same as s3) ──
        signal = pd.Series(0.0, index=ohlcv.index)
        position = 0
        entry_price = None
        bars_held = 0

        for bar_i in range(len(ohlcv.index)):
            if position == 0:
                if entry_long.iloc[bar_i]:
                    position = 1
                    entry_price = ohlcv["close"].iloc[bar_i]
                    bars_held = 0
                elif entry_short.iloc[bar_i]:
                    position = -1
                    entry_price = ohlcv["close"].iloc[bar_i]
                    bars_held = 0
            else:
                bars_held += 1
                close_price = ohlcv["close"].iloc[bar_i]
                pnl_pct = (close_price - entry_price) / entry_price * position
                exit_flag = False

                # Time stop
                if bars_held >= self.HOLD_MAX_HOURS:
                    exit_flag = True
                # Take profit
                if pnl_pct >= self.TP_PCT / 100.0:
                    exit_flag = True
                # Stop loss (backstop)
                if pnl_pct <= -self.SL_PCT / 100.0:
                    exit_flag = True
                # Signal invalidation: factor returns to neutral percentile band
                if 40 <= sc_pct.iloc[bar_i] <= 60:
                    exit_flag = True
                if 40 <= fd_pct.iloc[bar_i] <= 60:
                    exit_flag = True

                if exit_flag:
                    position = 0
                    entry_price = None
                    bars_held = 0

            signal.iloc[bar_i] = float(position) * self.SIZE_MULT

        return {symbol: signal}
