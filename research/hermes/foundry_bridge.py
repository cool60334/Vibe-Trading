"""Foundry -> pipeline bridge.

Foundry vets factors but stores only PRE-OOS values (the OOS window is
deliberately reserved), so its frozen values cannot back a walk-forward
backtest. The bridge therefore re-runs each candidate's PERSISTED CODE over the
full span, reconciles the pre-oos slice against what Foundry stored, and hands
the result to the pipeline as an overlay.

It NEVER writes production features_<sym>.parquet — promote.py stays the only
path into what the live trader reads.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT / "research", _REPO_ROOT / "dashboard" / "server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from schemas import FactorEntry, FactorVerdict          # noqa: E402
from research.factor_regime import classify_stability, refine_verdict   # noqa: E402
from research.hermes.candidate_store import _candidate_path, load_candidate_code  # noqa: E402
from research.hermes.evidence_card import VERDICT_CANDIDATE               # noqa: E402
from research.hermes.evidence_store import load_cards                    # noqa: E402
from research.hermes.foundry_runner import resolve_image_id               # noqa: E402
from research.lib.factor_io import _atomic_to_parquet, _symbol_short      # noqa: E402

log = logging.getLogger(__name__)

FOUNDRY_PREFIX = "foundry_"
DEFAULT_CAP = 50


def recompute_full_span(code: str, panel: pd.DataFrame, run_sandbox) -> pd.Series:
    """Re-run a forged factor over the WHOLE panel (train + OOS).

    `run_sandbox(code, panel) -> pd.Series` is injected (real DockerSandbox in
    production, a fake in tests) so the bridge never imports docker directly.
    """
    series = run_sandbox(code, panel)
    series = pd.Series(series, index=panel.index) if not isinstance(series, pd.Series) else series
    if not series.index.equals(panel.index):
        series = series.reindex(panel.index)
    return series


def cached_recompute(code: str, code_sha: str, panel: pd.DataFrame, run_sandbox,
                     cache_dir) -> pd.Series:
    """Reuse a previously recomputed full-span series when the code sha matches,
    so the nightly cadence never re-runs the sandbox for an unchanged factor.

    The cache is keyed on code_sha alone, not on the panel's index — the panel
    can grow between nightly runs (new bars appended) while the factor's code
    stays byte-identical. A cache hit is only trusted if the CACHED data's own
    index (before any reindexing) already covers the panel's current tail
    (`cached_max >= panel_max`); reindexing a shorter cached series onto a
    longer panel would otherwise return legitimate-looking NaN for every new
    bar forever, since a code-sha hit would keep matching on every future call
    and the cache would never regenerate. When the panel has grown past what's
    cached, this is treated as a miss: recompute over the full (now-longer)
    span and overwrite the cache file with the fresh, fully-covering result.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{code_sha}.parquet"
    if cached.exists():
        s = pd.read_parquet(cached).iloc[:, 0]
        if s.index.max() >= panel.index.max():
            # Normalise the name away: the parquet column is always literally
            # "v", while a freshly computed series is typically unnamed.
            # Without this, a cache hit and a cache miss would return series
            # that compare unequal on .name alone even though the values are
            # identical.
            return s.reindex(panel.index).rename(None)
        # Cached data's tail is older than the panel's current tail — the
        # panel has grown since this cache entry was written. Fall through
        # to a full recompute rather than returning a series whose newest
        # rows would be silently (and permanently) NaN.
    series = recompute_full_span(code, panel, run_sandbox)
    _atomic_to_parquet(series.to_frame("v"), cached)
    return series.rename(None)


def reconciles_pre_oos(recomputed: pd.Series, stored: pd.Series,
                       oos_start: str, atol: float = 1e-8) -> bool:
    """Does the re-run reproduce what Foundry stored on the pre-oos window?

    A mismatch means the code is not reproducible in this environment — a
    non-causal residue that only shows up once future rows exist, or a drifted
    sandbox image. Either way the factor must be dropped, not trusted.

    equal_nan=True is REQUIRED: a rolling factor is NaN through its warm-up
    window, and the numpy default (equal_nan=False) would report every such
    factor as a mismatch and silently kill it. (forge's own pit_check_via_sandbox
    compares with equal_nan=True for exactly this reason.)
    """
    cutoff = pd.Timestamp(oos_start)
    if cutoff.tz is None:
        cutoff = cutoff.tz_localize(recomputed.index.tz)
    left = recomputed[recomputed.index < cutoff]
    right = stored.reindex(left.index)
    if left.empty:
        return False
    return bool(np.allclose(left.to_numpy(dtype="float64"),
                            right.to_numpy(dtype="float64"),
                            atol=atol, rtol=0.0, equal_nan=True))


def card_to_entry(card, horizon_h: int) -> FactorEntry:
    """Foundry evidence card -> pipeline FactorEntry.

    Every statistic is taken from the card, i.e. from Foundry's PRE-OOS
    evaluation. Nothing is recomputed from the full-span series — that would
    leak OOS information into the selection stage2 performs.

    `stability` reuses the pipeline's own classify_stability(cross_regime_ic)
    (a regime_stable/conditional classification) rather than inventing a
    different metric, so stage2's filters read it with the same meaning as a
    library factor's.
    """
    regime_ic = dict(card.regime_ic or {})
    stability = classify_stability(regime_ic) if regime_ic else None
    verdict = FactorVerdict.SINGLE_USE
    if stability is not None:
        verdict = refine_verdict(verdict, stability)   # conditional -> ensemble_only
    return FactorEntry(
        name=f"{FOUNDRY_PREFIX}{card.factor_id}",
        ic_by_horizon={int(horizon_h): card.gross_ic},
        ir=float(card.ir),
        sample_size=int(card.n_samples),
        cross_regime_ic=regime_ic or None,
        stability=stability,
        verdict=verdict,
    )


def build_overlay(symbol, manifests_dir, panel, run_sandbox, oos_start,
                  horizon_h: int, cap: int = DEFAULT_CAP):
    """Recompute every reconciling candidate over `panel` and return
    (overlay_df, entries). Any factor that cannot be trusted is DROPPED with a
    log line — a bad factor must never silently poison a backtest, and the
    bridge must never crash the pipeline."""
    cards = [c for c in load_cards(symbol, manifests_dir)
             if getattr(c, "verdict", None) == VERDICT_CANDIDATE]
    # OOM guard: Foundry accumulates candidates over time; an unbounded
    # full-span concat would blow up the stage subprocesses.
    cards.sort(key=lambda c: (getattr(c, "dsr", 0.0) or 0.0), reverse=True)
    cards = cards[:cap]

    cand_path = _candidate_path(symbol, manifests_dir)
    stored = pd.read_parquet(cand_path) if Path(cand_path).exists() else pd.DataFrame()

    cols: dict = {}
    entries: list = []
    for card in cards:
        fid = card.factor_id
        name = f"{FOUNDRY_PREFIX}{fid}"
        if name in panel.columns:
            log.warning("bridge: %s collides with an existing feature; skipping", name)
            continue
        try:
            code, meta = load_candidate_code(fid, symbol, manifests_dir)
        except FileNotFoundError:
            log.warning("bridge: no stored code for %s; skipping", fid)
            continue
        if fid not in stored.columns:
            log.warning("bridge: %s absent from candidate parquet; skipping", fid)
            continue
        # The card's gross_ic was measured at the horizon Foundry ran with, so
        # reuse THAT per-factor horizon. (research_config's horizons_h is
        # (8, 24, 72, 168) — taking its first element would mislabel the IC as 8h.)
        _h = meta.get("horizon_h")
        h = int(_h) if _h is not None else int(horizon_h)
        try:
            cache_dir = (Path(manifests_dir) / "candidate_features" / "recompute_cache"
                        / _symbol_short(symbol))
            series = cached_recompute(code, meta.get("code_sha256") or fid, panel,
                                      run_sandbox, cache_dir)
        except Exception as exc:                     # noqa: BLE001 - degrade, never crash
            log.warning("bridge: recompute failed for %s (%s); skipping", fid, exc)
            continue
        if not reconciles_pre_oos(series, stored[fid], oos_start):
            log.warning("bridge: %s pre-oos does not reconcile (non-causal or image "
                        "drift); skipping", fid)
            continue
        cutoff = pd.Timestamp(oos_start)
        if cutoff.tz is None:
            cutoff = cutoff.tz_localize(series.index.tz)
        oos_slice = series[series.index >= cutoff]
        if oos_slice.isna().all():
            log.warning("bridge: %s OOS window is entirely NaN; skipping", fid)
            continue
        cols[name] = series
        entries.append(card_to_entry(card, horizon_h=h))

    if not cols:
        return pd.DataFrame(index=panel.index).iloc[:, :0], []
    df = pd.DataFrame(cols, index=panel.index)
    # Index-alignment defence: a one-tick difference would misalign the whole
    # concat downstream and flood the panel with NaN.
    df = df.reindex(panel.index)
    assert len(df) == len(panel), "overlay lost rows against the production panel"
    return df, entries


def write_overlay(overlay_dir, symbol: str, df: pd.DataFrame, entries: list) -> None:
    """Materialise the per-run overlay. This is a CACHE for one pipeline run —
    it is NOT production. It exists because the stages are separate subprocesses
    and cannot share an in-memory frame."""
    d = Path(overlay_dir)
    d.mkdir(parents=True, exist_ok=True)
    sym = _symbol_short(symbol)
    df.to_parquet(d / f"foundry_overlay_{sym}.parquet")
    (d / f"foundry_manifest_{sym}.json").write_text(
        json.dumps({"factors": [e.model_dump(mode="json") for e in entries]}, indent=2),
        encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="foundry-bridge",
        description="Recompute Foundry candidates over the full span into a per-run overlay.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--overlay-dir", required=True, help="per-run cache dir (NOT production)")
    ap.add_argument("--image", required=True, help="sandbox image tag")
    ap.add_argument("--manifests-dir", default=None)
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--timeout-s", type=int, default=120)
    ap.add_argument("--horizon-h", type=int, default=24,
                    help="fallback horizon when a factor's code meta lacks one")
    args = ap.parse_args(argv)

    from research.hermes.foundry_runner import load_ohlcv
    from research.hermes.orchestrator import make_run_sandbox
    from research.hermes.sandbox import DockerSandbox, SandboxError
    from research.lib.factor_io import _default_manifests_dir, load_features
    from pipeline.config import load_config

    mdir = Path(args.manifests_dir) if args.manifests_dir else _default_manifests_dir()
    cfg = load_config()
    oos_start = cfg.oos_start

    features = load_features(args.symbol, manifests_dir=mdir)
    ohlcv = load_ohlcv(mdir / f"ohlcv_{_symbol_short(args.symbol)}.parquet")
    panel = features.join(ohlcv, how="left")

    try:
        sandbox = DockerSandbox(image=resolve_image_id(args.image),
                                timeout_s=args.timeout_s, allow_unpinned=False)
        run_sandbox = make_run_sandbox(sandbox, mdir / "_foundry_scratch")
        df, entries = build_overlay(args.symbol, mdir, panel, run_sandbox, oos_start,
                                    horizon_h=args.horizon_h, cap=args.cap)
    except SandboxError as exc:
        log.warning("bridge: sandbox unavailable (%s); writing empty overlay so the "
                    "pipeline still runs on library factors", exc)
        df, entries = pd.DataFrame(index=panel.index).iloc[:, :0], []
    write_overlay(args.overlay_dir, args.symbol, df, entries)
    print(f"foundry bridge: {len(entries)} candidate(s) -> {args.overlay_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
