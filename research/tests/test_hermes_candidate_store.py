from pathlib import Path

import json
import pandas as pd
import pytest
from research.hermes.candidate_store import (
    code_dir, write_candidate_code, load_candidate_code,
    write_candidate, ProductionWriteError, CANDIDATE_SUBDIR,
)


# ── Restored tests: containment law for write_candidate ──────────────────────

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


def test_refuses_production_filename_prefix(tmp_path, monkeypatch):
    # Guard clause: filename must not start with production prefixes (features_, factor_values_)
    # Monkeypatch _candidate_path to simulate a scenario where it would return a production basename
    from research.hermes import candidate_store

    monkeypatch.setattr(
        candidate_store,
        "_candidate_path",
        lambda symbol, manifests_dir: Path(manifests_dir) / "candidate_features" / "features_eth.parquet",
    )
    with pytest.raises(ProductionWriteError):
        write_candidate(_df(), "eth", manifests_dir=tmp_path)


# ── New Task 1 tests: code persistence for candidates ────────────────────────

def test_candidate_code_roundtrips_with_metadata(tmp_path):
    code = "def compute(panel):\n    return panel['close'].pct_change()\n"
    meta = {"code_sha256": "abc123", "base_image_id": "sha256:deadbeef",
            "interval": "1H", "horizon_h": 24}

    write_candidate_code("zoo_mom", "eth", tmp_path, code, meta)
    got_code, got_meta = load_candidate_code("zoo_mom", "eth", tmp_path)

    assert got_code == code
    assert got_meta["code_sha256"] == "abc123"
    assert got_meta["base_image_id"] == "sha256:deadbeef"


def test_code_store_lives_under_candidate_features(tmp_path):
    # must never escape the isolated candidate store (same law as write_candidate)
    d = code_dir("eth", tmp_path)
    assert d.parent.parent.name == "candidate_features"


def test_load_candidate_code_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_candidate_code("nope", "eth", tmp_path)
