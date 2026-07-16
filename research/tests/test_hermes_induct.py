import hashlib, json
import numpy as np, pandas as pd
import pytest
from research.hermes import induct as ind
from research.hermes.induct import verify_gates, InductRefused, revalidate
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


def _panel(n=120, start="2024-11-01"):
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.arange(float(n))}, index=idx)


def test_revalidate_refuses_non_deterministic(tmp_path, monkeypatch):
    panel = _panel()
    # run_sandbox returns a DIFFERENT series each call -> non-deterministic
    seq = iter([pd.Series(np.arange(120.0), index=panel.index),
                pd.Series(np.arange(120.0) + 1, index=panel.index)])
    monkeypatch.setattr("research.hermes.induct.pit_check_via_sandbox", lambda *a, **k: None)
    with pytest.raises(InductRefused, match="determinism|deterministic"):
        revalidate("code", "foundry_x", "eth", panel,
                   lambda c, p: next(seq), "2024-11-03", tmp_path)


def test_revalidate_refuses_bridge_divergence(tmp_path, monkeypatch):
    panel = _panel()
    monkeypatch.setattr("research.hermes.induct.pit_check_via_sandbox", lambda *a, **k: None)
    # stored cand values differ from the recompute -> diverges
    oos = "2024-11-03"
    pre = panel.index < pd.Timestamp(oos, tz="UTC")
    cand = pd.DataFrame({"foundry_x": pd.Series(np.arange(120.0) + 999, index=panel.index)[pre]})
    from research.hermes.candidate_store import _candidate_path
    p = _candidate_path("eth", tmp_path); p.parent.mkdir(parents=True, exist_ok=True); cand.to_parquet(p)
    with pytest.raises(InductRefused, match="diverge|reconcile"):
        revalidate("code", "foundry_x", "eth", panel,
                   lambda c, p2: pd.Series(np.arange(120.0), index=panel.index), oos, tmp_path)


def test_revalidate_fails_closed_no_candidate_parquet(tmp_path, monkeypatch):
    """Fail-closed: refuse induction when no candidate parquet exists at all,
    even if determinism passes."""
    panel = _panel()
    monkeypatch.setattr("research.hermes.induct.pit_check_via_sandbox", lambda *a, **k: None)
    # run_sandbox returns the SAME series both times -> determinism passes
    series = pd.Series(np.arange(120.0), index=panel.index)
    with pytest.raises(InductRefused, match="no stored candidate|cannot verify"):
        revalidate("code", "foundry_x", "eth", panel,
                   lambda c, p: series, "2024-11-03", tmp_path)


def test_revalidate_fails_closed_missing_factor_column(tmp_path, monkeypatch):
    """Fail-closed: refuse induction when factor_id is not a column in the
    stored candidate parquet, even if determinism passes."""
    panel = _panel()
    monkeypatch.setattr("research.hermes.induct.pit_check_via_sandbox", lambda *a, **k: None)
    # run_sandbox returns the SAME series both times -> determinism passes
    series = pd.Series(np.arange(120.0), index=panel.index)
    # Write a candidate parquet with a different factor column (not "foundry_x")
    oos = "2024-11-03"
    pre = panel.index < pd.Timestamp(oos, tz="UTC")
    cand = pd.DataFrame({"some_other_factor": pd.Series(np.arange(120.0), index=panel.index)[pre]})
    from research.hermes.candidate_store import _candidate_path
    p = _candidate_path("eth", tmp_path); p.parent.mkdir(parents=True, exist_ok=True); cand.to_parquet(p)
    with pytest.raises(InductRefused, match="is not a column|cannot verify path-consistency"):
        revalidate("code", "foundry_x", "eth", panel,
                   lambda c, p: series, oos, tmp_path)
