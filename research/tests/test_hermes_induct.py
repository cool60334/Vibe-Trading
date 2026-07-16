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


from research.hermes.induct import write_induction
from research.lib.inducted_factors import inducted_dir, inducted_names


def test_write_induction_writes_module_meta_and_golden(tmp_path):
    code = "def compute(df):\n    return df['close']\n"
    fixture = _panel(6)
    expected = fixture["close"]
    write_induction(code, "foundry_x", "eth", fixture, expected,
                    root=tmp_path, tests_root=tmp_path / "t")

    d = inducted_dir("eth", root=tmp_path)
    assert (d / "foundry_x.py").read_text(encoding="utf-8") == code
    assert (d / "foundry_x.meta.json").exists()
    assert inducted_names("eth", root=tmp_path) == {"foundry_x"}
    assert (tmp_path / "t" / "foundry_x_fixture.parquet").exists()
    assert (tmp_path / "t" / "test_foundry_x.py").exists()


def test_write_induction_refuses_overwrite_without_flag(tmp_path):
    code = "def compute(df):\n    return df['close']\n"
    fixture = _panel(6)
    expected = fixture["close"]
    write_induction(code, "foundry_x", "eth", fixture, expected,
                    root=tmp_path, tests_root=tmp_path / "t")

    with pytest.raises(InductRefused, match="already inducted"):
        write_induction(code, "foundry_x", "eth", fixture, expected,
                        root=tmp_path, tests_root=tmp_path / "t")

    # overwrite=True allows the same factor_id to be re-inducted.
    new_code = "def compute(df):\n    return df['close'] * 2\n"
    write_induction(new_code, "foundry_x", "eth", fixture, expected,
                    root=tmp_path, tests_root=tmp_path / "t", overwrite=True)
    d = inducted_dir("eth", root=tmp_path)
    assert (d / "foundry_x.py").read_text(encoding="utf-8") == new_code


def test_write_induction_normalizes_symbol_consistently(tmp_path):
    """Regression (final-review Important finding): write_induction() must write
    the module under the NORMALIZED symbol dir (inducted_dir's _symbol_short),
    and the generated golden test's _SYM / mod_path must reference that same
    normalized value -- not the raw, un-normalized `symbol` argument. Before the
    fix, passing "ETH" wrote the module to inducted/eth/ (normalized) but baked
    _SYM='ETH' into the golden test, so its mod_path pointed at inducted/ETH/
    -- a directory that doesn't exist on a case-sensitive filesystem."""
    code = "def compute(df):\n    return df['close']\n"
    fixture = _panel(6)
    expected = fixture["close"]
    raw_symbol = "ETH"

    write_induction(code, "foundry_y", raw_symbol, fixture, expected,
                    root=tmp_path, tests_root=tmp_path / "t")

    normalized = inducted_dir(raw_symbol, root=tmp_path).name
    assert normalized == "eth"  # sanity: this symbol DOES change under normalization

    # Module was written under the normalized dir, not a raw "ETH" dir.
    d = inducted_dir(raw_symbol, root=tmp_path)
    assert (d / "foundry_y.py").exists()
    assert not (tmp_path / "ETH").exists()

    # .meta.json's "symbol" field is the normalized value.
    meta = json.loads((d / "foundry_y.meta.json").read_text(encoding="utf-8"))
    assert meta["symbol"] == normalized

    # The generated golden test's _SYM matches the normalized value (and thus
    # its mod_path agrees with where the module actually landed).
    test_src = (tmp_path / "t" / "test_foundry_y.py").read_text(encoding="utf-8")
    assert f"_SYM = {normalized!r}" in test_src
    assert f"_SYM = {raw_symbol!r}" not in test_src
