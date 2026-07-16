import hashlib, json
import pytest
from research.hermes import induct as ind
from research.hermes.induct import verify_gates, InductRefused
from research.hermes.candidate_store import write_candidate_code


def _card(fid, verdict="candidate"):
    class C:  # duck-typed EvidenceCard
        factor_id = fid;
    C.verdict = verdict
    return C


def test_verify_gates_returns_code_for_a_clean_candidate(tmp_path, monkeypatch):
    code = "def compute(df):\n    return df['close'].rolling(3).mean()\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code,
                         {"code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x")])
    assert verify_gates("foundry_x", "eth", tmp_path) == code


def test_verify_gates_refuses_non_candidate(tmp_path, monkeypatch):
    code = "def compute(df):\n    return df['close']\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code,
                         {"code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x", verdict="graveyard")])
    with pytest.raises(InductRefused, match="verdict"):
        verify_gates("foundry_x", "eth", tmp_path)


def test_verify_gates_refuses_sha_mismatch(tmp_path, monkeypatch):
    write_candidate_code("foundry_x", "eth", tmp_path,
                         "def compute(df):\n    return df['close']\n", {"code_sha256": "deadbeef"})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x")])
    with pytest.raises(InductRefused, match="sha"):
        verify_gates("foundry_x", "eth", tmp_path)


def test_verify_gates_refuses_unsafe_ast(tmp_path, monkeypatch):
    code = "import os\ndef compute(df):\n    return df['close']\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code,
                         {"code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x")])
    with pytest.raises(InductRefused, match="AST|import"):
        verify_gates("foundry_x", "eth", tmp_path)
