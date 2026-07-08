import pandas as pd
import pytest
from research.hermes.candidate_store import write_candidate, ProductionWriteError, CANDIDATE_SUBDIR


def _df():
    idx = pd.date_range("2024-01-01", periods=5, freq="1h")
    return pd.DataFrame({"cand_feat": [1.0, 2, 3, 4, 5]}, index=idx)


def test_writes_under_candidate_dir_atomically(tmp_path):
    path = write_candidate(_df(), "eth", manifests_dir=tmp_path)
    assert CANDIDATE_SUBDIR in path.parts
    assert path.exists()
    assert not list(path.parent.glob("*.tmp"))          # no temp leftover
    pd.testing.assert_frame_equal(pd.read_parquet(path), _df(), check_freq=False)


def test_refuses_production_feature_path(tmp_path, monkeypatch):
    # simulate a caller trying to redirect output at the production store
    from research.hermes import candidate_store
    monkeypatch.setattr(candidate_store, "CANDIDATE_SUBDIR", "features")  # production dir name
    with pytest.raises(ProductionWriteError):
        write_candidate(_df(), "eth", manifests_dir=tmp_path)
