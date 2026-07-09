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
