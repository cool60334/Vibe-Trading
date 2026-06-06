"""
Cross-regime factor IC analysis — task 2.3.

Loads each symbol's aligned factor dataframe (re-fetching live data via the
same research/lib/ helpers used by factor_extended.py), computes regime labels
via lib.regime.compute_regime, measures Spearman IC per factor per regime, and
enriches the existing research/manifests/factor_<symbol>.json manifest with:
  - cross_regime_ic  : {regime: IC}  per factor
  - stability        : regime_stable | conditional  per factor
  - verdict          : refined (conditional factors cannot be single_use)

Design decisions (task 2.3):
────────────────────────────
Stability rule:
    A factor is `regime_stable` if ALL of the following hold:
      1. IC signs are consistent across every regime that has a non-NaN IC
         (all ICs >= 0 or all ICs <= 0).
      2. At least 2 regimes have |IC| > 0.02  (non-trivial signal exists in
         multiple regimes; prevents near-zero-IC "stable" conclusions).
      3. max(|IC|) − min(|IC|) ≤ 0.15 among non-NaN, non-trivial regimes
         (consistent magnitude; a factor that is strong in one regime and weak
         in another is "regime-conditional", not "stable").
    Otherwise: conditional.

Verdict-refinement rule:
    If stability == conditional → single_use downgraded to ensemble_only.
    ensemble_only and reject are unchanged regardless of stability.

Regime computation:
    Uses compute_regime() from lib.regime with default EMA parameters.
    Daily close is derived from hourly candles via daily_close_from_hourly().
    Funding rate is used for the mania/capitulation override where available.
    Regime labels are joined back onto the hourly candle index (forward-fill).

The 4 pure-logic functions are importable and network-free (testable):
    compute_regime_ic, classify_stability, refine_verdict, enrich_manifest

main() is the thin orchestration shell (not tested; requires network).
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent          # research/
_REPO_ROOT = _THIS_DIR.parent                        # repo root
_RESEARCH_DIR = _REPO_ROOT / "research"
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"

for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

# ── Third-party ────────────────────────────────────────────────────────────────
import pandas as pd

# ── Internal imports ───────────────────────────────────────────────────────────
from lib.factor_metrics import add_forward_returns, compute_ic
from lib.regime import compute_regime, daily_close_from_hourly
from pipeline.config import load_config, ResearchConfig, SymbolConfig
from pipeline.config import _REPO_ROOT as _CFG_REPO_ROOT
from schemas import FactorEntry, FactorManifest, FactorStability, FactorVerdict


# ─── Stability classification threshold (documented rule) ─────────────────────

#: Minimum |IC| for a regime to count as "non-trivial".
_NONTRIVIAL_IC_THRESHOLD: float = 0.02

#: Minimum number of regimes with non-trivial IC needed for regime_stable.
_MIN_NONTRIVIAL_REGIMES: int = 2

#: Maximum allowed spread (max|IC| − min|IC|) among non-trivial regimes for regime_stable.
_MAX_SPREAD: float = 0.15


# ─── Pure-logic functions (testable) ──────────────────────────────────────────


def compute_regime_ic(
    factor: pd.Series,
    returns: pd.Series,
    regime_labels: pd.Series,
) -> dict[str, float]:
    """Compute Spearman IC per regime.

    Args:
        factor:        Factor values (hourly index).
        returns:       Forward returns (same index as factor).
        regime_labels: Regime label per bar (e.g. "bull"/"bear"/"neutral").

    Returns:
        Mapping of regime label -> Spearman IC.  Regimes with fewer than
        20 paired non-NaN observations return float("nan").
    """
    result: dict[str, float] = {}
    regimes_present = regime_labels.dropna().unique().tolist()
    for regime in regimes_present:
        mask = regime_labels == regime
        ic = compute_ic(factor[mask], returns[mask], method="spearman")
        result[regime] = ic
    return result


def classify_stability(cross_regime_ic: dict[str, float]) -> FactorStability:
    """Classify a factor as regime_stable or conditional.

    Rule (see module docstring for full rationale):
      regime_stable if ALL of:
        1. Consistent IC sign across non-NaN regimes (all >= 0 or all <= 0).
        2. At least _MIN_NONTRIVIAL_REGIMES regimes with |IC| > _NONTRIVIAL_IC_THRESHOLD.
        3. Spread (max|IC| − min|IC|) <= _MAX_SPREAD among non-NaN, non-trivial regimes.
      conditional otherwise.

    Args:
        cross_regime_ic: {regime_label: IC} where IC may be NaN.

    Returns:
        FactorStability.REGIME_STABLE or FactorStability.CONDITIONAL.
    """
    # Collect usable IC values (skip None and NaN — both mean "no signal here")
    valid_ic = {
        r: v
        for r, v in cross_regime_ic.items()
        if v is not None and not math.isnan(v)
    }

    if not valid_ic:
        return FactorStability.CONDITIONAL

    values = list(valid_ic.values())

    # Rule 1: consistent sign (allow zero to be either positive or negative)
    has_positive = any(v > 0 for v in values)
    has_negative = any(v < 0 for v in values)
    if has_positive and has_negative:
        return FactorStability.CONDITIONAL

    # Rule 2: at least _MIN_NONTRIVIAL_REGIMES have |IC| > threshold
    nontrivial = {r: v for r, v in valid_ic.items() if abs(v) > _NONTRIVIAL_IC_THRESHOLD}
    if len(nontrivial) < _MIN_NONTRIVIAL_REGIMES:
        return FactorStability.CONDITIONAL

    # Rule 3: spread among non-trivial regimes must be <= _MAX_SPREAD
    abs_values = [abs(v) for v in nontrivial.values()]
    spread = max(abs_values) - min(abs_values)
    if spread > _MAX_SPREAD:
        return FactorStability.CONDITIONAL

    return FactorStability.REGIME_STABLE


def refine_verdict(
    base_verdict: FactorVerdict,
    stability: FactorStability,
) -> FactorVerdict:
    """Refine verdict based on stability classification.

    Rule:
      - conditional + single_use  -> ensemble_only  (cannot be single_use)
      - conditional + ensemble_only/reject -> unchanged
      - regime_stable              -> unchanged

    Args:
        base_verdict: The |IC|-threshold verdict from task 2.2.
        stability:    Stability classification from classify_stability().

    Returns:
        Refined FactorVerdict.
    """
    if stability == FactorStability.CONDITIONAL and base_verdict == FactorVerdict.SINGLE_USE:
        return FactorVerdict.ENSEMBLE_ONLY
    return base_verdict


def enrich_manifest(
    manifest: FactorManifest,
    cross_regime_data: dict[str, dict[str, float]],
) -> FactorManifest:
    """Enrich a FactorManifest with cross-regime IC, stability, and refined verdicts.

    Only factors present in cross_regime_data are updated; others keep their
    existing (possibly null) cross_regime_ic and stability fields.

    Args:
        manifest:          Existing FactorManifest (typically loaded from JSON).
        cross_regime_data: {factor_name: {regime: IC}}.

    Returns:
        A NEW FactorManifest with enriched FactorEntry objects.
        The returned manifest validates against the schema.
    """
    enriched_entries: list[FactorEntry] = []
    for entry in manifest.factors:
        if entry.name not in cross_regime_data:
            enriched_entries.append(entry)
            continue

        regime_ic = cross_regime_data[entry.name]
        stability = classify_stability(regime_ic)
        refined = refine_verdict(entry.verdict, stability)

        enriched_entries.append(
            FactorEntry(
                name=entry.name,
                ic_by_horizon=entry.ic_by_horizon,
                ir=entry.ir,
                sample_size=entry.sample_size,
                cross_regime_ic=regime_ic,
                stability=stability,
                verdict=refined,
            )
        )

    return FactorManifest(
        schema_version=manifest.schema_version,
        symbol=manifest.symbol,
        generated_at=manifest.generated_at,
        period_days=manifest.period_days,
        horizons_h=manifest.horizons_h,
        factors=enriched_entries,
    )


# ─── Per-symbol orchestration ──────────────────────────────────────────────────


def _resolve_manifests_dir() -> Path:
    """Return research/manifests/ (same resolution as factor_extended.py)."""
    return _CFG_REPO_ROOT / "research" / "manifests"


def _load_factor_values(manifests_dir: Path, sym_name: str) -> pd.DataFrame | None:
    """Load the stored derived-factor values for a symbol, or None if missing.

    These parquets (``factor_values_<sym>.parquet``) hold the SAME factor
    columns the manifest reports (e.g. ``funding_z``, ``stablecoin_supply_z``),
    on the hourly index used for IC evaluation. Reusing them — rather than the
    old hardcoded ``funding_rate``/``oi_change_24h``/``fng`` recomputation —
    keeps cross-regime IC aligned with the factors the manifest actually scores.
    """
    path = manifests_dir / f"factor_values_{sym_name}.parquet"
    if not path.exists():
        print(f"  WARN: factor values parquet not found at {path}, skipping regime IC")
        return None
    fv = pd.read_parquet(path)
    return fv


def run_symbol_regime(sym: SymbolConfig, cfg: ResearchConfig, manifests_dir: Path) -> None:
    """Compute cross-regime IC for a symbol's STORED factors and enrich its manifest."""
    from lib.ccxt_data import fetch_funding_history_multiyear
    from lib.okx_data import fetch_candles

    period_days = cfg.period
    horizons = list(cfg.horizons_h)

    print(f"\n{'='*60}")
    print(f"[regime] Symbol: {sym.name.upper()}")
    print(f"{'='*60}")

    # ── Stored derived factors (the ones the manifest actually reports) ──────────
    print(f"[1/3] stored factor values (factor_values_{sym.name}.parquet)")
    fv = _load_factor_values(manifests_dir, sym.name)
    if fv is None or fv.empty:
        return
    print(f"     factors: {list(fv.columns)}  rows: {len(fv)}")

    # ── Close candles for forward returns + regime detection ─────────────────────
    print(f"[2/3] hourly candles (last {period_days}d)")
    candles = fetch_candles(sym.okx_swap, period_days, bar="1H", use_history_endpoint=True)
    print(f"     rows: {len(candles)}")

    # ── Funding for the regime mania/capitulation override (fail-soft) ───────────
    print(f"[3/3] funding history (last {period_days}d, ccxt multi-year)")
    try:
        funding = fetch_funding_history_multiyear(
            ccxt_symbol=sym.ccxt_bybit, days=period_days, exchange="binance",
            okx_swap=sym.okx_swap,
        )
        print(f"     rows: {len(funding)}")
    except Exception as exc:  # noqa: BLE001
        print(f"     WARN: funding fetch failed ({exc}); regime without funding override")
        funding = None

    # Build hourly frame on the stored-factor index; bring in close.
    df = fv.copy()
    df["close"] = candles["close"].reindex(df.index, method="ffill")

    fund_h = None
    if funding is not None and not funding.empty:
        fund_h = funding.reindex(df.index, method="ffill").bfill()

    df = add_forward_returns(df, "close", horizons)

    # Compute daily close for regime detection
    daily_close = daily_close_from_hourly(df, col="close")

    regime_df = compute_regime(
        daily_close,
        funding_rate=fund_h["funding_rate"] if fund_h is not None else None,
    )

    # Reindex regime labels back to hourly index (forward-fill daily -> hourly)
    hourly_regime = regime_df["regime"].reindex(df.index, method="ffill")

    print(f"\n[regime] label distribution: {dict(hourly_regime.value_counts())}")

    # Pick the longest forward-return horizon for cross-regime IC: stability is
    # about direction consistency, where the long horizon has the best
    # signal-to-noise (less microstructure noise).
    best_horizon = max(horizons)
    ret_col = f"ret_{best_horizon}h"

    cross_regime_data: dict[str, dict[str, float]] = {}
    for factor in fv.columns:
        if df[factor].isna().all():
            print(f"  {factor}: all NaN, skipping regime IC")
            continue
        if ret_col not in df.columns:
            print(f"  {factor}: return column {ret_col} missing, skipping")
            continue
        regime_ic = compute_regime_ic(df[factor], df[ret_col], hourly_regime)
        # NaN (sparse regime, <20 obs) -> None: NaN is invalid JSON and breaks
        # the dashboard's fetch().json(); the schema allows null per regime.
        regime_ic = {
            r: (None if (v is None or math.isnan(v)) else v)
            for r, v in regime_ic.items()
        }
        cross_regime_data[factor] = regime_ic
        print(f"  {factor:>20}: " + "  ".join(
            f"{r}={v:+.4f}" if v is not None else f"{r}=NaN"
            for r, v in regime_ic.items()
        ))

    # Load existing manifest
    json_path = manifests_dir / f"factor_{sym.name}.json"
    if not json_path.exists():
        print(f"  WARN: manifest not found at {json_path}, skipping.")
        return

    existing = FactorManifest.model_validate_json(json_path.read_text(encoding="utf-8"))

    # Enrich and write back
    enriched = enrich_manifest(existing, cross_regime_data)
    json_path.write_text(enriched.model_dump_json(indent=2), encoding="utf-8")
    print(f"\n[manifest] enriched -> {json_path}")

    # Print stability / verdict summary
    print("\nFactor stability summary:")
    for fe in enriched.factors:
        stab = fe.stability.value if fe.stability else "null"
        print(f"  {fe.name:>16}: stability={stab:>14}  verdict={fe.verdict.value}")


# ─── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    cfg = load_config()
    manifests_dir = _resolve_manifests_dir()

    print(f"[factor_regime] period={cfg.period}d  horizons={list(cfg.horizons_h)}  symbols={cfg.symbol_names()}")

    for sym in cfg.symbols:
        run_symbol_regime(sym, cfg, manifests_dir)

    print("\n[done] regime enrichment complete.")


if __name__ == "__main__":
    main()
