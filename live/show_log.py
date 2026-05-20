"""Quick viewer for signals.parquet — show recent state, changes, summary.

Usage:
    python live/show_log.py                  # last 24 hours
    python live/show_log.py --tail 100       # last 100 hours
    python live/show_log.py --changes        # only signal change events
    python live/show_log.py --summary        # regime distribution + stats
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

LOG = Path(__file__).resolve().parents[1] / "live" / "state" / "signals.parquet"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=24)
    parser.add_argument("--changes", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if not LOG.exists():
        print(f"no log at {LOG}\nrun: python live/alert_only.py")
        return

    df = pd.read_parquet(LOG)
    print(f"log: {LOG}")
    print(f"rows: {len(df)}   range: {df.index.min()} → {df.index.max()}\n")

    if args.summary:
        print("=== regime distribution ===")
        print(df["regime"].value_counts(normalize=True).mul(100).round(1).to_string())
        print(f"\n=== position stats ===")
        print(f"  hours short (pos<0):  {(df['position'] < 0).sum()}  ({(df['position'] < 0).mean()*100:.1f}%)")
        print(f"  hours flat  (pos==0): {(df['position'] == 0).sum()}  ({(df['position'] == 0).mean()*100:.1f}%)")
        print(f"  max short: {df['position'].min():+.3f}")
        print(f"  mean position: {df['position'].mean():+.4f}")
        print(f"\n=== latest 5 ===")
        cols = ["regime", "position", "close", "funding", "fng", "s2", "s3", "s4"]
        print(df[cols].tail(5).to_string())
        return

    if args.changes:
        chg = df[df["position"].diff().abs() > 1e-9]
        print(f"=== {len(chg)} signal change events ===")
        cols = ["regime", "position", "close", "funding", "fng", "s2", "s3", "s4"]
        print(chg[cols].tail(args.tail).to_string())
        return

    cols = ["regime", "position", "close", "funding", "fng", "s2", "s3", "s4"]
    print(df[cols].tail(args.tail).to_string())

    latest = df.iloc[-1]
    print(f"\n=== latest @ {latest.name} ===")
    print(f"  regime:   {latest['regime']}")
    print(f"  position: {latest['position']:+.4f}")
    print(f"  close:    ${latest['close']:,.0f}")
    print(f"  funding:  {latest['funding']:+.6f}")
    print(f"  F&G:      {latest['fng']:.0f}")
    print(f"  S2/S3/S4: {latest['s2']:+.1f} / {latest['s3']:+.1f} / {latest['s4']:+.1f}")


if __name__ == "__main__":
    main()
