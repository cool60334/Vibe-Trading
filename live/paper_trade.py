"""Layer-2 paper trade — alert_only signal + ccxt Bybit testnet execution.

Reads same signal pipeline as alert_only.py, then reconciles Bybit testnet
position to target. Logs all orders to state/orders.parquet.

REQUIRED env vars (or .env file at repo root):
    BYBIT_TESTNET_API_KEY
    BYBIT_TESTNET_API_SECRET

Optional env:
    LIVE_CAPITAL_USDT     (default 1000)
    LIVE_LEVERAGE         (default 1.5)
    LIVE_DRY_RUN          (default 0; set 1 to log orders without sending)

Usage (after env exported):
    python live/paper_trade.py
    python live/paper_trade.py --dry-run        # never sends orders
    python live/paper_trade.py --no-signal      # skip signal compute, only reconcile current target
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "live"))

from alert_only import (  # noqa: E402
    LOG_PATH as SIGNAL_LOG,
    compute_signals,
    fetch_live_data,
    load_log,
    append_log,
    alert,
)
from bybit_client import BybitClient, SafetyConfig  # noqa: E402

STATE_DIR = ROOT / "live" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ORDERS_LOG = STATE_DIR / "orders.parquet"


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def append_order_log(row: dict) -> None:
    df_new = pd.DataFrame([row])
    if ORDERS_LOG.exists():
        df = pd.read_parquet(ORDERS_LOG)
        df = pd.concat([df, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_parquet(ORDERS_LOG)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=130)
    parser.add_argument("--dry-run", action="store_true", help="never send orders")
    parser.add_argument("--no-signal", action="store_true", help="skip signal compute, use last logged target")
    parser.add_argument("--webhook", default=None)
    args = parser.parse_args()

    load_env_file()

    api_key = os.getenv("BYBIT_TESTNET_API_KEY") or os.getenv("BYBIT_API_KEY")
    secret = os.getenv("BYBIT_TESTNET_API_SECRET") or os.getenv("BYBIT_API_SECRET")
    if not api_key or not secret:
        print("ERROR: BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET not set in env or .env")
        sys.exit(1)

    capital = float(os.getenv("LIVE_CAPITAL_USDT", "1000"))
    leverage = float(os.getenv("LIVE_LEVERAGE", "1.5"))
    dry_run = args.dry_run or os.getenv("LIVE_DRY_RUN", "0") == "1"

    # ---------------- signal step (skip if --no-signal) ----------------
    if not args.no_signal:
        log_before = load_log()
        prev_position = None if log_before.empty else float(log_before["position"].iloc[-1])
        data = fetch_live_data(args.lookback_days)
        sig = compute_signals(data)
        if log_before.empty:
            new_rows = sig
        else:
            new_rows = sig.loc[sig.index > log_before.index.max()]
        if not new_rows.empty:
            append_log(new_rows)
            print(f"[signal] appended {len(new_rows)} rows")
        latest = sig.iloc[-1]
        alert(latest, prev_position, args.webhook)
    else:
        sig_log = load_log()
        if sig_log.empty:
            print("ERROR: --no-signal but no log; run alert_only.py first")
            sys.exit(1)
        latest = sig_log.iloc[-1]

    target = float(latest["position"])
    signal_close = float(latest["close"])
    signal_ts = latest.name

    print(f"\n[reconcile] target={target:+.4f} signal_close=${signal_close:,.0f} ts={signal_ts}")

    # ---------------- execution ----------------
    client = BybitClient(api_key, secret, safety=SafetyConfig())
    balance = client.fetch_balance_usdt()
    print(f"[bybit] testnet balance: {balance:.2f} USDT (using capital cap {capital})")

    result = client.reconcile_to_target(
        target_position_weight=target,
        capital_usdt=capital,
        leverage=leverage,
        signal_mark_price=signal_close,
        dry_run=dry_run,
    )

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal_ts": signal_ts.isoformat() if hasattr(signal_ts, "isoformat") else str(signal_ts),
        "target_weight": target,
        "leverage": leverage,
        "capital": capital,
        "dry_run": dry_run,
        **result,
    }
    append_order_log(row)

    print(f"\n[order] {json.dumps(result, indent=2, default=str)}")

    if args.webhook and result.get("status") in ("filled", "aborted", "error"):
        try:
            req = urllib.request.Request(
                args.webhook,
                data=json.dumps(row, default=str).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
            print(f"[webhook] posted order result")
        except Exception as e:
            print(f"[webhook] failed: {e}")


if __name__ == "__main__":
    main()
