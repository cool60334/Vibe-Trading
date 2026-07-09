# manual: do-not-overwrite
"""
sol_s1_single_factor_regime — paper-forward validation engine.

Frozen clone of the auto-built archetype (ls_divergence contrarian single-factor
+ regime overlay), hard-coded to the stage-4 TRAIN-best params. Runs at full size
(SIZE_MULT=1.0): the alpha verdict (sharpe) is size-invariant, so size is decoupled
and applied post-hoc as a risk lever (see forward_protocol.md). FACTOR_LAG_HOURS
simulates the live ~24h archive-availability lag for the Phase-0 gate.

`# manual: do-not-overwrite` (line 1) makes stage2b skip this file so the pipeline
never overwrites the frozen artifact.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd


def _ensure_research_on_syspath() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]          # runs/<run>/code/signal_engine.py → repo
    research_dir = repo_root / "research"
    if str(research_dir) not in sys.path:
        sys.path.insert(0, str(research_dir))


def _load_regime_series(symbol_short: str, target_index: pd.DatetimeIndex) -> pd.Series:
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
    # ffill_regime_to (NOT a bare reindex+ffill): the manifest's date for a
    # day is derived from that day's OWN end-of-day close
    # (resample("1D").last()), so it isn't knowable until ~23:59 that day. A
    # naive reindex+ffill would assign day-D's label starting at D 00:00 --
    # ~1 day of look-ahead into real entry decisions. See
    # research/lib/regime.py:ffill_regime_to for the full explanation.
    from lib.regime import ffill_regime_to
    out = ffill_regime_to(df["regime"].astype(str), target_index.normalize())
    out.index = target_index
    return out.fillna("neutral")


class SignalEngine:
    SYMBOL = "SOL-USDT-SWAP"
    SYMBOL_SHORT = "sol"

    # ── stage-4 TRAIN-best params (from optimization.json sweep_027) ──
    LOOKBACK_DAYS = 60      # ← best_params["lookback_days"]
    ENTRY_HIGH_PCT = 90.0   # ← best_params["entry_high_pct"]
    ENTRY_LOW_PCT = 15.0    # ← best_params["entry_low_pct"]
    HOLD_MAX_HOURS = 96     # ← best_params["hold_max_hours"]
    SL_PCT = 4.0            # ← best_params["sl_pct"]
    TP_PCT = 5.5            # ← best_params["tp_pct"]
    PERSIST_K = 5           # ← best_params["persistence_last_n"]
    PERSIST_M = 2           # ← best_params["persistence_min_hits"]

    # Full size for the alpha verdict; size is a post-hoc risk lever, not tuned here.
    SIZE_MULT = 1.0

    # FACTOR_LAG_HOURS is read from env at generate() time (not class level) to satisfy
    # the runner's AST validator which only permits literal class-level assignments.
    # SOL_FACTOR_LAG_HOURS=24 simulates the live archive-availability lag (Phase-0 gate).

    def generate(self, data_map: dict) -> dict:
        _ensure_research_on_syspath()
        from lib.factor_io import load_factor_values

        # Read lag at call time so the subprocess env var is honoured correctly.
        factor_lag_hours = int(os.environ.get("SOL_FACTOR_LAG_HOURS", "0"))

        symbol = self.SYMBOL
        ohlcv = data_map[symbol]

        factors = load_factor_values(symbol)
        if factors.index.tz is not None:
            factors.index = factors.index.tz_localize(None)

        ls = factors["ls_divergence"].reindex(ohlcv.index, method="ffill")
        if factor_lag_hours:
            ls = ls.shift(factor_lag_hours)   # delay info to live-availability
        ls = ls.rolling(3, min_periods=1).mean()

        win = self.LOOKBACK_DAYS * 24
        half = max(1, win // 2)
        pct = ls.rolling(win, min_periods=half).rank(pct=True) * 100

        # Contrarian (negative IC): long at LOW percentile, short at HIGH.
        cond_long = (pct <= self.ENTRY_LOW_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M
        cond_short = (pct >= self.ENTRY_HIGH_PCT).rolling(self.PERSIST_K).sum() >= self.PERSIST_M

        regime = _load_regime_series(self.SYMBOL_SHORT, ohlcv.index)
        cond_long = cond_long & (regime != "bear")     # bull/neutral
        cond_short = cond_short & (regime != "bull")    # bear/neutral

        signal = pd.Series(0.0, index=ohlcv.index)
        position = 0
        entry_price = None
        bars_held = 0
        for i in range(len(ohlcv.index)):
            if position == 0:
                if bool(cond_long.iloc[i]):
                    position, entry_price, bars_held = 1, ohlcv["close"].iloc[i], 0
                elif bool(cond_short.iloc[i]):
                    position, entry_price, bars_held = -1, ohlcv["close"].iloc[i], 0
            else:
                bars_held += 1
                pnl = (ohlcv["close"].iloc[i] - entry_price) / entry_price * position
                exit_flag = (
                    bars_held >= self.HOLD_MAX_HOURS
                    or pnl >= self.TP_PCT / 100.0
                    or pnl <= -self.SL_PCT / 100.0
                    or 40 <= pct.iloc[i] <= 60           # signal-invalidation neutral band
                )
                if exit_flag:
                    position, entry_price, bars_held = 0, None, 0
            signal.iloc[i] = float(position) * self.SIZE_MULT
        return {symbol: signal}
