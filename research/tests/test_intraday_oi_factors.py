import numpy as np
import pandas as pd

from lib import intraday_oi_factors as iof


def _oi_frame(n, freq="30min"):
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"oi": np.linspace(100, 200, n),
         "global_ls_accounts": np.linspace(1.0, 2.0, n),
         "toptrader_ls_positions": np.linspace(2.0, 1.0, n)},
        index=idx,
    )


def test_ls_factors_window_scales_with_interval():
    df = _oi_frame(200, "30min")
    f30 = iof.ls_factors(df, "30m", window_h=10)   # 10h -> 20 bars at 30m
    assert "global_ls_z_s" in f30 and "toptrader_ls_z_s" in f30 and "ls_divergence_s" in f30
    # divergence is exactly the difference of the two z's
    pd.testing.assert_series_equal(
        f30["ls_divergence_s"], (f30["global_ls_z_s"] - f30["toptrader_ls_z_s"]),
        check_names=False,
    )
    # window scaled: at 15m the same 10h is 40 bars, so early NaNs differ
    f15 = iof.ls_factors(_oi_frame(200, "15min"), "15m", window_h=10)
    assert f15["global_ls_z_s"].isna().sum() > f30["global_ls_z_s"].isna().sum()


def test_oi_velocity_factors_present_and_interval_scaled():
    df = _oi_frame(100, "30min")
    close = pd.Series(np.linspace(10, 11, 100), index=df.index)
    f = iof.oi_velocity_factors(df, close, "30m")
    assert set(f) == {"oi_mom_1h", "oi_accel", "oi_price_div"}
    # oi_mom_1h at 30m is pct_change over 2 bars
    pd.testing.assert_series_equal(f["oi_mom_1h"], df["oi"].pct_change(2), check_names=False)


def test_causal_1h_control_has_no_lookahead():
    # 1H factor: value at hour H is known only after H closes (at H+1h)
    h_idx = pd.date_range("2024-01-01 00:00", periods=4, freq="1h", tz="UTC")
    factor_1h = pd.Series([10.0, 20.0, 30.0, 40.0], index=h_idx)
    intraday_idx = pd.date_range("2024-01-01 00:00", periods=8, freq="30min", tz="UTC")
    ctrl = iof.causal_1h_control(factor_1h, intraday_idx)
    # at 00:00 and 00:30 (hour 0 not yet closed) -> NaN (no known 1H value)
    assert np.isnan(ctrl.loc["2024-01-01 00:00"])
    assert np.isnan(ctrl.loc["2024-01-01 00:30"])
    # at 01:00 (hour 0 just closed) -> hour-0 value
    assert ctrl.loc["2024-01-01 01:00"] == 10.0
    assert ctrl.loc["2024-01-01 01:30"] == 10.0
    # at 02:00 -> hour-1 value
    assert ctrl.loc["2024-01-01 02:00"] == 20.0


from lib import intraday_oi_recon as recon


def test_classify_nogo_when_ic_below_floor():
    v, reasons = recon.classify_factor(decay={4: 0.01, 8: 0.02}, incr_vs_1h=0.05, peak_h=2.0)
    assert v == "NO_GO" and any("IC" in r for r in reasons)


def test_classify_nogo_when_no_incremental_over_1h():
    # strong raw IC but ~0 incremental vs its own 1H => intraday adds nothing
    v, reasons = recon.classify_factor(decay={2: 0.08}, incr_vs_1h=0.004, peak_h=2.0)
    assert v == "NO_GO" and any("1H" in r for r in reasons)


def test_classify_nogo_when_peak_is_slow():
    # edge only at 72h => it's the slow signal in disguise, not intraday
    v, reasons = recon.classify_factor(decay={144: 0.06}, incr_vs_1h=0.05, peak_h=72.0)
    assert v == "NO_GO" and any("slow" in r or "peak" in r for r in reasons)


def test_classify_go_when_intraday_incremental_and_fast():
    v, reasons = recon.classify_factor(decay={2: 0.06}, incr_vs_1h=0.045, peak_h=2.0)
    assert v == "GO"
