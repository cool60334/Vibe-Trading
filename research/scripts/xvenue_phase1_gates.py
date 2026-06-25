# research/scripts/xvenue_phase1_gates.py
"""Phase-1 kill-gate for the cross-venue premium factor (design spec §6).

Prereqs:
  1. MASSIVE_API_KEY set in env.
  2. Feature store + evidence regenerated for btc/eth/sol WITH the Massive fetch
     (run stage0a after backing up research/manifests/{btc,eth,sol}).

Prints, per (symbol, factor in {depeg_z, fiat_prem_z}):
  abs IC, partial IC (controls funding_z+basis_rel), entry-lag IC (lag0 vs lag1),
  sign-flip rate, net-of-cost decile spread.
Then you make the GO/No-Go call against the spec gate thresholds.
"""
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve()
_RESEARCH = _HERE.parents[1]

FACTORS = ["depeg_z", "fiat_prem_z"]
CONTROLS = ["funding_z", "basis_rel"]
HORIZON_H = 24
SYMBOLS = ["btc", "eth", "sol"]
FEATURE_DIR = _RESEARCH / "manifests"


def _load_feature_store(sym: str, cfg) -> pd.DataFrame:
    """Load feature parquet and join close price from OKX candles."""
    from lib.okx_data import fetch_candles
    from pipeline.config import SymbolConfig

    feats = pd.read_parquet(FEATURE_DIR / f"features_{sym}.parquet")
    sym_cfg = next(s for s in cfg.symbols if s.name == sym)
    candles = fetch_candles(sym_cfg.okx_swap, days=cfg.period, bar=cfg.interval)
    feats["close"] = candles["close"].reindex(feats.index).ffill()
    return feats


def main() -> None:
    if str(_RESEARCH) not in sys.path:
        sys.path.insert(0, str(_RESEARCH))

    from lib.factor_metrics import add_forward_returns, compute_ic
    from lib.factor_gates import partial_ic, sign_flip_rate, decile_spread_net_of_cost
    from lib.orderflow_eval import execution_ic
    from pipeline.config import load_config

    cfg = load_config()
    header = f"{'sym/factor':22}{'absIC':>9}{'partIC':>9}{'lag0':>9}{'lag1':>9}{'flip':>7}{'net_bps':>9}"
    print(header)
    print("-" * len(header))
    for sym in SYMBOLS:
        df = _load_feature_store(sym, cfg)
        if "close" not in df.columns:
            print(f"{sym}: no close column — skip")
            continue
        df = add_forward_returns(df, "close", [HORIZON_H], interval=cfg.interval)
        ret = df[f"ret_{HORIZON_H}h"]
        controls = [df[c] for c in CONTROLS if c in df.columns]
        for fac in FACTORS:
            if fac not in df.columns:
                print(f"{sym}/{fac:14} MISSING (no coverage?)")
                continue
            f = df[fac]
            abs_ic = compute_ic(f, ret)
            pic = partial_ic(f, controls, ret) if controls else float("nan")
            ic0 = execution_ic(f, df["close"], entry_lag_bars=0, hold_bars=HORIZON_H, min_obs=200)
            ic1 = execution_ic(f, df["close"], entry_lag_bars=1, hold_bars=HORIZON_H, min_obs=200)
            flip = sign_flip_rate(f)
            net = decile_spread_net_of_cost(f, ret, slippage_bps=7.5) * 1e4
            print(f"{sym}/{fac:14}{abs_ic:>9.3f}{pic:>9.3f}{ic0:>9.3f}{ic1:>9.3f}{flip:>7.2f}{net:>9.1f}")


if __name__ == "__main__":
    main()
