import numpy as np
import pandas as pd


def test_build_feature_dict_merges_inducted_over_full_library(tmp_path, monkeypatch):
    # an inducted factor may reference a library column; it must see the fully
    # built library panel and appear in the output.
    import research.pipeline.stage0a_features as s0
    from research.lib.inducted_factors import inducted_dir

    d = inducted_dir("eth", root=tmp_path); d.mkdir(parents=True)
    # depends on a library feature 'atr_14' -> proves it sees the built library
    (d / "foundry_x.py").write_text("def compute(df):\n    return df['atr_14'] * 10\n", encoding="utf-8")
    (d / "foundry_x.meta.json").write_text("{}", encoding="utf-8")

    idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    monkeypatch.setattr(s0, "compute_inducted",
                        lambda panel, symbol, root=None: __import__("research.lib.inducted_factors",
                        fromlist=["compute_inducted"]).compute_inducted(panel, symbol, root=tmp_path))

    features = {"atr_14": pd.Series(np.arange(5.0), index=idx)}
    panel = pd.DataFrame({"close": np.arange(5.0)}, index=idx).assign(**features)
    out = s0._merge_inducted(features, panel, "eth")

    assert "foundry_x" in out
    assert (out["foundry_x"].to_numpy() == np.arange(5.0) * 10).all()
