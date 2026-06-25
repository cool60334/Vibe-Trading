from __future__ import annotations

import pandas as pd

SCREEN_ZSCORE_DAYS = 30   # 30-day rolling window = 720 bars at 1H
SCREEN_MOM_HOURS   = 24   # momentum lookback in hours
OI_MOM_HOURS       = 72   # OI momentum lookback in hours


def _rolling_z(s: pd.Series, window_h: int) -> pd.Series:
    m = s.rolling(window_h, min_periods=window_h // 2).mean()
    sd = s.rolling(window_h, min_periods=window_h // 2).std()
    return (s - m) / (sd + 1e-9)


def basis_factors(perp_close: pd.Series, spot_close: pd.Series) -> dict[str, pd.Series]:
    spot = spot_close.reindex(perp_close.index, method="ffill")
    basis_rel = (perp_close - spot) / spot
    basis_z = _rolling_z(basis_rel, SCREEN_ZSCORE_DAYS * 24)
    basis_mom = basis_rel - basis_rel.shift(SCREEN_MOM_HOURS)
    return {"basis_rel": basis_rel, "basis_z": basis_z, "basis_mom": basis_mom}


def cross_venue_premium_factors(
    okx_spot_close: pd.Series,
    massive_usd_close: pd.Series,
    usdt_usd_rate: pd.Series,
    *,
    max_ffill_bars: int = 6,
    min_coverage: float = 0.5,
    max_abs_prem: float = 0.05,
    min_price_rel_std: float = 0.005,
) -> dict[str, pd.Series]:
    """Cross-venue premium factors from OKX USDT spot vs Massive USD spot.

    Splits the muddy spread into two deconfounded factors (design spec §3):
      depeg     = R - 1                       (pure USDT vs USD; category stablecoin)
      fiat_prem = okx*R / usd - 1             (OKX repriced to USD, depeg removed;
                                               pure offshore-vs-US fiat premium; basis)
    where R = usdt_usd_rate. All outputs are aligned to okx_spot_close.index.

    Data-quality guards (Phase-1 artifact fix). The cross-venue spread is only
    meaningful when both legs have real, aligned, sane data; otherwise a factor
    here is a disguised price level that shows spurious IC:
      * ``max_ffill_bars`` — limit forward-fill so a few stale USD bars are not
        stretched into a flat line (the original 0.3-0.56 IC bug).
      * ``max_abs_prem`` — an arbitrage-bound spot premium is ~bps; values beyond
        this are misalignment, not signal, and are dropped.
      * ``min_coverage`` — if a factor's real (non-NaN) coverage over the index
        falls below this fraction, it is refused (all-NaN) rather than emitted.
      * ``min_price_rel_std`` — the USD price leg must actually move; a frozen /
        flatlined feed (rel std ~0, e.g. a repeated value over continuous
        timestamps) makes fiat_prem a disguised price level, so it is refused.
        Applies only to fiat_prem's price leg, never to depeg (USDT/USD is
        legitimately near-constant for long stretches).
    Refused factors are all-NaN, so downstream screening simply skips them.
    """
    idx = okx_spot_close.index
    nan = pd.Series(float("nan"), index=idx)

    usd = massive_usd_close.reindex(idx, method="ffill", limit=max_ffill_bars)
    r = usdt_usd_rate.reindex(idx, method="ffill", limit=max_ffill_bars)

    depeg = r - 1.0
    fiat_prem = (okx_spot_close * r) / usd - 1.0
    fiat_prem = fiat_prem.where(fiat_prem.abs() <= max_abs_prem)

    usd_cov = usd.dropna()
    price_frozen = (
        usd_cov.empty
        or usd_cov.mean() == 0
        or (usd_cov.std() / abs(usd_cov.mean())) < min_price_rel_std
    )

    def _guard(series: pd.Series, ok: bool = True) -> tuple[pd.Series, pd.Series]:
        if not ok or series.notna().mean() < min_coverage:
            return nan, nan
        return series, _rolling_z(series, SCREEN_ZSCORE_DAYS * 24)

    depeg_lvl, depeg_z = _guard(depeg)
    fiat_lvl, fiat_z = _guard(fiat_prem, ok=not price_frozen)
    return {
        "depeg": depeg_lvl,
        "depeg_z": depeg_z,
        "fiat_prem": fiat_lvl,
        "fiat_prem_z": fiat_z,
    }


def funding_factors(funding_on_candle: pd.Series) -> dict[str, pd.Series]:
    funding_z = _rolling_z(funding_on_candle, SCREEN_ZSCORE_DAYS * 24)
    funding_mom = funding_on_candle - funding_on_candle.shift(SCREEN_MOM_HOURS)
    return {"funding_z": funding_z, "funding_mom": funding_mom}


def oi_factors(oi_on_candle: pd.Series, close: pd.Series) -> dict[str, pd.Series]:
    oi_z = _rolling_z(oi_on_candle, SCREEN_ZSCORE_DAYS * 24)
    oi_price_divergence = oi_on_candle.pct_change(24) * close.pct_change(24)
    oi_mom = oi_on_candle.pct_change(OI_MOM_HOURS)
    return {"oi_z": oi_z, "oi_price_divergence": oi_price_divergence, "oi_mom": oi_mom}


def positioning_factors(oi: pd.DataFrame, candle_idx: pd.Index) -> dict[str, pd.Series]:
    """Long/short positioning factors from the Binance OI/L-S archive columns.

    Each ratio is 30-day rolling z-scored on its native 1H index, then reindexed
    to the candle grid with NO ffill — gap hours stay NaN so screening IC isn't
    inflated. ``ls_divergence`` = retail z − smart-money z (rank-2 with the two
    z's; downstream ensembles must not combine all three — see spec).
    """
    out: dict[str, pd.Series] = {}
    if "global_ls_accounts" in oi.columns:
        out["global_ls_acct_z"] = _rolling_z(
            oi["global_ls_accounts"], SCREEN_ZSCORE_DAYS * 24
        ).reindex(candle_idx)
    # toptrader_ls_accounts (by account count) is excluded — spec uses the
    # position-weighted ratio (toptrader_ls_positions) which better reflects PnL.
    if "toptrader_ls_positions" in oi.columns:
        out["toptrader_ls_z"] = _rolling_z(
            oi["toptrader_ls_positions"], SCREEN_ZSCORE_DAYS * 24
        ).reindex(candle_idx)
    if "global_ls_acct_z" in out and "toptrader_ls_z" in out:
        out["ls_divergence"] = out["global_ls_acct_z"] - out["toptrader_ls_z"]
    return out
