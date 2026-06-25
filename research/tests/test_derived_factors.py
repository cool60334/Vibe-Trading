import sys
from pathlib import Path
_RESEARCH_DIR = Path(__file__).resolve().parents[1]  # research/
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

import numpy as np
import pandas as pd
import pytest

from lib.derived_factors import (
    basis_factors,
    funding_factors,
    oi_factors,
    SCREEN_MOM_HOURS,
    OI_MOM_HOURS,
    SCREEN_ZSCORE_DAYS,
)

N = 800  # enough bars to exceed the 720-bar rolling window


def _index(n: int = N) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="1h")


# ─── basis_factors ────────────────────────────────────────────────────────────

def test_basis_rel_exact_values():
    idx = _index()
    perp = pd.Series(np.linspace(100, 110, N), index=idx)
    spot = pd.Series(np.linspace(99, 109, N), index=idx)
    out = basis_factors(perp, spot)
    expected = (perp - spot) / spot
    pd.testing.assert_series_equal(out["basis_rel"], expected, check_names=False)


def test_basis_mom_24bar_diff():
    idx = _index()
    perp = pd.Series(np.linspace(100, 110, N), index=idx)
    spot = pd.Series(np.linspace(99, 109, N), index=idx)
    out = basis_factors(perp, spot)
    basis_rel = out["basis_rel"]
    i = 100
    expected_mom_at_i = basis_rel.iloc[i] - basis_rel.iloc[i - SCREEN_MOM_HOURS]
    assert abs(out["basis_mom"].iloc[i] - expected_mom_at_i) < 1e-12


def test_basis_output_index_matches_input():
    idx = _index()
    perp = pd.Series(np.ones(N), index=idx)
    spot = pd.Series(np.ones(N), index=idx)
    out = basis_factors(perp, spot)
    for key in ("basis_rel", "basis_z", "basis_mom"):
        assert out[key].index.equals(idx), f"{key} index mismatch"


def test_basis_no_lookahead():
    idx = _index()
    perp = pd.Series(np.random.default_rng(0).random(N) + 100, index=idx)
    spot = pd.Series(np.random.default_rng(1).random(N) + 99, index=idx)
    full = basis_factors(perp, spot)
    truncated = basis_factors(perp.iloc[:-1], spot.iloc[:-1])
    # Last common index point must agree
    for key in ("basis_rel", "basis_z", "basis_mom"):
        v_full = full[key].iloc[-2]
        v_trunc = truncated[key].iloc[-1]
        assert abs(v_full - v_trunc) < 1e-12 or (np.isnan(v_full) and np.isnan(v_trunc)), \
            f"look-ahead detected in {key}"


def test_basis_spot_reindex_ffill():
    # spot has fewer bars — should be forward-filled onto perp index
    perp_idx = _index()
    spot_idx = perp_idx[::2]  # every other hour
    perp = pd.Series(np.linspace(100, 110, N), index=perp_idx)
    spot = pd.Series(np.linspace(99, 104, len(spot_idx)), index=spot_idx)
    out = basis_factors(perp, spot)
    assert out["basis_rel"].index.equals(perp_idx)


# ─── funding_factors ──────────────────────────────────────────────────────────

def test_funding_index_matches():
    idx = _index()
    funding = pd.Series(np.random.default_rng(2).random(N) * 0.001, index=idx)
    out = funding_factors(funding)
    for key in ("funding_z", "funding_mom"):
        assert out[key].index.equals(idx), f"{key} index mismatch"


def test_funding_mom_24bar_diff():
    idx = _index()
    funding = pd.Series(np.linspace(0.0001, 0.0003, N), index=idx)
    out = funding_factors(funding)
    i = 100
    expected = funding.iloc[i] - funding.iloc[i - SCREEN_MOM_HOURS]
    assert abs(out["funding_mom"].iloc[i] - expected) < 1e-15


def test_funding_values_finite():
    idx = _index()
    funding = pd.Series(np.random.default_rng(3).normal(0, 0.0001, N), index=idx)
    out = funding_factors(funding)
    # After warm-up, z-scores should be finite
    assert np.all(np.isfinite(out["funding_z"].iloc[SCREEN_ZSCORE_DAYS * 24 :]))


def test_funding_no_lookahead():
    idx = _index()
    funding = pd.Series(np.random.default_rng(4).random(N), index=idx)
    full = funding_factors(funding)
    trunc = funding_factors(funding.iloc[:-1])
    for key in ("funding_z", "funding_mom"):
        v_full = full[key].iloc[-2]
        v_trunc = trunc[key].iloc[-1]
        assert abs(v_full - v_trunc) < 1e-12 or (np.isnan(v_full) and np.isnan(v_trunc)), \
            f"look-ahead detected in {key}"


# ─── oi_factors ───────────────────────────────────────────────────────────────

def test_oi_divergence_positive_same_direction():
    idx = _index()
    # Step up at bar 25; pct_change(24) at bar 48 compares iloc[48]=1100 to iloc[24]=1000 → +10%
    oi = pd.Series(np.ones(N) * 1000.0, index=idx)
    close = pd.Series(np.ones(N) * 50000.0, index=idx)
    oi.iloc[25:] = 1100.0
    close.iloc[25:] = 55000.0
    out = oi_factors(oi, close)
    assert out["oi_price_divergence"].iloc[48] > 0


def test_oi_divergence_negative_opposite_direction():
    idx = _index()
    # OI rises, price falls — product must be negative
    oi = pd.Series(np.ones(N) * 1000.0, index=idx)
    close = pd.Series(np.ones(N) * 50000.0, index=idx)
    oi.iloc[25:] = 1100.0
    close.iloc[25:] = 45000.0
    out = oi_factors(oi, close)
    assert out["oi_price_divergence"].iloc[48] < 0


def test_oi_mom_72bar_pct_change():
    idx = _index()
    oi = pd.Series(np.linspace(1000, 2000, N), index=idx)
    close = pd.Series(np.ones(N) * 50000.0, index=idx)
    out = oi_factors(oi, close)
    i = 200
    expected = oi.pct_change(OI_MOM_HOURS).iloc[i]
    assert abs(out["oi_mom"].iloc[i] - expected) < 1e-12


def test_oi_output_index_matches():
    idx = _index()
    oi = pd.Series(np.ones(N) * 1000.0, index=idx)
    close = pd.Series(np.ones(N) * 50000.0, index=idx)
    out = oi_factors(oi, close)
    for key in ("oi_z", "oi_price_divergence", "oi_mom"):
        assert out[key].index.equals(idx), f"{key} index mismatch"


def test_oi_no_lookahead():
    rng = np.random.default_rng(5)
    idx = _index()
    oi = pd.Series(rng.random(N) * 1000 + 500, index=idx)
    close = pd.Series(rng.random(N) * 10000 + 40000, index=idx)
    full = oi_factors(oi, close)
    trunc = oi_factors(oi.iloc[:-1], close.iloc[:-1])
    for key in ("oi_z", "oi_price_divergence", "oi_mom"):
        v_full = full[key].iloc[-2]
        v_trunc = trunc[key].iloc[-1]
        assert abs(v_full - v_trunc) < 1e-12 or (np.isnan(v_full) and np.isnan(v_trunc)), \
            f"look-ahead detected in {key}"


# ─── positioning_factors ──────────────────────────────────────────────────────

from lib.derived_factors import positioning_factors


def _oi_frame(n=800):
    idx = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "global_ls_accounts": np.linspace(1.0, 2.0, n),
            "toptrader_ls_positions": np.linspace(2.0, 1.0, n),
        },
        index=idx,
    )


def test_positioning_factors_emits_three_columns():
    oi = _oi_frame()
    out = positioning_factors(oi, oi.index)
    assert set(out) == {"global_ls_acct_z", "toptrader_ls_z", "ls_divergence"}


def test_positioning_factors_divergence_is_difference_of_zs():
    oi = _oi_frame()
    out = positioning_factors(oi, oi.index)
    expected = out["global_ls_acct_z"] - out["toptrader_ls_z"]
    pd.testing.assert_series_equal(out["ls_divergence"], expected, check_names=False)


def test_positioning_factors_missing_column_guard():
    oi = _oi_frame()[["global_ls_accounts"]]  # no toptrader column
    out = positioning_factors(oi, oi.index)
    assert set(out) == {"global_ls_acct_z"}  # no toptrader, hence no divergence


def test_positioning_factors_reindex_no_ffill():
    oi = _oi_frame()
    # candle grid extends 24h past the OI data → those hours must stay NaN
    candle_idx = pd.date_range("2022-01-01", periods=824, freq="h", tz="UTC")
    out = positioning_factors(oi, candle_idx)
    assert out["global_ls_acct_z"].iloc[800:].isna().all()


# ─── cross_venue_premium_factors ──────────────────────────────────────────────

from lib.derived_factors import cross_venue_premium_factors


def _idx(n):
    return pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")


def test_cross_venue_premium_keys_and_shape():
    idx = _idx(800)
    okx = pd.Series(100.0, index=idx)
    usd = pd.Series(100.0, index=idx)
    r = pd.Series(1.0, index=idx)
    out = cross_venue_premium_factors(okx, usd, r)
    assert set(out) == {"depeg", "depeg_z", "fiat_prem", "fiat_prem_z"}
    for v in out.values():
        assert len(v) == len(idx)


def test_depeg_isolated_from_price():
    """A pure USDT discount (R<1) with equal USD/OKX prices => depeg negative,
    fiat_prem ~0 after deconfounding."""
    idx = _idx(800)
    okx = pd.Series(100.0, index=idx)   # USDT-denominated
    usd = pd.Series(100.0, index=idx)   # real USD
    r = pd.Series(0.99, index=idx)      # 1 USDT = 0.99 USD (discount)
    out = cross_venue_premium_factors(okx, usd, r)
    assert (out["depeg"] < 0).all()
    # fiat_prem = okx*R/usd - 1 = 100*0.99/100 - 1 = -0.01  (NOT ~0 here)
    assert np.allclose(out["fiat_prem"].dropna(), -0.01, atol=1e-9)


def test_fiat_premium_pure_when_no_depeg():
    """R==1 (no depeg): fiat_prem reduces to OKX-vs-USD spread."""
    idx = _idx(800)
    okx = pd.Series(101.0, index=idx)
    usd = pd.Series(100.0, index=idx)
    r = pd.Series(1.0, index=idx)
    out = cross_venue_premium_factors(okx, usd, r)
    assert np.allclose(out["fiat_prem"].dropna(), 0.01, atol=1e-9)
    assert np.allclose(out["depeg"].dropna(), 0.0, atol=1e-9)


def test_reindex_aligns_to_okx_index():
    idx = _idx(800)
    okx = pd.Series(100.0, index=idx)
    usd = pd.Series(100.0, index=idx[::2])   # sparser
    r = pd.Series(1.0, index=idx[::3])
    out = cross_venue_premium_factors(okx, usd, r)
    assert (out["fiat_prem"].index == idx).all()


def test_truncation_stability():
    idx = _idx(800)
    okx = pd.Series(np.linspace(100, 110, 800), index=idx)
    usd = pd.Series(np.linspace(100, 109, 800), index=idx)
    r = pd.Series(1.0, index=idx)
    full = cross_venue_premium_factors(okx, usd, r)
    trunc = cross_venue_premium_factors(okx.iloc[:-1], usd.iloc[:-1], r.iloc[:-1])
    # earlier values must not change when one bar is appended
    assert np.allclose(
        full["fiat_prem"].iloc[:-1].dropna().values,
        trunc["fiat_prem"].dropna().values,
        atol=1e-12,
    )


# ─── cross_venue_premium_factors: data-quality guards (Phase-1 artifact fix) ───
# Root cause of the spurious 0.3-0.56 IC: Massive free tier returned only a tiny
# tail of the requested range, and unlimited reindex(ffill) stretched a few stale
# bars into a flat line => fiat_prem became okx/const-1 = a disguised price level
# => z-score correlated with forward returns. The factor must REFUSE such data.


def test_partial_coverage_massive_refuses_factor():
    """Massive covering only a small tail of the index (e.g. free-tier 30d of a
    multi-year backtest) must NOT yield a factor. Isolates the COVERAGE guard:
    the covered region tracks OKX (bps premium, in-bound) so only thin coverage
    can trigger the refusal."""
    idx = _idx(4000)
    okx = pd.Series(np.linspace(100.0, 60.0, 4000), index=idx)  # real declining price
    massive = pd.Series(np.nan, index=idx)
    massive.iloc[-800:] = okx.iloc[-800:].values * 1.0005       # only last 20% covered
    r = pd.Series(1.0, index=idx)
    out = cross_venue_premium_factors(okx, massive, r)
    assert out["fiat_prem_z"].notna().sum() == 0
    assert out["fiat_prem"].notna().sum() == 0


def test_partial_coverage_rate_refuses_depeg():
    """USDT/USD rate covering only a small tail must refuse depeg too."""
    idx = _idx(4000)
    okx = pd.Series(100.0, index=idx)
    massive = pd.Series(100.0, index=idx)
    r = pd.Series(np.nan, index=idx)
    r.iloc[-800:] = 1.001                                       # only last 20% covered
    out = cross_venue_premium_factors(okx, massive, r)
    assert out["depeg_z"].notna().sum() == 0
    assert out["depeg"].notna().sum() == 0


def test_out_of_bound_fiat_prem_refused():
    """A 40% cross-venue 'premium' is impossible (arbitrage bounds it to bps) =>
    misalignment => refused, not emitted as a constant. Isolates the SANITY
    bound: coverage is full, only the magnitude is wrong."""
    idx = _idx(4000)
    okx = pd.Series(60.0, index=idx)
    massive = pd.Series(100.0, index=idx)                        # 40% gap
    r = pd.Series(1.0, index=idx)
    out = cross_venue_premium_factors(okx, massive, r)
    assert out["fiat_prem_z"].notna().sum() == 0


def test_well_aligned_data_still_produces_factor():
    """Regression: full-coverage, bps-scale premium must STILL yield a factor."""
    idx = _idx(4000)
    rng = np.random.default_rng(0)
    price = pd.Series(100 + np.cumsum(rng.normal(0, 0.5, 4000)), index=idx)
    massive = price * (1 + rng.normal(0, 0.0005, 4000))         # ~5bps noise, full coverage
    r = pd.Series(1.0 + rng.normal(0, 0.0002, 4000), index=idx)
    out = cross_venue_premium_factors(price, massive, r)
    assert out["fiat_prem_z"].notna().sum() > 1000
    assert out["fiat_prem"].abs().max() < 0.05


# ─── massive_spot source registry ──────────────────────────────────────────────

def test_massive_source_registered():
    from lib.sources import SOURCE_REGISTRY
    spec = SOURCE_REGISTRY["massive_spot_usd"]
    assert spec.status == "available"
    assert spec.category in {"basis", "stablecoin"}
