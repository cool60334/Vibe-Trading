import numpy as np
import pandas as pd
import pytest
from research.hermes.split import foundry_split, OOSLeakError


def _df(start, n, freq="1D"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"x": np.arange(n)}, index=idx)


def test_split_drops_oos_and_orders_train_before_val():
    df = _df("2024-06-01", 400)  # spans past 2025-01-01 oos_start
    train, val = foundry_split(df, oos_start="2025-01-01", val_frac=0.25)
    assert train.index.max() < pd.Timestamp("2025-01-01")
    assert val.index.max() < pd.Timestamp("2025-01-01")
    assert train.index.max() <= val.index.min()          # chronological, no shuffle
    assert len(val) == round(0.25 * (len(train) + len(val)))


def test_strict_mode_raises_when_caller_preselected_oos_rows():
    df = _df("2025-02-01", 30)  # entirely inside locked OOS
    with pytest.raises(OOSLeakError):
        foundry_split(df, oos_start="2025-01-01", val_frac=0.2, strict=True)


def test_tz_aware_index_does_not_crash():
    df = _df("2024-06-01", 400)
    df.index = df.index.tz_localize("UTC")  # like okx_data.py real loaders
    train, val = foundry_split(df, oos_start="2025-01-01", val_frac=0.25)
    assert not train.empty and not val.empty
    assert train.index.max() < pd.Timestamp("2025-01-01", tz="UTC")


def test_unsorted_index_raises():
    df = _df("2024-06-01", 400).iloc[::-1]  # descending order
    with pytest.raises(ValueError, match="sorted"):
        foundry_split(df, oos_start="2025-01-01", val_frac=0.25)


def test_val_frac_producing_empty_split_raises():
    df = _df("2024-06-01", 5)  # too few pre-oos rows for a tiny val_frac
    with pytest.raises(ValueError, match="empty split"):
        foundry_split(df, oos_start="2025-01-01", val_frac=0.01)
