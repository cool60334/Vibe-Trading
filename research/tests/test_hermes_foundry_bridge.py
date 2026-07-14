import numpy as np
import pandas as pd
from research.hermes.foundry_bridge import FOUNDRY_PREFIX, recompute_full_span, reconciles_pre_oos


def _panel(n=100, start="2024-11-01"):
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.arange(float(n))}, index=idx)


def test_recompute_covers_the_full_span_including_oos():
    # Foundry only ever computed pre-oos values; the bridge must produce values
    # across the WHOLE panel or stage3's OOS window would be all-NaN.
    panel = _panel(100)
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    out = recompute_full_span("code", panel, run)

    assert out.index.equals(panel.index)
    assert out.notna().all()
    assert FOUNDRY_PREFIX == "foundry_"


_OOS = "2024-11-03"


def test_reconciles_when_pre_oos_matches():
    panel = _panel(100)
    rec = pd.Series(np.arange(100.0), index=panel.index)
    stored = rec[rec.index < pd.Timestamp(_OOS, tz="UTC")]
    assert reconciles_pre_oos(rec, stored, _OOS) is True


def test_reconciliation_tolerates_burn_in_nan():
    # rolling factors are NaN for their warm-up window; np.allclose defaults to
    # equal_nan=False, which would kill a perfectly valid factor.
    panel = _panel(100)
    rec = pd.Series(np.arange(100.0), index=panel.index)
    rec.iloc[:5] = np.nan
    stored = rec[rec.index < pd.Timestamp(_OOS, tz="UTC")]
    assert reconciles_pre_oos(rec, stored, _OOS) is True


def test_reconciliation_fails_when_pre_oos_drifts():
    panel = _panel(100)
    rec = pd.Series(np.arange(100.0), index=panel.index)
    stored = rec[rec.index < pd.Timestamp(_OOS, tz="UTC")] + 1.0   # drift
    assert reconciles_pre_oos(rec, stored, _OOS) is False
