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
