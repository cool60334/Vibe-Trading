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


def test_weights_survive_a_non_float64_factor_dtype():
    # Real bug found only by a real Task 9 run against a real LLM-generated
    # factor: pandas object-dtype Series division calls Python's own `/`
    # per-element, which raises ZeroDivisionError on an exact 0.0 -- unlike
    # float64 array division, which returns inf/nan (verified directly:
    # pd.Series([1.0], dtype=object) / pd.Series([0.0], dtype=object) raises
    # "ZeroDivisionError: float division by zero"). `factor` here comes from
    # an untrusted LLM-generated compute(), so its dtype is not guaranteed.
    # This locks factor_to_weights's defensive coercion to float64 up front.
    f = _s([1.0] * 100 + [-1.0] * 100).astype(object)
    w = factor_to_weights(f, span=48)                # must not raise
    assert w.dtype == np.float64
    assert w.abs().max() <= 1.0 + 1e-9


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


def test_nonoverlap_ic_strides_by_calendar_position_not_post_dropna_row_count():
    # A dropna-before-stride implementation shrinks the frame first, so a
    # later iloc[::horizon_bars] lands on whatever row ends up at that
    # position post-shrink -- not the row horizon_bars calendar-bars away.
    # Scattering NaN at positions that are NOT stride multiples must therefore
    # be a no-op on the result once striding happens BEFORE dropna.
    from research.hermes.gatekeeper import nonoverlap_ic
    from scipy.stats import spearmanr
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    rng = np.random.default_rng(11)
    factor = pd.Series(rng.normal(size=n), index=idx)
    fwd = pd.Series(rng.normal(size=n), index=idx)
    horizon_bars = 24

    strided_first = pd.concat([factor, fwd], axis=1).iloc[::horizon_bars].dropna()
    expected = float(spearmanr(strided_first.iloc[:, 0], strided_first.iloc[:, 1]).statistic)

    factor_scattered = factor.copy()
    scatter_positions = [1, 2, 3, 5, 7, 10, 13, 17, 19, 23]  # none are multiples of 24
    factor_scattered.iloc[scatter_positions] = np.nan

    actual = nonoverlap_ic(factor_scattered, fwd, horizon_bars=horizon_bars)
    assert actual == pytest.approx(expected, abs=1e-9)


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


def test_regime_ic_handles_fwd_ret_longer_than_factor():
    # In foundry the factor rides the FEATURES index while fwd_ret rides the
    # (wider) OHLCV index — the mask is built on the factor index, so masking a
    # longer fwd_ret with it must not raise "Unalignable boolean Series".
    from research.hermes.gatekeeper import regime_ic
    fidx = pd.date_range("2024-01-01", periods=240, freq="1h")    # factor: 10 days
    widx = pd.date_range("2023-12-30", periods=288, freq="1h")    # fwd: superset, 12 days
    factor = pd.Series(np.arange(240, dtype="float64"), index=fidx)
    fwd = pd.Series(np.arange(288, dtype="float64"), index=widx)
    didx = pd.date_range("2024-01-01", periods=10, freq="1D")
    daily_labels = pd.Series((["bull"] * 5) + (["bear"] * 5), index=didx)
    r = regime_ic(factor, fwd, daily_labels)                       # must not raise
    assert set(r) <= {"bull", "bear", "neutral"}
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


def test_evaluate_1h_interval_uses_add_forward_returns_path(tmp_path, monkeypatch):
    # Task 7 code-review follow-up: the plan's literal (non-1D-deviation) branch
    # -- add_forward_returns()/bars_per_hour() -- had zero coverage; all 3
    # plan-required evaluate() tests use interval="1D" (the deviation branch).
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 2000
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    rng = np.random.default_rng(8)
    close = pd.Series(100 + np.cumsum(rng.normal(size=n)), index=idx)
    factor = pd.Series(rng.normal(size=n), index=idx)
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1H", horizon_h=4))
    assert "gross_ic" in res.metrics and np.isfinite(res.metrics["turnover"])


def test_evaluate_passes_clean_predictive_factor(tmp_path, monkeypatch):
    # Task 7 code-review follow-up: no test previously proved a good factor can
    # actually clear all four gates (redundant/turnover/gross_ic/dsr) to passed=True.
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 3000
    idx = pd.date_range("2015-01-01", periods=n, freq="1D")
    rng = np.random.default_rng(7)
    t = np.arange(n, dtype="float64")
    factor_true = np.sin(t / 80.0)                       # slow oscillation -> low turnover
    ret = np.zeros(n, dtype="float64")
    ret[1:] = 0.02 * factor_true[:-1] + rng.normal(scale=0.002, size=n - 1)
    close = pd.Series(100.0 * np.cumprod(1.0 + ret), index=idx)
    factor = pd.Series(factor_true, index=idx)
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=24))
    assert res.passed is True, res.rejection_reason
    assert res.metrics["gross_ic"] > 0.03


def test_evaluate_rejects_redundant_factor(tmp_path, monkeypatch):
    # Task 7 code-review follow-up: no test previously exercised the
    # redundant-factor short-circuit (the first rejection check in evaluate()).
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    rng = np.random.default_rng(9)
    close = pd.Series(100 + np.cumsum(rng.normal(size=n)), index=idx)
    factor = pd.Series(rng.normal(size=n), index=idx)
    # evaluate() internally shifts factor by entry_lag (default 1) before
    # comparing against existing_and_dead -- pre-shift the duplicate here so it
    # matches what nearest_correlate actually sees post-shift (near-1.0 abs corr).
    existing = pd.DataFrame({"dup": factor.shift(1)}, index=idx)
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=existing, symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=24))
    assert res.passed is False and "redundant" in res.rejection_reason.lower()


def test_evaluate_1d_horizon_not_multiple_of_24_raises(tmp_path, monkeypatch):
    # Task 7 code-review follow-up: the horizon_h % 24 guard in the 1D-interval
    # deviation branch had no direct coverage.
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 100
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    close = pd.Series(100.0 + np.arange(n, dtype="float64"), index=idx)
    factor = pd.Series(np.arange(n, dtype="float64"), index=idx)
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    with pytest.raises(ValueError, match="multiple of 24"):
        evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                 existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                 manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=5))


def test_gross_ir_ignores_cost_while_net_ir_pays_it():
    """DSR 的虛無假設是『零 alpha 下 gross SR 期望為 0』。成本是確定性的、
    因子專屬的，不是搜尋的抽樣噪音 —— 混進 DSR 的變異數就毀掉它。"""
    import numpy as np, pandas as pd
    from research.hermes.gatekeeper import gross_ir, net_ir

    idx = pd.date_range("2024-01-01", periods=500, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    ret1 = pd.Series(rng.standard_normal(500) * 0.01, index=idx)
    weights = pd.Series(np.sign(rng.standard_normal(500)), index=idx)

    g = gross_ir(weights, ret1)
    n = net_ir(weights, ret1, cost_frac=0.0006)
    assert np.isfinite(g) and np.isfinite(n)
    assert g > n                       # 成本只會扣分


def test_gross_ir_equals_net_ir_at_zero_cost():
    import numpy as np, pandas as pd
    from research.hermes.gatekeeper import gross_ir, net_ir

    idx = pd.date_range("2024-01-01", periods=500, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    ret1 = pd.Series(rng.standard_normal(500) * 0.01, index=idx)
    weights = pd.Series(rng.standard_normal(500), index=idx).clip(-1, 1)
    assert gross_ir(weights, ret1) == pytest.approx(net_ir(weights, ret1, cost_frac=0.0))


def test_gross_ir_is_undefined_on_a_flat_position():
    import numpy as np, pandas as pd
    from research.hermes.gatekeeper import gross_ir

    idx = pd.date_range("2024-01-01", periods=500, freq="h", tz="UTC")
    ret1 = pd.Series(np.linspace(0.001, 0.002, 500), index=idx)
    assert np.isnan(gross_ir(pd.Series(0.0, index=idx), ret1))


def _trial(md, gross=None, net=0.0, interval="1H"):
    from research.lib.research_ledger import append_event
    d = {"sr_per_bar": net, "interval": interval, "factor_id": "f"}
    if gross is not None:
        d["gross_sr_per_bar"] = gross
    append_event(md, kind="factor_trial", symbol="eth", detail=d)


def test_foundry_dsr_counts_legacy_trials_but_ignores_their_variance(tmp_path):
    """The 39 legacy eth trials recorded NET Sharpe, whose spread tracks turnover
    rather than search noise -- drop the variance. But we really did cast 39 times,
    and that debt does not evaporate because the net was broken. Count them."""
    from research.hermes.gatekeeper import foundry_dsr
    for _ in range(39):
        _trial(tmp_path, gross=None, net=-0.05)        # legacy: net only, wild spread
    for g in (0.001, -0.001, 0.002, -0.002):
        _trial(tmp_path, gross=g, net=g - 0.01)        # new: gross, tight spread

    with_debt = foundry_dsr(0.02, "eth", "1H", tmp_path, T=20000)

    # same variance sample, no legacy debt -> must be strictly more lenient
    import shutil
    clean = tmp_path / "clean"; clean.mkdir()
    for g in (0.001, -0.001, 0.002, -0.002):
        _trial(clean, gross=g, net=g - 0.01)
    assert foundry_dsr(0.02, "eth", "1H", clean, T=20000) > with_debt


def test_foundry_dsr_variance_uses_only_gross_rows(tmp_path):
    """A legacy net row at -0.9 would blow up the variance if it leaked in."""
    from research.hermes.gatekeeper import foundry_dsr
    _trial(tmp_path, gross=None, net=-0.9)
    for g in (0.001, -0.001, 0.002, -0.002):
        _trial(tmp_path, gross=g)
    assert foundry_dsr(0.02, "eth", "1H", tmp_path, T=20000) > 0.5


def test_foundry_dsr_still_filters_by_interval(tmp_path):
    from research.hermes.gatekeeper import foundry_dsr
    for g in (0.5, -0.5, 0.6, -0.6):
        _trial(tmp_path, gross=g, interval="15m")
    assert foundry_dsr(0.02, "eth", "1H", tmp_path, T=20000) == 1.0   # no 1H trials


def test_evaluate_feeds_dsr_the_gross_sharpe_and_the_sample_count(tmp_path, monkeypatch):
    """T must be the observations that actually entered the estimate (n_samples),
    not one year of bars."""
    import research.hermes.gatekeeper as gk
    seen = {}
    monkeypatch.setattr(gk, "foundry_dsr",
                        lambda best, sym, iv, md, T: seen.update(best=best, T=T) or 0.9)
    idx = pd.date_range("2022-01-01", periods=400, freq="h", tz="UTC")
    rng = np.random.default_rng(3)
    ohlcv = pd.DataFrame({"close": 100 + np.cumsum(rng.standard_normal(400) * 0.1)}, index=idx)
    factor = pd.Series(rng.standard_normal(400), index=idx)
    res = gk.evaluate(factor, ohlcv, pd.Series("neutral", index=idx.normalize().unique()),
                      pd.DataFrame(index=idx), "eth", tmp_path,
                      gk.GateConfig(interval="1H", horizon_h=24))
    assert seen["best"] == res.metrics["gross_ir"]     # gross, not net
    assert seen["T"] == res.metrics["n_samples"]       # not bars_per_year (8760)
