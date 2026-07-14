import json
import pytest
from research.hermes.candidate_store import (
    code_dir, write_candidate_code, load_candidate_code,
)


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
