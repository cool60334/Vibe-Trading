"""Pre-fetch funding + F&G into factor_data/ for backtest signal_engine.

Uses Binance funding history via ccxt (2-year depth, vs OKX's 90-day public limit).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "research"))

from lib.ccxt_data import fetch_funding_rate_history_ccxt  # noqa: E402
from lib.sentiment import fetch_fear_greed  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "factor_data"
DATA_DIR.mkdir(exist_ok=True)


def main() -> None:
    print("fetch funding (Binance via ccxt, 730d)")
    funding = fetch_funding_rate_history_ccxt("binance", "BTC/USDT:USDT", days=730)
    funding.index = funding.index.tz_convert(None)
    funding.to_parquet(DATA_DIR / "funding.parquet")
    print(f"  saved {len(funding)} rows -> factor_data/funding.parquet")
    print(f"  range: {funding.index.min()} ~ {funding.index.max()}")

    print("fetch F&G (alternative.me, 730d)")
    fng = fetch_fear_greed(days=730)
    fng.index = fng.index.tz_convert(None)
    fng = fng[["fng"]]
    fng.to_parquet(DATA_DIR / "fng.parquet")
    print(f"  saved {len(fng)} rows -> factor_data/fng.parquet")
    print(f"  range: {fng.index.min()} ~ {fng.index.max()}")


if __name__ == "__main__":
    main()
