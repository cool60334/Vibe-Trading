"""Alert-only MVP — live BTC perp ensemble short signal.

What it does:
1. Pull latest funding (Binance perp), F&G (alternative.me), BTC daily + 1H OHLCV
2. Compute regime (v1c) + ensemble (S2+S3+S4 short-only) + 48/2 persistence + gate
3. Write hourly signal log to live/state/signals.parquet
4. Catch up missing hours since last log entry
5. Print latest signal + diff vs previous to console; optional webhook
6. NO orders placed. Validation / dry-run only.

Run hourly via Windows Task Scheduler or cron:
    python C:/Users/cool6/Vibe-Trading/live/alert_only.py

Stateless invocation — each run reads log, computes fresh, appends new rows. Safe
to reschedule, safe to crash, safe to re-run.

Usage examples:
    python live/alert_only.py                     # normal hourly run
    python live/alert_only.py --lookback-days 180 # force longer fetch (one-off)
    python live/alert_only.py --no-alert          # silent, log only
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

from lib.ccxt_data import (  # noqa: E402
    fetch_funding_rate_history_ccxt,
    fetch_ohlcv_ccxt,
)
from lib.regime import compute_regime  # noqa: E402
from lib.sentiment import fetch_fear_greed  # noqa: E402

STATE_DIR = ROOT / "live" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = STATE_DIR / "signals.parquet"

# ---------------- params (match Python production ensemble v2 + short-only + gated) ----------------
EMA_WINDOW = 100
SLOPE_WINDOW = 20
PERSIST_DAYS = 20
PERSIST_THR = 0.55
FUND_MANIA = 3e-4

# sub-strategy
S2_PCT_WIN = 90 * 24
S2_MIN = 7 * 24
S2_FH = 0.80
S2_FL = 0.20
S2_FNG_VETO_HI = 0.85
S2_FNG_VETO_LO = 0.15

S3_EMA_FAST = 20
S3_EMA_SLOW = 50
S3_PCT_WIN = 90 * 24
S3_MIN = 7 * 24
S3_GATE_LONG = 0.60
S3_GATE_SHORT = 0.40

S4_PCT_WIN = 120 * 24
S4_MIN = 14 * 24
S4_FH = 0.85
S4_FL = 0.15
S4_FNG_HI = 0.80
S4_FNG_LO = 0.20

INTERNAL_WIN = 24
INTERNAL_HITS = 2
OUTER_PERSIST_WIN = 48
OUTER_PERSIST_HITS = 2
POSITION_SIZE = 0.5


# ---------------- data fetch ----------------
def fetch_live_data(lookback_days: int = 130) -> dict:
    """Pull all required series ending at most recent complete data."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)

    print(f"[fetch] funding (Binance) {since.date()} → now")
    fund = fetch_funding_rate_history_ccxt(
        "binance", "BTC/USDT:USDT", since_dt=since, until_dt=now,
    )
    fund.index = fund.index.tz_convert(None)
    print(f"  {len(fund)} rows, last {fund.index.max()}")

    print(f"[fetch] BTC 1H OHLCV (Binance spot)")
    ohlcv = fetch_ohlcv_ccxt("binance", "BTC/USDT", days=lookback_days, timeframe="1h")
    ohlcv.index = ohlcv.index.tz_convert(None) if ohlcv.index.tz is not None else ohlcv.index
    print(f"  {len(ohlcv)} rows, last {ohlcv.index.max()}")

    print(f"[fetch] BTC daily OHLCV (for regime detector)")
    daily = fetch_ohlcv_ccxt("binance", "BTC/USDT", days=max(lookback_days, 300), timeframe="1d")
    daily.index = daily.index.tz_convert(None) if daily.index.tz is not None else daily.index
    print(f"  {len(daily)} daily bars, last {daily.index.max()}")

    print(f"[fetch] F&G (alternative.me)")
    fng = fetch_fear_greed(days=max(lookback_days, 200))
    fng.index = fng.index.tz_convert(None) if fng.index.tz is not None else fng.index
    fng = fng[["fng"]]
    print(f"  {len(fng)} rows, last {fng.index.max()}")

    return {"funding": fund, "ohlcv": ohlcv, "daily": daily, "fng": fng}


# ---------------- signal helpers (mirror runs/_templates/signal_engine_short_only.py) ----------------
def _pct(series: pd.Series, win: int, minp: int, q: float) -> pd.Series:
    return series.rolling(win, min_periods=minp).quantile(q)


def _persist(raw: pd.Series, win: int, hits: int) -> pd.Series:
    short_h = (raw == -1.0).rolling(win, min_periods=1).sum()
    long_h = (raw == 1.0).rolling(win, min_periods=1).sum()
    out = pd.Series(0.0, index=raw.index)
    out[short_h.values >= hits] = -1.0
    out[long_h.values >= hits] = 1.0
    return out


def _signal_s2(funding: pd.Series, fng: pd.Series) -> pd.Series:
    f_hi = _pct(funding, S2_PCT_WIN, S2_MIN, S2_FH)
    f_lo = _pct(funding, S2_PCT_WIN, S2_MIN, S2_FL)
    fng_hi = _pct(fng, S2_PCT_WIN, S2_MIN, S2_FNG_VETO_HI)
    fng_lo = _pct(fng, S2_PCT_WIN, S2_MIN, S2_FNG_VETO_LO)
    short_now = (funding >= f_hi) & (fng < fng_lo).pipe(lambda s: ~s)
    long_now = (funding <= f_lo) & (fng > fng_hi).pipe(lambda s: ~s)
    raw = pd.Series(0.0, index=funding.index)
    raw[short_now] = -1.0
    raw[long_now] = 1.0
    return _persist(raw, INTERNAL_WIN, INTERNAL_HITS)


def _signal_s3(close: pd.Series, funding: pd.Series) -> pd.Series:
    ema_f = close.ewm(span=S3_EMA_FAST, adjust=False).mean()
    ema_s = close.ewm(span=S3_EMA_SLOW, adjust=False).mean()
    uptrend = ema_f > ema_s
    downtrend = ema_f < ema_s
    f_gate_long = _pct(funding, S3_PCT_WIN, S3_MIN, S3_GATE_LONG)
    f_gate_short = _pct(funding, S3_PCT_WIN, S3_MIN, S3_GATE_SHORT)
    long_now = uptrend & (funding <= f_gate_long)
    short_now = downtrend & (funding >= f_gate_short)
    raw = pd.Series(0.0, index=funding.index)
    raw[short_now] = -1.0
    raw[long_now] = 1.0
    return _persist(raw, INTERNAL_WIN, INTERNAL_HITS)


def _signal_s4(funding: pd.Series, fng: pd.Series) -> pd.Series:
    f_hi = _pct(funding, S4_PCT_WIN, S4_MIN, S4_FH)
    f_lo = _pct(funding, S4_PCT_WIN, S4_MIN, S4_FL)
    fng_hi = _pct(fng, S4_PCT_WIN, S4_MIN, S4_FNG_HI)
    fng_lo = _pct(fng, S4_PCT_WIN, S4_MIN, S4_FNG_LO)
    short_now = (funding >= f_hi) & (fng >= fng_hi)
    long_now = (funding <= f_lo) & (fng <= fng_lo)
    raw = pd.Series(0.0, index=funding.index)
    raw[short_now] = -1.0
    raw[long_now] = 1.0
    return _persist(raw, INTERNAL_WIN, INTERNAL_HITS)


# ---------------- compute full hourly signal series ----------------
def compute_signals(data: dict) -> pd.DataFrame:
    ohlcv = data["ohlcv"]
    funding_raw = data["funding"]["funding_rate"]
    fng_raw = data["fng"]["fng"]
    daily = data["daily"]["close"]

    # Regime on daily series → forward-fill to hourly
    regime_df = compute_regime(
        daily,
        funding_rate=funding_raw,
        ema_window=EMA_WINDOW,
        slope_window=SLOPE_WINDOW,
        funding_window_hours=30 * 24,
        funding_mania_threshold=FUND_MANIA,
        bear_persistence_days=PERSIST_DAYS,
        bear_persistence_threshold=PERSIST_THR,
    )

    idx = ohlcv.index
    funding = funding_raw.reindex(idx, method="ffill").bfill()
    fng = fng_raw.reindex(idx, method="ffill").bfill()
    regime = regime_df["regime"].reindex(idx, method="ffill").bfill()
    close = ohlcv["close"]

    s2 = _signal_s2(funding, fng)
    s3 = _signal_s3(close, funding)
    s4 = _signal_s4(funding, fng)

    ensemble = (s2 + s3 + s4) / 3.0
    ensemble_short = ensemble.clip(upper=0.0)

    # outer 48/2 persistence on short side
    is_short = ensemble_short < 0
    outer_hits = is_short.rolling(OUTER_PERSIST_WIN, min_periods=1).sum()
    confirmed = outer_hits >= OUTER_PERSIST_HITS

    raw_pos = ensemble_short.values * POSITION_SIZE
    raw_pos = np.where(confirmed.values, raw_pos, 0.0)

    bear_mask = (regime.values == "bear")
    gated = np.where(bear_mask, raw_pos, 0.0)

    df = pd.DataFrame({
        "close": close,
        "funding": funding,
        "fng": fng,
        "regime": regime,
        "s2": s2,
        "s3": s3,
        "s4": s4,
        "ensemble_raw": ensemble,
        "ensemble_short": ensemble_short,
        "outer_confirmed": confirmed,
        "position_pre_gate": raw_pos,
        "position": gated,
    }, index=idx)
    return df


# ---------------- log + alert ----------------
def load_log() -> pd.DataFrame:
    if LOG_PATH.exists():
        return pd.read_parquet(LOG_PATH)
    return pd.DataFrame(columns=["close", "funding", "fng", "regime", "s2", "s3", "s4",
                                  "ensemble_raw", "ensemble_short", "outer_confirmed",
                                  "position_pre_gate", "position"])


def append_log(new_rows: pd.DataFrame) -> None:
    log = load_log()
    combined = pd.concat([log, new_rows])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.to_parquet(LOG_PATH)


def alert(latest: pd.Series, prev_position: float | None, webhook: str | None) -> None:
    ts = latest.name
    pos = float(latest["position"])
    regime = latest["regime"]
    changed = prev_position is None or abs(pos - prev_position) > 1e-9

    arrow = ""
    if prev_position is not None:
        if pos < prev_position:
            arrow = f" (was {prev_position:+.3f}, INCREASED short)"
        elif pos > prev_position:
            arrow = f" (was {prev_position:+.3f}, REDUCED short)"

    tag = "[ALERT]" if changed else "[hold]"
    print(f"\n{tag} {ts} regime={regime}  position={pos:+.3f}{arrow}")
    print(f"  funding={float(latest['funding']):+.6f}  fng={float(latest['fng']):.0f}  close=${float(latest['close']):,.0f}")
    print(f"  s2={float(latest['s2']):+.1f}  s3={float(latest['s3']):+.1f}  s4={float(latest['s4']):+.1f}  ensemble={float(latest['ensemble_short']):+.3f}")

    if webhook and changed:
        payload = {
            "timestamp": ts.isoformat(),
            "regime": str(regime),
            "position": pos,
            "prev_position": prev_position,
            "close": float(latest["close"]),
            "funding": float(latest["funding"]),
            "fng": float(latest["fng"]),
            "s2": float(latest["s2"]),
            "s3": float(latest["s3"]),
            "s4": float(latest["s4"]),
        }
        try:
            req = urllib.request.Request(
                webhook,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
            print(f"  webhook posted → {webhook}")
        except Exception as e:
            print(f"  webhook FAILED: {e}")


# ---------------- main ----------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=130)
    parser.add_argument("--no-alert", action="store_true")
    parser.add_argument("--webhook", default=None, help="Optional POST URL for alerts on change")
    args = parser.parse_args()

    log_before = load_log()
    last_logged = None if log_before.empty else log_before.index.max()
    prev_position = None if log_before.empty else float(log_before["position"].iloc[-1])
    print(f"[state] log rows={len(log_before)}  last={last_logged}  prev_position={prev_position}")

    data = fetch_live_data(args.lookback_days)
    sig = compute_signals(data)

    # Append all rows past last_logged
    if last_logged is None:
        new_rows = sig
    else:
        new_rows = sig.loc[sig.index > last_logged]

    if not new_rows.empty:
        append_log(new_rows)
        print(f"[log] appended {len(new_rows)} new rows  range {new_rows.index.min()} → {new_rows.index.max()}")
    else:
        print(f"[log] no new rows (live data ends at {sig.index.max()}, same hour already logged)")

    latest = sig.iloc[-1]
    if not args.no_alert:
        alert(latest, prev_position, args.webhook)


if __name__ == "__main__":
    main()
