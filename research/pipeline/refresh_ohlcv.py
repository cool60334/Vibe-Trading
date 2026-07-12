"""Refresh full-span OHLCV parquets for the Foundry.

Fetches OKX candles per configured symbol, validates coverage against that
symbol's feature index using the run's own _align_ohlcv gate, and atomically
writes ohlcv_<sym>.parquet next to features_<sym>.parquet. Mirrors
refresh_factors: per-symbol independent; exit 0 if all ok, 1 if any failed."""
from __future__ import annotations

import sys
from pathlib import Path

# ── path bootstrap: research/ (bare pipeline.*/lib.*) + repo root (research.hermes.*)
_THIS = Path(__file__).resolve()
_RESEARCH_DIR = _THIS.parents[1]
_REPO_ROOT = _THIS.parents[2]
for _p in (_REPO_ROOT, _RESEARCH_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import logging
from datetime import datetime, timezone

from pipeline.config import load_config
from lib.okx_data import fetch_candles
from lib.factor_io import load_features, _atomic_to_parquet, _symbol_short
from research.hermes.orchestrator import _align_ohlcv

log = logging.getLogger(__name__)
BUFFER_DAYS = 3


def refresh_symbol(sym_cfg, cfg, manifests_dir) -> bool:
    """Fetch full-span OKX candles for one symbol, validate coverage against its
    feature index, and atomically write ohlcv_<short>.parquet. Returns True on
    success. Any failure logs and returns False, leaving the old parquet intact."""
    short = _symbol_short(sym_cfg.name)
    try:
        feats = load_features(sym_cfg.name, manifests_dir=manifests_dir)
    except FileNotFoundError as exc:
        log.error("%s: no features (%s); skipping", short, exc)
        return False
    if feats.empty:
        log.error("%s: features parquet is empty; skipping", short)
        return False
    # both sides tz-aware: naive utcnow() would raise TypeError against the UTC index
    days = (datetime.now(timezone.utc) - feats.index.min()).days + BUFFER_DAYS
    try:
        ohlcv = fetch_candles(sym_cfg.okx_swap, days, bar=cfg.interval)
    except Exception as exc:      # noqa: BLE001 - one symbol's fetch must not sink the rest
        log.error("%s: OKX fetch failed (%s); old parquet left intact", short, exc)
        return False
    try:
        _align_ohlcv(ohlcv, feats.index)          # the run's own gate (close non-NaN >= 95%)
    except ValueError as exc:
        log.error("%s: fetched OHLCV does not cover features (%s); not writing", short, exc)
        return False
    _atomic_to_parquet(ohlcv, Path(manifests_dir) / f"ohlcv_{short}.parquet")
    log.info("%s: wrote ohlcv (%d bars, %s .. %s)", short, len(ohlcv),
             ohlcv.index.min(), ohlcv.index.max())
    return True


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(
        prog="refresh_ohlcv",
        description="Fetch full-span OHLCV per configured symbol for the Foundry")
    ap.add_argument("--config", default=None, help="research_config.yaml path")
    ap.add_argument("--manifests-dir", type=Path, default=None,
                    help="override output dir (default: repo/<cfg.feature_store_path>, interval-aware)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)                # honors RESEARCH_ONLY_SYMBOL / RESEARCH_INTERVAL
    mdir = args.manifests_dir.resolve() if args.manifests_dir is not None \
        else _REPO_ROOT / cfg.feature_store_path
    log.info("refresh_ohlcv: manifests=%s symbols=%s", mdir, [s.name for s in cfg.symbols])

    results = {s.name: refresh_symbol(s, cfg, mdir) for s in cfg.symbols}
    failed = [s for s, ok in results.items() if not ok]
    if failed:
        log.error("refresh_ohlcv: failed symbols: %s", failed)
        return 1
    log.info("refresh_ohlcv: all symbols ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
