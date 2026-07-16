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


def test_verify_gates_refuses_missing_sha(tmp_path, monkeypatch):
    """Fail-closed: meta.json lacking a code_sha256 key at all must refuse,
    not silently pass the tamper check (previously only an actual mismatch
    refused; a missing key skipped the check entirely)."""
    code = "def compute(df):\n    return df['close']\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code, {})  # no code_sha256 key
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
    assert (tmp_path / "t" / "eth_foundry_x_fixture.parquet").exists()
    assert (tmp_path / "t" / "test_eth_foundry_x.py").exists()


def test_golden_files_are_symbol_namespaced_no_cross_symbol_overwrite(tmp_path):
    # agy acceptance finding: two symbols can each have a `foundry_x`. Golden
    # test/fixture filenames MUST carry the symbol, or inducting btc:foundry_x
    # would physically overwrite eth:foundry_x's golden test -> lost coverage.
    code = "def compute(df):\n    return df['close']\n"
    fx = _panel(6); exp = fx["close"]
    write_induction(code, "foundry_x", "eth", fx, exp, root=tmp_path, tests_root=tmp_path / "t")
    write_induction(code, "foundry_x", "btc", fx, exp, root=tmp_path, tests_root=tmp_path / "t")

    assert (tmp_path / "t" / "test_eth_foundry_x.py").exists()
    assert (tmp_path / "t" / "test_btc_foundry_x.py").exists()          # both survive
    assert (tmp_path / "t" / "eth_foundry_x_fixture.parquet").exists()
    assert (tmp_path / "t" / "btc_foundry_x_fixture.parquet").exists()


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
    test_src = (tmp_path / "t" / "test_eth_foundry_y.py").read_text(encoding="utf-8")
    assert f"_SYM = {normalized!r}" in test_src
    assert f"_SYM = {raw_symbol!r}" not in test_src


# --- main() end-to-end: the CLI glue (arg wiring, join, gate ordering,
# --confirm/--overwrite threading, the broadened except) was previously only
# smoke-tested via --help. verify_gates/revalidate's own logic is already
# covered above, so these tests stub them out and focus purely on main()'s
# own orchestration.

def _panel_idx(n=5):
    return pd.date_range("2024-06-01", periods=n, freq="1h", tz="UTC")


def _stub_main_infra(monkeypatch, idx):
    """Stub every lazily-imported dependency main() wires together, so a call
    reaches the confirm-gate / write path without touching Docker, the
    network, or any real file outside tmp_path."""
    import research.lib.factor_io as factor_io
    import research.hermes.foundry_runner as foundry_runner
    import research.hermes.sandbox as sandbox_mod
    import research.hermes.orchestrator as orchestrator

    # features and ohlcv must not share columns, mirroring production (feature
    # store columns vs. raw OHLCV) -- .join() raises on overlapping columns.
    features = pd.DataFrame({"some_factor": np.arange(len(idx), dtype=float)}, index=idx)
    ohlcv = pd.DataFrame({"close": np.arange(len(idx), dtype=float)}, index=idx)
    monkeypatch.setattr(factor_io, "load_features", lambda symbol, manifests_dir=None, **k: features)
    monkeypatch.setattr(foundry_runner, "load_ohlcv", lambda path: ohlcv.copy())
    monkeypatch.setattr(foundry_runner, "resolve_image_id", lambda tag: "fake-image-id")

    class _DummySandbox:
        def __init__(self, **kwargs):
            pass
    monkeypatch.setattr(sandbox_mod, "DockerSandbox", _DummySandbox)
    monkeypatch.setattr(orchestrator, "make_run_sandbox",
                        lambda sandbox, scratch_dir: (lambda code, panel: panel["close"]))


def test_main_refuses_without_confirm(tmp_path, monkeypatch, capsys):
    idx = _panel_idx()
    _stub_main_infra(monkeypatch, idx)
    monkeypatch.setattr(ind, "verify_gates", lambda factor_id, symbol, mdir: "def compute(df):\n    return df['close']\n")
    monkeypatch.setattr(ind, "revalidate", lambda *a, **k: None)
    write_calls = []
    monkeypatch.setattr(ind, "write_induction", lambda *a, **k: write_calls.append((a, k)))

    rc = ind.main(["--symbol", "eth", "--factor", "foundry_x",
                   "--manifests-dir", str(tmp_path), "--oos-start", "2024-06-02"])

    assert rc == 2
    assert write_calls == []  # confirm-gate: no write without --confirm
    out = capsys.readouterr().out
    assert "Re-run with --confirm" in out


def test_main_writes_with_confirm(tmp_path, monkeypatch, capsys):
    idx = _panel_idx()
    _stub_main_infra(monkeypatch, idx)
    monkeypatch.setattr(ind, "verify_gates", lambda factor_id, symbol, mdir: "def compute(df):\n    return df['close']\n")
    revalidate_calls = []
    monkeypatch.setattr(ind, "revalidate", lambda *a, **k: revalidate_calls.append((a, k)))
    write_calls = []
    monkeypatch.setattr(ind, "write_induction", lambda *a, **k: write_calls.append((a, k)))

    rc = ind.main(["--symbol", "eth", "--factor", "foundry_x",
                   "--manifests-dir", str(tmp_path), "--oos-start", "2024-06-02",
                   "--confirm", "--overwrite"])

    assert rc == 0
    assert len(write_calls) == 1
    args, kwargs = write_calls[0]
    # code, factor_id, symbol, fixture, expected positional args + overwrite kwarg threaded through
    assert args[0] == "def compute(df):\n    return df['close']\n"
    assert args[1] == "foundry_x"
    assert args[2] == "eth"
    assert kwargs.get("overwrite") is True
    assert len(revalidate_calls) == 1
    out = capsys.readouterr().out
    assert "inducted" in out


def test_main_broadened_except_catches_infra_failure_cleanly(tmp_path, monkeypatch, capsys):
    """A Docker/infra failure (SandboxError, a HermesGuardError subclass) must
    produce a clean [induct] REFUSED: message and exit 2, not a raw traceback."""
    from research.hermes.sandbox import SandboxError
    idx = _panel_idx()
    _stub_main_infra(monkeypatch, idx)
    import research.hermes.foundry_runner as foundry_runner
    def _boom(tag):
        raise SandboxError(f"sandbox image {tag!r} does not resolve on this host")
    monkeypatch.setattr(foundry_runner, "resolve_image_id", _boom)
    monkeypatch.setattr(ind, "verify_gates", lambda factor_id, symbol, mdir: "def compute(df):\n    return df['close']\n")

    rc = ind.main(["--symbol", "eth", "--factor", "foundry_x",
                   "--manifests-dir", str(tmp_path), "--oos-start", "2024-06-02", "--confirm"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "[induct] REFUSED:" in err
    assert "does not resolve on this host" in err
