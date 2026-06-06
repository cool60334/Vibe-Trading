"""
Regenerate research/manifests/evidence_btc.json from the cached feature store +
OHLCV, using the corrected stage-0a IC measurement layer (stationary OBV z-score,
native-frequency funding/stablecoin). Offline — no network refetch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_RESEARCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RESEARCH))

from lib.factor_io import load_features, dump_evidence  # noqa: E402
from pipeline.stage0a_features import (  # noqa: E402
    compute_evidence_entries,
    sort_evidence_by_ic,
    build_evidence_payload,
)

HORIZONS = (8, 24, 72, 168)
OHLCV = _RESEARCH.parent / "runs" / "btc_s1_multi_factor_consensus_sweep_000" / "artifacts" / "ohlcv_BTC-USDT-SWAP.csv"
MANIFESTS = _RESEARCH / "manifests"


def main() -> None:
    feats = load_features("btc")
    if feats.index.tz is None:
        feats.index = feats.index.tz_localize("UTC")

    px = pd.read_csv(OHLCV, parse_dates=["trade_date"]).set_index("trade_date").sort_index()
    if px.index.tz is None:
        px.index = px.index.tz_localize("UTC")
    close = px["close"].reindex(feats.index, method="ffill")
    candles = pd.DataFrame({"close": close}, index=feats.index)

    feature_dict = {c: feats[c] for c in feats.columns}
    entries = compute_evidence_entries(candles, feature_dict, HORIZONS)
    sorted_entries = sort_evidence_by_ic(entries)
    payload = build_evidence_payload("btc", sorted_entries)
    dump_evidence("btc", payload, MANIFESTS)

    print(f"{'feature':<22}{'transform':<14}{'8h':>9}{'24h':>9}{'72h':>9}{'168h':>9}{'n':>8}")
    for e in sorted_entries:
        ic = e["ic_by_horizon"]
        def g(h):
            v = ic.get(h)
            return f"{v:+.4f}" if v is not None else "   nan"
        print(f"{e['feature_key']:<22}{str(e['ic_eval_transform']):<14}"
              f"{g(8):>9}{g(24):>9}{g(72):>9}{g(168):>9}{e['sample_size']:>8}")


if __name__ == "__main__":
    main()
