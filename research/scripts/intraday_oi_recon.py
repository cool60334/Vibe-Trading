"""Intraday OI/positioning factor recon — GO/NO-GO per coin at 15m/30m.

    python research/scripts/intraday_oi_recon.py --symbol sol --interval 30m

Loads the Binance OI archive (raw 5-min, per-file create_time normalized),
re-aggregates to the target interval AND to 1H, builds interval-correct factors,
and for each measures: decay IC across horizons, half-life, the decisive
incremental IC vs its own causal 1H value, and a realistic-entry-lag execution IC.
Writes research/manifests/<interval>/intraday_oi_recon_<sym>.json + prints a table.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPTS = Path(__file__).resolve().parent
_RESEARCH = _SCRIPTS.parent
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from lib import intraday_oi_factors as iof          # noqa: E402
from lib import intraday_oi_recon as verdict        # noqa: E402
from lib import oi_metrics, orderflow_eval          # noqa: E402
from lib.timeframe import bars_per_hour             # noqa: E402

_PANDAS_FREQ = {"15m": "15min", "30m": "30min", "1H": "1h"}
_HORIZONS_H = [0.5, 1, 2, 4, 8, 24, 48, 72]


def run_recon(oi_df: pd.DataFrame, close: pd.Series, interval: str, symbol: str) -> dict:
    """Pure core: given an interval OI frame + aligned close, evaluate every factor."""
    bph = bars_per_hour(interval)
    close = close.reindex(oi_df.index)
    factors_iv = iof.build_intraday_oi_factors(oi_df, close, interval)

    # 1H versions for the causal-1H control (resample OI to 1H, rebuild at 1H)
    oi_1h = oi_df.resample("1h", label="left", closed="left").last()
    close_1h = close.resample("1h", label="left", closed="left").last()
    factors_1h = iof.build_intraday_oi_factors(oi_1h, close_1h, "1H")

    max_bars = int(max(_HORIZONS_H) * bph)
    out: dict[str, dict] = {}
    for name, fac in factors_iv.items():
        decay = orderflow_eval.decay_profile(fac, close, max_bars=max_bars)
        decay_h = {h: decay.get(int(h * bph)) for h in _HORIZONS_H if int(h * bph) >= 1}
        valid = {h: v for h, v in decay_h.items() if v is not None and v == v}
        peak_h = max(valid, key=lambda h: abs(valid[h])) if valid else None
        fac = fac.replace([np.inf, -np.inf], np.nan)
        ctrl = iof.causal_1h_control(factors_1h.get(name, pd.Series(dtype=float)), oi_df.index)
        ctrl = ctrl.replace([np.inf, -np.inf], np.nan)
        try:
            incr = orderflow_eval.incremental_ic(fac, ctrl, close, fwd_bars=max(1, int(2 * bph)))
        except np.linalg.LinAlgError:
            incr = float("nan")
        exec0 = orderflow_eval.execution_ic(fac, close, entry_lag_bars=0, hold_bars=max(1, int(2 * bph)))
        exec1 = orderflow_eval.execution_ic(fac, close, entry_lag_bars=1, hold_bars=max(1, int(2 * bph)))
        v, reasons = verdict.classify_factor(decay_h, incr, peak_h)
        out[name] = {
            "deployable": name in iof.DEPLOYABLE_FACTORS,
            "max_abs_ic": max((abs(v2) for v2 in valid.values()), default=0.0),
            "peak_h": peak_h,
            "ic_by_h": valid,
            "half_life_bars": orderflow_eval.half_life_bars(decay),
            "incr_vs_1h": incr,
            "exec_ic_lag0": exec0,
            "exec_ic_lag1": exec1,
            "verdict": v,
            "reasons": reasons,
        }
    return {"symbol": symbol, "interval": interval, "factors": out}


def _load_interval_oi(symbol_binance: str, interval: str, cache_dir: Path,
                      start: date, end: date) -> pd.DataFrame:
    """Load raw 5-min metrics day-by-day, normalize each file's create_time,
    then aggregate to the target interval."""
    frames = []
    day = start
    from datetime import timedelta
    while day <= end:
        zp = oi_metrics.binance_dump.download_metrics_day(symbol_binance, day, cache_dir, verify=False)
        if zp is not None:
            frames.append(oi_metrics.normalize_create_time(oi_metrics.read_metrics_zip(zp)))
        day += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    raw = pd.concat(frames).sort_index()
    raw = raw[~raw.index.duplicated(keep="last")]
    return oi_metrics.aggregate_to_interval(raw, _PANDAS_FREQ[interval])


def _format_table(report: dict) -> str:
    lines = [f"{report['symbol']} @ {report['interval']}",
             f"{'factor':22s} {'dep':4s} {'maxIC':>7s} {'peak_h':>7s} {'incr1H':>7s} {'lag0':>6s} {'lag1':>6s} verdict"]
    for name, r in report["factors"].items():
        def f(x): return f"{x:.3f}" if isinstance(x, float) and x == x else "—"
        lines.append(f"{name:22s} {'Y' if r['deployable'] else 'n':4s} "
                     f"{f(r['max_abs_ic']):>7s} {str(r['peak_h']):>7s} {f(r['incr_vs_1h']):>7s} "
                     f"{f(r['exec_ic_lag0']):>6s} {f(r['exec_ic_lag1']):>6s} {r['verdict']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Intraday OI factor recon")
    p.add_argument("--symbol", required=True, help="short coin name, e.g. sol")
    p.add_argument("--interval", default="30m", choices=["15m", "30m"])
    p.add_argument("--cache-dir", default=str(_RESEARCH / "data" / "oi" / "_raw"))
    p.add_argument("--start", type=date.fromisoformat, default=date(2022, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date.today())
    args = p.parse_args(argv)

    from pipeline.config import load_config
    cfg = load_config()
    sym = next((s for s in cfg.symbols if s.name == args.symbol), None)
    if sym is None:
        raise SystemExit(f"unknown symbol {args.symbol!r}")

    oi_df = _load_interval_oi(sym.binance_usdt, args.interval, Path(args.cache_dir), args.start, args.end)
    if oi_df.empty:
        raise SystemExit(f"no OI data for {sym.binance_usdt} in range")
    from lib import okx_data
    days = (args.end - args.start).days
    candles = okx_data.fetch_candles(sym.okx_swap, days=days, bar=args.interval)
    report = run_recon(oi_df, candles["close"], args.interval, args.symbol)

    out_dir = _RESEARCH / "manifests" / args.interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"intraday_oi_recon_{args.symbol}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(_format_table(report))
    print(f"\nreport -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
