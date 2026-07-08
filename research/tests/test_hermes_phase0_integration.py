import numpy as np
import pandas as pd
from research.hermes.pit import assert_no_lookahead
from research.hermes.split import foundry_split
from research.hermes.candidate_store import write_candidate


def test_end_to_end_causal_factor_pipeline(tmp_path):
    idx = pd.date_range("2024-06-01", periods=400, freq="1D")
    df = pd.DataFrame({"close": 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 400))}, index=idx)

    compute = lambda d: d["close"].pct_change(5)     # causal
    assert_no_lookahead(compute, df) is None          # PIT gate

    train, val = foundry_split(df, oos_start="2025-01-01", val_frac=0.2)
    assert train.index.max() < pd.Timestamp("2025-01-01")

    feat = pd.DataFrame({"mom5": compute(train)})
    path = write_candidate(feat, "eth", manifests_dir=tmp_path)
    assert path.exists() and "candidate_features" in path.parts
