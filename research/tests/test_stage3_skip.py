# research/tests/test_stage3_skip.py
"""stage3 missing-factor graceful skip — run from research/ (pytest tests/).

A 1H-only strategy whose factor isn't a candidate at a sub-hour interval makes
its signal engine raise KeyError. The runner exits 3 (distinct from a real
failure's 1); stage3 must classify that as a SKIP, not a FAIL, so one
interval-incompatible strategy doesn't block the whole pipeline.
"""
import json
import types

from pipeline.stage3_backtest import (
    BacktestRunResult,
    classify_backtest_proc,
    clear_missing_factor_sentinel,
    compute_exit_code,
    write_missing_factor_sentinel,
)


def test_returncode_3_is_skip_not_fail():
    r = classify_backtest_proc("eth_s5_train", returncode=3, stderr="missing column")
    assert r is not None
    assert r.ok is True
    assert r.skipped_missing_factor is True


def test_returncode_1_is_fail():
    r = classify_backtest_proc("eth_s5_train", returncode=1, stderr="KeyError boom")
    assert r is not None
    assert r.ok is False
    assert "code 1" in (r.error or "")


def test_returncode_0_returns_none_for_artifact_verification():
    assert classify_backtest_proc("x", returncode=0, stderr="") is None


def test_missing_factor_skip_does_not_fail_stage():
    results = [
        BacktestRunResult(run_name="ok_run", ok=True),
        BacktestRunResult(run_name="eth_s5_train", ok=True, skipped_missing_factor=True),
    ]
    assert compute_exit_code(results) == 0


# ── stage3 sentinel: the cross-process channel to stage3-diag ────────────────


def test_write_missing_factor_sentinel_creates_file(tmp_path):
    write_missing_factor_sentinel(tmp_path, "sol_s1_multi_factor_consensus", interval="1H")
    p = tmp_path / "sol_s1_multi_factor_consensus" / "missing_factor.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["missing_factor"] is True
    assert data["interval"] == "1H"


def test_clear_missing_factor_sentinel_removes_file(tmp_path):
    write_missing_factor_sentinel(tmp_path, "s1", interval="1H")
    clear_missing_factor_sentinel(tmp_path, "s1")
    assert not (tmp_path / "s1" / "missing_factor.json").exists()


def test_clear_missing_factor_sentinel_noop_when_absent(tmp_path):
    clear_missing_factor_sentinel(tmp_path, "never_written")  # must not raise


# ── stage3-diag mirrors the graceful skip ────────────────────────────────────


def _make_entry(base_run: str | None = "sol_s1_base"):
    from pipeline.strategy_runs import StrategyRunsEntry
    return StrategyRunsEntry(
        symbol="SOL-USDT-SWAP",
        spec_yaml="research/strategies/strategy_S1.yaml",
        base_run=base_run,
        regime_runs=types.MappingProxyType({}),
        stress_runs=types.MappingProxyType({}),
        sweep_run=None,
    )


def _make_cfg():
    from pipeline.config import FeesConfig, ResearchConfig, SymbolConfig
    return ResearchConfig(
        symbols=(SymbolConfig(name="sol", okx_swap="SOL-USDT-SWAP", ccxt_bybit="SOL/USDT:USDT"),),
        period=730,
        interval="1H",
        data_source="okx",
        engine="daily",
        fees=FeesConfig(maker_rate=0.0002, taker_rate=0.00055, slippage=0.0005),
        horizons_h=(8, 24, 72, 168),
    )


def test_diag_skips_when_missing_factor_sentinel_present(tmp_path):
    """metrics.csv absent + sentinel present -> SKIP (ok=True), no LLM call."""
    from pipeline.stage3_diagnose import _diagnose_strategy
    from pipeline import stage3_backtest

    runs_root = tmp_path / "runs"
    manifests_dir = tmp_path / "manifests"
    # Sentinel present, metrics.csv deliberately NOT created.
    stage3_backtest.write_missing_factor_sentinel(
        manifests_dir, "sol_s1_multi_factor_consensus", interval="1H"
    )

    result = _diagnose_strategy(
        strategy_id="sol_s1_multi_factor_consensus",
        entry=_make_entry("sol_s1_base"),
        cfg=_make_cfg(),
        runs_root=runs_root,
        manifests_dir=manifests_dir,
    )
    assert result.ok is True
    assert result.skipped is True


def test_diag_fails_when_metrics_missing_and_no_sentinel(tmp_path):
    """metrics.csv absent + NO sentinel -> genuine FAIL (ok=False)."""
    from pipeline.stage3_diagnose import _diagnose_strategy

    result = _diagnose_strategy(
        strategy_id="sol_s1_real_failure",
        entry=_make_entry("sol_s1_base"),
        cfg=_make_cfg(),
        runs_root=tmp_path / "runs",
        manifests_dir=tmp_path / "manifests",
    )
    assert result.ok is False
    assert result.skipped is False
    assert "metrics.csv" in (result.error or "")


def test_diag_skipped_result_does_not_fail_stage():
    from pipeline.stage3_diagnose import DiagnosisCheckResult, compute_exit_code

    results = [
        DiagnosisCheckResult(strategy_id="ok", ok=True),
        DiagnosisCheckResult(
            strategy_id="skipped", ok=True, skipped=True, error="factor absent"
        ),
    ]
    assert compute_exit_code(results) == 0


def test_diag_print_summary_marks_skip(capsys):
    from pipeline.stage3_diagnose import DiagnosisCheckResult, print_summary

    print_summary(
        [DiagnosisCheckResult(strategy_id="s", ok=True, skipped=True, error="factor absent")]
    )
    out = capsys.readouterr().out
    assert "[SKIP]" in out
