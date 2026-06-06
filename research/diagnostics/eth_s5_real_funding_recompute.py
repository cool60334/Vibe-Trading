"""
eth_s5_real_funding_recompute.py
─────────────────────────────────
One-off diagnostic: recompute eth_s5_half_size OOS (and train) PnL replacing
the backtest engine's FIXED funding rate (config default 0.0001 per 8h
settlement) with the REAL funding_rate_raw series stored in
features_eth.parquet. Report the cost differential and adjusted metrics.

The CryptoEngine deducts funding via:
    fee = notional * funding_rate * direction
at settlements when bar.hour ∈ {0, 8, 16}. We replicate this per trade using
the recorded entry/exit timestamps from trades.csv and the real funding
series, then output the delta on annualised return / sharpe.

Run from repo root:
    python -m research.diagnostics.eth_s5_real_funding_recompute
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNDING_HOURS = {0, 8, 16}
FIXED_RATE = 0.0001  # CryptoEngine default

RUNS = [
    ("train", REPO_ROOT / "runs" / "eth_s5_half_size_train"),
    ("oos", REPO_ROOT / "runs" / "eth_s5_half_size_oos"),
]


def _pair_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Pair open/close rows from trades.csv into one row per round-trip.

    Backtest engine writes one row per leg. Open row has pnl=0, close row
    has pnl != 0. We pair consecutive open→close rows.
    """
    rows = []
    open_row = None
    for _, r in trades.iterrows():
        if open_row is None:
            open_row = r
        else:
            close_row = r
            # Direction: if open side == "buy", we went LONG (direction +1);
            # if open side == "sell", we shorted (direction -1).
            direction = 1 if open_row["side"] == "buy" else -1
            rows.append(
                {
                    "entry_ts": pd.Timestamp(open_row["timestamp"]),
                    "exit_ts": pd.Timestamp(close_row["timestamp"]),
                    "direction": direction,
                    "qty": float(open_row["qty"]),
                    "entry_price": float(open_row["price"]),
                    "exit_price": float(close_row["price"]),
                    "pnl_engine": float(close_row["pnl"]),
                    "return_pct_engine": float(close_row["return_pct"]),
                }
            )
            open_row = None
    return pd.DataFrame(rows)


def _real_funding_cost(
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    direction: int,
    qty: float,
    entry_price: float,
    funding_series: pd.Series,
) -> tuple[float, float, int]:
    """Return (real_cost, fixed_cost, n_settlements) for one round-trip.

    Iterates 1h bars between entry_ts and exit_ts and accrues funding at
    each settlement hour. Cost = notional * rate * direction; positive
    cost = longs pay (or shorts gain when rate < 0). We use entry_price as
    notional proxy (engine uses mark_price each bar; entry_price gives
    a tighter close-form vs the engine's mark-to-market drift, but the
    difference per bar is < 1%).
    """
    # Reindex funding to 1H grid spanning the trade.
    notional = abs(qty) * entry_price  # treat qty sign as direction
    rng = pd.date_range(entry_ts, exit_ts, freq="1h", tz=funding_series.index.tz)
    # Trim to settlement hours only.
    settlements = [ts for ts in rng if ts.hour in FUNDING_HOURS]

    real_cost = 0.0
    fixed_cost = 0.0
    for ts in settlements:
        # Use nearest forward-fill from funding series (8h funding is ffilled).
        try:
            rate = funding_series.asof(ts)
        except KeyError:
            continue
        if pd.isna(rate):
            continue
        real_cost += notional * float(rate) * direction
        fixed_cost += notional * FIXED_RATE * direction
    return real_cost, fixed_cost, len(settlements)


def _annualised(total_return: float, days: float) -> float:
    if days <= 0:
        return 0.0
    return (1 + total_return) ** (365.0 / days) - 1


def _sharpe(returns: np.ndarray, bars_per_year: int = 365) -> float:
    if len(returns) < 2 or returns.std() == 0:
        return 0.0
    return float(np.sqrt(bars_per_year) * returns.mean() / returns.std())


def _process_run(run_name: str, run_dir: Path, funding_series: pd.Series) -> dict:
    trades_csv = run_dir / "artifacts" / "trades.csv"
    metrics_csv = run_dir / "artifacts" / "metrics.csv"
    if not trades_csv.exists():
        return {"error": f"missing {trades_csv}"}

    trades = pd.read_csv(trades_csv)
    paired = _pair_trades(trades)

    real_costs = []
    fixed_costs = []
    settlements = []
    for _, row in paired.iterrows():
        real, fixed, n = _real_funding_cost(
            row["entry_ts"],
            row["exit_ts"],
            int(row["direction"]),
            row["qty"],
            row["entry_price"],
            funding_series,
        )
        real_costs.append(real)
        fixed_costs.append(fixed)
        settlements.append(n)

    paired["funding_real"] = real_costs
    paired["funding_fixed"] = fixed_costs
    paired["funding_delta"] = paired["funding_fixed"] - paired["funding_real"]
    paired["settlements"] = settlements

    starting_capital = 1_000_000.0
    engine_pnl_total = paired["pnl_engine"].sum()
    cost_delta_total = paired["funding_delta"].sum()  # positive = real cheaper than fixed
    adjusted_pnl_total = engine_pnl_total + cost_delta_total

    engine_metrics = pd.read_csv(metrics_csv).iloc[0].to_dict()

    # Per-trade adjusted returns (use original starting capital as denom)
    paired["pnl_adjusted"] = paired["pnl_engine"] + paired["funding_delta"]
    paired["ret_engine"] = paired["pnl_engine"] / starting_capital
    paired["ret_adjusted"] = paired["pnl_adjusted"] / starting_capital

    span_days = (paired["exit_ts"].iloc[-1] - paired["entry_ts"].iloc[0]).days

    out = {
        "run": run_name,
        "n_trades": int(len(paired)),
        "total_settlements": int(paired["settlements"].sum()),
        "starting_capital": starting_capital,
        "span_days": span_days,
        "engine_metrics": {
            "total_return": float(engine_metrics.get("total_return", 0)),
            "annual_return": float(engine_metrics.get("annual_return", 0)),
            "sharpe": float(engine_metrics.get("sharpe", 0)),
            "max_drawdown": float(engine_metrics.get("max_drawdown", 0)),
            "final_value": float(engine_metrics.get("final_value", 0)),
        },
        "funding_cost_USD": {
            "fixed_assumption": float(paired["funding_fixed"].sum()),
            "real_series": float(paired["funding_real"].sum()),
            "delta_USD": float(cost_delta_total),
        },
        "adjusted_estimate": {
            "total_pnl_engine": float(engine_pnl_total),
            "total_pnl_adjusted": float(adjusted_pnl_total),
            "final_value_adjusted": starting_capital + adjusted_pnl_total,
            "total_return_adjusted": float(adjusted_pnl_total / starting_capital),
            "annual_return_adjusted": _annualised(
                adjusted_pnl_total / starting_capital, span_days
            ),
            "sharpe_adjusted_estimate": _sharpe(paired["ret_adjusted"].values * 252),  # rough
        },
        "per_trade_summary": {
            "median_funding_real_USD": float(paired["funding_real"].median()),
            "median_funding_fixed_USD": float(paired["funding_fixed"].median()),
            "median_hold_settlements": float(paired["settlements"].median()),
        },
    }
    return out


def main() -> None:
    features_path = REPO_ROOT / "research" / "manifests" / "features_eth.parquet"
    features = pd.read_parquet(features_path)
    funding_series = features["funding_rate_raw"]
    # Make tz-aware index match trades.csv timestamps.
    if funding_series.index.tz is None:
        funding_series.index = funding_series.index.tz_localize("UTC")

    print("=" * 70)
    print("ETH s5 — funding cost diagnostic: fixed 0.0001 vs real series")
    print("=" * 70)
    print(
        f"Real funding stats:  mean={funding_series.mean():+.6f}  "
        f"std={funding_series.std():.6f}  "
        f"min={funding_series.min():+.6f}  max={funding_series.max():+.6f}"
    )
    print(f"Fixed assumption:    {FIXED_RATE:+.6f}")
    print(
        f"Ratio (real/fixed):  {funding_series.mean() / FIXED_RATE:.3f}x"
    )
    print()

    results = {}
    for run_name, run_dir in RUNS:
        res = _process_run(run_name, run_dir, funding_series)
        results[run_name] = res
        print(f"--- {run_name} ({run_dir.name}) ---")
        if "error" in res:
            print("  ERROR:", res["error"])
            continue
        m = res["engine_metrics"]
        f = res["funding_cost_USD"]
        a = res["adjusted_estimate"]
        print(f"  trades:                  {res['n_trades']}")
        print(f"  total settlements:       {res['total_settlements']}")
        print(f"  span (days):             {res['span_days']}")
        print()
        print(f"  ENGINE (fixed 0.0001):")
        print(f"    total_return:          {m['total_return']:+.4%}")
        print(f"    annual_return:         {m['annual_return']:+.4%}")
        print(f"    sharpe:                {m['sharpe']:+.4f}")
        print(f"    max_drawdown:          {m['max_drawdown']:+.4%}")
        print()
        print(f"  FUNDING COST (USD, total):")
        print(f"    fixed assumption:      ${f['fixed_assumption']:>12,.0f}")
        print(f"    real series:           ${f['real_series']:>12,.0f}")
        print(f"    delta (savings):       ${f['delta_USD']:>+12,.0f}")
        print()
        print(f"  ADJUSTED (with real funding):")
        print(f"    final_value:           ${a['final_value_adjusted']:>12,.0f}")
        print(f"    total_return:          {a['total_return_adjusted']:+.4%}")
        print(f"    annual_return:         {a['annual_return_adjusted']:+.4%}")
        print()

    # Write JSON summary
    out_path = REPO_ROOT / "research" / "manifests" / "eth_s5_half_size" / "real_funding_recompute.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
