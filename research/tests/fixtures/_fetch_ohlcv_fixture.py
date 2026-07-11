"""One-time generator for ohlcv_eth_1h.parquet. Run manually once; the parquet is
committed and the e2e reads it offline. Kept for reproducibility."""
from pathlib import Path
from research.lib.okx_data import fetch_candles

def main() -> None:
    # fetch_candles(days=...) computes its cutoff from *now*, not from the target
    # window. days=730 (the brief's original value) only reaches back exactly 2
    # calendar years from today, which — depending on run date — can land after
    # 2024-06-01 and truncate the fixture below the required ~4300+ bars. Use
    # days=800 for headroom so the fetch always reaches back past 2024-06-01
    # regardless of run date, then slice to the intended pre-OOS window.
    df = fetch_candles("ETH-USDT-SWAP", days=800, bar="1H")
    df = df.loc["2024-06-01":"2024-12-31"]          # pre-oos (oos_start=2025-01-01)
    out = Path(__file__).with_name("ohlcv_eth_1h.parquet")
    df.to_parquet(out)
    print(f"wrote {out} rows={len(df)} {df.index.min()}..{df.index.max()}")

if __name__ == "__main__":
    main()
