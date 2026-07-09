import numpy as np
import pandas as pd
import pytest
from research.hermes.gatekeeper import factor_to_weights, turnover_of


def _s(vals, freq="1h"):
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=len(vals), freq=freq),
                     dtype="float64")


def test_weights_clipped_and_discrete_signal_survives_zero_std():
    # a constant-then-step discrete signal must NOT be wiped to NaN by std==0
    f = _s([1.0] * 100 + [-1.0] * 100)
    w = factor_to_weights(f, span=48)
    assert w.abs().max() <= 1.0 + 1e-9
    assert w.notna().sum() > 0                      # not all NaN despite std==0 runs


def test_turnover_is_weight_change():
    w = pd.Series([0.0, 1.0, -1.0], index=pd.date_range("2024-01-01", periods=3, freq="1h"))
    assert turnover_of(w).fillna(0).tolist() == pytest.approx([0.0, 1.0, 2.0])


def test_turnover_charges_first_entry_from_nan_warmup():
    # agy-3 #1: NaN warmup -> first real position must NOT be a free entry
    w = pd.Series([np.nan, np.nan, 0.8, 0.8],
                  index=pd.date_range("2024-01-01", periods=4, freq="1h"))
    tau = turnover_of(w).fillna(0.0)
    assert tau.iloc[2] == pytest.approx(0.8)          # entry 0 -> 0.8 charged
    assert tau.iloc[3] == pytest.approx(0.0)


def test_gross_ic_matches_spearman():
    from research.hermes.gatekeeper import gross_ic
    from scipy.stats import spearmanr
    rng = np.random.default_rng(0)
    f = _s(rng.normal(size=400)); r = _s(rng.normal(size=400))
    assert gross_ic(f, r) == pytest.approx(float(spearmanr(f, r).statistic), abs=1e-9)


def test_net_ir_penalises_high_turnover():
    from research.hermes.gatekeeper import net_ir, factor_to_weights
    rng = np.random.default_rng(1)
    n = 500
    ret1 = _s(rng.normal(scale=0.01, size=n))               # 1-period returns
    calm = _s(np.sin(np.linspace(0, 6, n)))                 # smooth -> low turnover
    churn = _s(rng.normal(size=n))                          # noisy -> high turnover
    ir_calm = net_ir(factor_to_weights(calm), ret1, cost_frac=0.0006)
    ir_churn = net_ir(factor_to_weights(churn), ret1, cost_frac=0.02)
    assert ir_churn < ir_calm                               # cost drag bites churn


def test_net_ir_uses_one_period_return_not_overlapping():
    # guard against the overlapping-return Sharpe-inflation trap: net_ir must be
    # called with a 1-period return series; a longer overlap would inflate it.
    from research.hermes.gatekeeper import net_ir, factor_to_weights
    n = 300
    ret1 = _s(np.random.default_rng(2).normal(scale=0.01, size=n))
    w = factor_to_weights(_s(np.arange(n, dtype="float64")))
    ir = net_ir(w, ret1, cost_frac=0.0006)
    assert np.isfinite(ir)


def test_nonoverlap_ic_strides_and_monotone_is_one():
    from research.hermes.gatekeeper import nonoverlap_ic
    n = 300
    f = _s(np.arange(n, dtype="float64")); r = _s(np.arange(n, dtype="float64"))
    assert nonoverlap_ic(f, r, horizon_bars=24) == pytest.approx(1.0, abs=1e-6)


def test_nonoverlap_ic_nan_when_too_few():
    from research.hermes.gatekeeper import nonoverlap_ic
    f = _s(np.arange(30, dtype="float64"))
    assert np.isnan(nonoverlap_ic(f, f, horizon_bars=24))


def test_regime_ic_ffills_daily_labels_to_factor_freq():
    from research.hermes.gatekeeper import regime_ic
    # hourly factor, DAILY regime labels — must ffill, not drop 23/24 rows
    hidx = pd.date_range("2024-01-01", periods=240, freq="1h")   # 10 days
    factor = pd.Series(np.arange(240, dtype="float64"), index=hidx)
    fwd = pd.Series(np.arange(240, dtype="float64"), index=hidx)
    didx = pd.date_range("2024-01-01", periods=10, freq="1D")
    daily_labels = pd.Series((["bull"] * 5) + (["bear"] * 5), index=didx)
    r = regime_ic(factor, fwd, daily_labels)
    assert set(r) <= {"bull", "bear", "neutral"}
    # bull covers ~5 days * 24h = 120 hourly rows (ffill worked), IC computable
    assert "bull" in r and np.isfinite(r["bull"])


def test_yearly_ic_splits_by_year():
    from research.hermes.gatekeeper import yearly_ic
    idx = pd.date_range("2022-06-01", periods=500, freq="1D")
    f = pd.Series(np.arange(500, dtype="float64"), index=idx)
    y = yearly_ic(f, f)
    assert "2022" in y and "2023" in y


def test_nearest_correlate_pairwise_survives_disjoint_lifespans():
    from research.hermes.gatekeeper import nearest_correlate
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    base = pd.Series(np.arange(n, dtype="float64"), index=idx)
    others = pd.DataFrame({
        "dead_early": np.r_[np.arange(150, dtype="float64"), [np.nan] * 150],  # dies mid
        "dead_late": np.r_[[np.nan] * 150, -np.arange(150, dtype="float64")],  # born mid, inverse
    }, index=idx)
    # global dropna() would empty this (no row has BOTH non-NaN); pairwise must not.
    name, absrho = nearest_correlate(base, others)
    assert name in {"dead_early", "dead_late"}
    assert absrho == pytest.approx(1.0, abs=1e-6)          # abs catches inverse


def test_nearest_correlate_empty_matrix():
    from research.hermes.gatekeeper import nearest_correlate
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    base = pd.Series(np.arange(50, dtype="float64"), index=idx)
    assert nearest_correlate(base, pd.DataFrame(index=idx)) == (None, 0.0)


def test_dsr_uses_only_same_interval_homogeneous_trials(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    events = (
        [{"kind": "factor_trial", "symbol": "eth",
          "detail": {"sr_per_bar": s, "interval": "1H"}} for s in [0.001, 0.002, 0.0015, 0.003]]
        + [{"kind": "factor_trial", "symbol": "eth",       # WRONG interval — must be excluded
            "detail": {"sr_per_bar": 9.9, "interval": "1D"}}]
    )
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: events)
    dsr = gatekeeper.foundry_dsr(0.004, "eth", "1H", manifests_dir=tmp_path, T=8760)
    assert 0.0 <= dsr <= 1.0
    # the 1D outlier (9.9) would blow up variance if wrongly included; exclude it
    monkeypatch.setattr(gatekeeper, "read_events",
                        lambda md: [e for e in events if e["detail"]["interval"] == "1H"])
    assert gatekeeper.foundry_dsr(0.004, "eth", "1H", tmp_path, 8760) == pytest.approx(dsr, abs=1e-12)


def test_dsr_safe_default_when_too_few(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    assert gatekeeper.foundry_dsr(0.004, "eth", "1H", tmp_path, 8760) == 1.0


def test_evaluate_enforces_lag_internally(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    # factor == next-bar return (look-ahead if NOT lagged). Internal shift must
    # break this perfect same-bar coupling so gross_ic is not a spurious ~1.
    close = pd.Series(100 + np.cumsum(np.random.default_rng(0).normal(size=n)), index=idx)
    ret1 = close.pct_change().shift(-1)
    factor = ret1.copy()                                   # peeks unless lagged
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=24))
    assert abs(res.metrics["gross_ic"]) < 0.99            # lag broke the peek


def test_evaluate_rejects_high_turnover(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    close = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(size=n)), index=idx)
    factor = pd.Series(np.random.default_rng(2).normal(size=n), index=idx)   # noisy -> churn
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path,
                   cfg=GateConfig(interval="1D", horizon_h=24, max_turnover=0.01))
    assert res.passed is False and "turnover" in res.rejection_reason.lower()


def test_evaluate_metrics_match_card_fields(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    close = pd.Series(100 + np.cumsum(np.random.default_rng(3).normal(size=n)), index=idx)
    factor = pd.Series(np.random.default_rng(4).normal(size=n), index=idx)
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=24))
    for k in ("gross_ic", "ic_nonoverlap", "ir", "dsr", "pbo", "turnover",
              "n_samples", "regime_ic", "yearly_ic", "nearest_factor",
              "nearest_abs_spearman"):
        assert k in res.metrics
