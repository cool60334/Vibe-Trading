"""
Standard-engine validation of btc_s6_stablecoin_sign_capped.

Drives the REAL CryptoEngine (maker/taker fees + slippage + 8h funding-fee
settlement + liquidation) on the cached 4-year OHLCV — no network, deterministic.
This is the honest test the vectorised diagnostic could NOT do: the engine charges
funding fees on the always-in-market position, which the vectorised pnl ignored.

Scenarios:
  full / IS (<2025-01) / OOS (>=2025-01)
  walk-forward: consecutive 6-month folds (each run through the engine)
  cost-stress: full + OOS at 1x / 2x / 3x (taker, slippage, funding)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_RESEARCH = Path(__file__).resolve().parent.parent
_REPO = _RESEARCH.parent
sys.path.insert(0, str(_RESEARCH))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent"))

from backtest.engines.crypto import CryptoEngine  # noqa: E402
from backtest.runner import _AutoLoader  # noqa: E402
from backtest.metrics import calc_bars_per_year  # noqa: E402

SYMBOL = "BTC-USDT-SWAP"
OHLCV = _REPO / "runs" / "btc_s1_multi_factor_consensus_sweep_000" / "artifacts" / f"ohlcv_{SYMBOL}.csv"
OUT = _REPO / "runs" / "btc_s6_validation"
SPLIT = pd.Timestamp("2025-01-01")
BPY = calc_bars_per_year("1H", "okx")


def load_ohlcv() -> pd.DataFrame:
    df = pd.read_csv(OHLCV, parse_dates=["trade_date"]).set_index("trade_date").sort_index()
    return df


def make_engine_signal():
    spec_dir = _RESEARCH / "strategies" / "code" / "btc_s6_stablecoin_sign_capped"
    import importlib.util
    s = importlib.util.spec_from_file_location("s6_engine", spec_dir / "signal_engine.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m.SignalEngine()


def run(df: pd.DataFrame, tag: str, mult: float = 1.0) -> dict:
    config = {
        "codes": [SYMBOL],
        "source": "okx",
        "interval": "1H",
        "initial_cash": 1_000_000,
        "leverage": 1.0,
        "maker_rate": 0.0002 * mult,
        "taker_rate": 0.0005 * mult,
        "slippage": 0.001 * mult,
        "funding_rate": 0.0001 * mult,
    }
    run_dir = OUT / tag
    run_dir.mkdir(parents=True, exist_ok=True)
    eng = CryptoEngine(config)
    eng.run_backtest(config, _AutoLoader({SYMBOL: df}), make_engine_signal(), run_dir, bars_per_year=BPY)
    m = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    return m


def line(tag: str, m: dict) -> str:
    return (f"{tag:<22} sharpe={m['sharpe']:+.2f}  ret={m['total_return']:+.1%}  "
            f"mdd={m['max_drawdown']:+.1%}  trades={int(m['trade_count'])}  "
            f"bench={m['benchmark_return']:+.1%}  excess={m['excess_return']:+.1%}")


def main() -> None:
    df = load_ohlcv()
    is_df = df[df.index < SPLIT]
    oos_df = df[df.index >= SPLIT]
    print(f"bars_per_year(1H,okx)={BPY}   full={len(df)}  IS={len(is_df)}  OOS={len(oos_df)}\n")

    print("=== headline (real engine: fees + slippage + 8h funding) ===")
    print(line("full", run(df, "full")))
    print(line("IS <2025-01", run(is_df, "is")))
    print(line("OOS >=2025-01", run(oos_df, "oos")))

    print("\n=== walk-forward: consecutive 6-month folds ===")
    folds = list(df.groupby(pd.Grouper(freq="6MS")))
    pos = 0
    for label, fdf in folds:
        if len(fdf) < 24 * 60:
            continue
        m = run(fdf, f"wf_{label.date()}")
        pos += 1 if m["sharpe"] > 0 else 0
        print(line(str(label.date()), m))
    print(f"folds sharpe>0: {pos}/{sum(1 for _,f in folds if len(f) >= 24*60)}")

    print("\n=== cost-stress (taker/slippage/funding x mult) ===")
    for mult in (1.0, 2.0, 3.0):
        mf = run(df, f"cs_full_{mult}", mult=mult)
        mo = run(oos_df, f"cs_oos_{mult}", mult=mult)
        print(f"x{mult:.0f}  full sharpe={mf['sharpe']:+.2f} ret={mf['total_return']:+.1%}   "
              f"OOS sharpe={mo['sharpe']:+.2f} ret={mo['total_return']:+.1%}")


if __name__ == "__main__":
    main()
