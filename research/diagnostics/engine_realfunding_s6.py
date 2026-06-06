"""
Fair verdict on btc_s6: re-run the REAL CryptoEngine but charge the ACTUAL
historical 8h funding (funding_rate_raw from the feature store) instead of the
flat 0.0001 proxy.

Method: subclass CryptoEngine and, each bar, set self.funding_rate to the real
per-timestamp rate before the funding hook runs — reusing the engine's exact
funding accounting (notional * rate * direction at 00/08/16 UTC, longs pay).

Brackets the answer with three funding modes:
  zero  : funding_rate = 0          (upper bound; what the funding-blind vectorised test assumed)
  real  : actual funding_rate_raw   (mean 6.3e-5/8h, 15% of slots negative -> longs paid)
  flat  : funding_rate = 0.0001     (the pessimistic proxy used in engine_validate_s6)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_RESEARCH = Path(__file__).resolve().parent.parent
_REPO = _RESEARCH.parent
for p in (str(_RESEARCH), str(_REPO), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from backtest.engines.crypto import CryptoEngine  # noqa: E402
from backtest.runner import _AutoLoader  # noqa: E402
from backtest.metrics import calc_bars_per_year  # noqa: E402
from lib.factor_io import load_features  # noqa: E402

SYMBOL = "BTC-USDT-SWAP"
OHLCV = _REPO / "runs" / "btc_s1_multi_factor_consensus_sweep_000" / "artifacts" / f"ohlcv_{SYMBOL}.csv"
OUT = _REPO / "runs" / "btc_s6_validation"
SPLIT = pd.Timestamp("2025-01-01")
BPY = calc_bars_per_year("1H", "okx")


class RealFundingCrypto(CryptoEngine):
    """CryptoEngine variant that charges per-timestamp funding from a lookup dict."""

    def attach_funding(self, lookup: dict) -> None:
        self._fund_lookup = lookup

    def on_bar(self, symbol, bar, timestamp):
        self.funding_rate = float(getattr(self, "_fund_lookup", {}).get(timestamp, 0.0))
        super().on_bar(symbol, bar, timestamp)


def load_ohlcv() -> pd.DataFrame:
    return pd.read_csv(OHLCV, parse_dates=["trade_date"]).set_index("trade_date").sort_index()


def real_funding_lookup(index: pd.DatetimeIndex) -> dict:
    fr = load_features("btc")["funding_rate_raw"]
    if fr.index.tz is not None:
        fr.index = fr.index.tz_localize(None)
    aligned = fr.reindex(index, method="ffill").fillna(0.0)
    return aligned.to_dict()


def make_signal():
    import importlib.util
    d = _RESEARCH / "strategies" / "code" / "btc_s6_stablecoin_sign_capped"
    s = importlib.util.spec_from_file_location("s6e", d / "signal_engine.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m.SignalEngine()


def run(df: pd.DataFrame, mode: str, tag: str) -> dict:
    config = {
        "codes": [SYMBOL], "source": "okx", "interval": "1H",
        "initial_cash": 1_000_000, "leverage": 1.0,
        "maker_rate": 0.0002, "taker_rate": 0.0005, "slippage": 0.001,
        "funding_rate": 0.0001 if mode == "flat" else 0.0,
    }
    run_dir = OUT / tag
    run_dir.mkdir(parents=True, exist_ok=True)
    if mode == "real":
        eng = RealFundingCrypto(config)
        eng.attach_funding(real_funding_lookup(df.index))
    else:
        eng = CryptoEngine(config)
    eng.run_backtest(config, _AutoLoader({SYMBOL: df}), make_signal(), run_dir, bars_per_year=BPY)
    return pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()


def fmt(m: dict) -> str:
    return (f"sharpe={m['sharpe']:+.2f}  ret={m['total_return']:+.1%}  "
            f"mdd={m['max_drawdown']:+.1%}  excess={m['excess_return']:+.1%}")


def main() -> None:
    df = load_ohlcv()
    is_df = df[df.index < SPLIT]
    oos_df = df[df.index >= SPLIT]
    print(f"BPY={BPY}  full={len(df)} IS={len(is_df)} OOS={len(oos_df)}  "
          f"(bench OOS = BTC -22%)\n")
    print(f"{'funding mode':<8}{'window':<6}  metrics")
    for mode in ("zero", "real", "flat"):
        for wlabel, wdf in (("full", df), ("IS", is_df), ("OOS", oos_df)):
            m = run(wdf, mode, f"rf_{mode}_{wlabel}")
            print(f"{mode:<8}{wlabel:<6}  {fmt(m)}")
        print()


if __name__ == "__main__":
    main()
