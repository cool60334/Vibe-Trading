"""Layer 1 validation — does live signal match a fresh backtest of same period?

Builds a one-shot backtest run dir matching the most recent N days of live data,
runs the backtest engine on it, then compares the engine's signal series against
the live log. Any divergence = bug in fetch / signal / log path.

Usage:
    python live/check_parity.py
    python live/check_parity.py --days 14
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

from lib.ccxt_data import fetch_funding_rate_history_ccxt  # noqa: E402
from lib.sentiment import fetch_fear_greed  # noqa: E402

LIVE_LOG = ROOT / "live" / "state" / "signals.parquet"
TEMPLATE = ROOT / "runs" / "_templates" / "signal_engine_short_only.py"
REGIME_PARQUET = ROOT / "research" / "regime_labels.parquet"


def build_check_run(start_str: str, end_str: str) -> Path:
    dst = ROOT / "runs" / "live_parity_check"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    (dst / "code").mkdir()
    shutil.copy(TEMPLATE, dst / "code" / "signal_engine.py")

    (dst / "factor_data").mkdir()
    start_dt = datetime.fromisoformat(start_str + "T00:00:00+00:00")
    end_dt = datetime.fromisoformat(end_str + "T00:00:00+00:00") + timedelta(days=1)

    print(f"[parity] fetch funding {start_str} → {end_str}")
    fund = fetch_funding_rate_history_ccxt("binance", "BTC/USDT:USDT", since_dt=start_dt - timedelta(days=120), until_dt=end_dt)
    fund.index = fund.index.tz_convert(None)
    fund.to_parquet(dst / "factor_data" / "funding.parquet")

    print(f"[parity] fetch F&G")
    fng = fetch_fear_greed(days=300)
    fng.index = fng.index.tz_convert(None) if fng.index.tz is not None else fng.index
    fng = fng[["fng"]]
    fng.to_parquet(dst / "factor_data" / "fng.parquet")

    regime_full = pd.read_parquet(REGIME_PARQUET)
    rg = regime_full.loc[start_str:end_str][["regime"]]
    rg.to_parquet(dst / "factor_data" / "regime.parquet")

    cfg = {
        "codes": ["BTC-USDT"],
        "start_date": start_str,
        "end_date": end_str,
        "source": "ccxt",
        "interval": "1H",
        "engine": "daily",
        "initial_cash": 1000.0,
        "leverage": 1.5,
        "maker_rate": 0.0002,
        "taker_rate": 0.00055,
        "slippage": 0.0005,
        "margin_mode": "isolated",
        "benchmark": "BTC-USDT",
    }
    (dst / "config.json").write_text(json.dumps(cfg, indent=2))
    print(f"[parity] built {dst.relative_to(ROOT)}")
    return dst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="Window to compare (default 14)")
    args = parser.parse_args()

    if not LIVE_LOG.exists():
        print("ERROR: no live log yet. Run live/alert_only.py first.")
        return

    live = pd.read_parquet(LIVE_LOG)
    end = live.index.max().normalize()
    start = end - pd.Timedelta(days=args.days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    run_dir = build_check_run(start_str, end_str)

    print(f"[parity] running backtest engine on {run_dir.name}")
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.runner", str(run_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print(f"[parity] backtest FAILED:\n{proc.stderr[-500:]}")
        return

    # backtest writes positions.csv to artifacts/
    pos_path = run_dir / "artifacts" / "positions.csv"
    if not pos_path.exists():
        print(f"[parity] no positions.csv at {pos_path}")
        return
    bt = pd.read_csv(pos_path, parse_dates=[0], index_col=0)
    bt.index = bt.index.tz_localize(None) if bt.index.tz is not None else bt.index
    bt_pos = bt.iloc[:, 0]  # first column = position weight per timestamp

    live_pos = live["position"].loc[start:end]

    # align on common index
    common = bt_pos.index.intersection(live_pos.index)
    if len(common) == 0:
        print(f"[parity] no overlapping timestamps! live={live_pos.index[:3].tolist()} bt={bt_pos.index[:3].tolist()}")
        return

    diff = (bt_pos.loc[common] - live_pos.loc[common]).abs()
    max_diff = diff.max()
    n_mismatch = int((diff > 1e-6).sum())
    pct_match = 100.0 * (len(common) - n_mismatch) / len(common)

    print(f"\n[parity] {len(common)} aligned hours  match={pct_match:.2f}%  mismatches={n_mismatch}  max_diff={max_diff:.6f}")

    if n_mismatch > 0:
        first_bad = diff[diff > 1e-6].head(5)
        print(f"[parity] first mismatches:")
        for ts in first_bad.index:
            print(f"  {ts}  live={live_pos.loc[ts]:+.4f}  bt={bt_pos.loc[ts]:+.4f}  diff={diff.loc[ts]:+.4f}")
    else:
        print(f"[parity] ✅ live signal == backtest signal across {len(common)} hours")


if __name__ == "__main__":
    main()
