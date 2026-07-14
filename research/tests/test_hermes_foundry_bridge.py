import numpy as np
import pandas as pd
from research.hermes.foundry_bridge import FOUNDRY_PREFIX, recompute_full_span


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
