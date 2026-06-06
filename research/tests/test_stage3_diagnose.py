"""
Tests for research/pipeline/stage3_diagnose.py pure-logic helpers.

All tests are self-contained — no real filesystem I/O (beyond tmp_path),
no real subprocess calls. Subprocess calls are mocked.

Covers:
  (a) read_metrics_csv              — happy path, missing file, malformed CSV
  (b) build_diagnosis_prompt        — contains strategy_id, recommended_action, metrics JSON
  (c) parse_diagnosis_response      — valid block, no block, invalid JSON, missing key, bad value
  (d) rule_based_action             — all three outcomes + edge cases + empty metrics
  (e) build_diagnosis_block         — structure matches DiagnosisBlock fields
  (f) verify_diagnosis              — present+valid, missing, invalid JSON, schema fail
  (g) compute_exit_code             — empty → 1, all ok → 0, any fail → 1
  (h) print_summary                 — smoke test (no crash)
  (i) integration-level             — _diagnose_strategy with mocked run_vibe_trading_diagnose

Pytest is run from research/ as:
    cd research && python -m pytest tests/test_stage3_diagnose.py -v
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Bootstrap: research/ and repo root must be on sys.path.
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_DIR = _THIS_FILE.parents[1]   # research/
_REPO_ROOT = _RESEARCH_DIR.parent       # repo root

for _p in (_RESEARCH_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

# Also add dashboard/server/ for schemas.
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"
if str(_DASHBOARD_SCHEMAS) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_SCHEMAS))

from schemas import DiagnosisBlock, RecommendedAction  # noqa: E402

from pipeline.stage3_diagnose import (  # noqa: E402
    DiagnosisCheckResult,
    _diagnose_strategy,
    build_diagnosis_block,
    build_diagnosis_prompt,
    compute_exit_code,
    parse_diagnosis_response,
    print_summary,
    read_metrics_csv,
    read_optimization_best,
    read_walk_forward_metrics,
    rule_based_action,
    verify_diagnosis,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_research_config(period: int = 730, interval: str = "1H"):
    """Return a minimal ResearchConfig for tests."""
    from pipeline.config import FeesConfig, ResearchConfig, SymbolConfig
    return ResearchConfig(
        symbols=(
            SymbolConfig(name="btc", okx_swap="BTC-USDT-SWAP", ccxt_bybit="BTC/USDT:USDT"),
        ),
        period=period,
        interval=interval,
        data_source="okx",
        engine="daily",
        fees=FeesConfig(maker_rate=0.0002, taker_rate=0.00055, slippage=0.0005),
        horizons_h=(8, 24, 72, 168),
    )


def _make_strategy_entry(
    strategy_id: str = "btc_s1_test",
    symbol: str = "BTC-USDT-SWAP",
    base_run: str | None = "btc_s1_base",
    regime_runs: dict | None = None,
    oos_runs: list | None = None,
):
    """Return a minimal StrategyRunsEntry for tests."""
    from pipeline.strategy_runs import StrategyRunsEntry
    return StrategyRunsEntry(
        symbol=symbol,
        spec_yaml="research/strategies/strategy_S1.yaml",
        base_run=base_run,
        regime_runs=types.MappingProxyType(regime_runs or {}),
        stress_runs=types.MappingProxyType({}),
        oos_runs=tuple(oos_runs or []),
        sweep_run=None,
    )


def _write_metrics_csv(path: Path, row: dict) -> None:
    """Write a 1-row metrics CSV to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


GOOD_METRICS = {
    "sharpe": "2.1",
    "max_drawdown": "-0.07",
    "trade_count": "150",
    "profit_factor": "1.8",
    "total_return": "0.45",
    "win_rate": "0.54",
}

POOR_METRICS = {
    "sharpe": "-0.3",
    "max_drawdown": "-0.35",
    "trade_count": "10",
    "profit_factor": "0.9",
    "total_return": "-0.15",
    "win_rate": "0.41",
}

MEDIOCRE_METRICS = {
    "sharpe": "0.8",
    "max_drawdown": "-0.12",
    "trade_count": "60",
    "profit_factor": "1.2",
    "total_return": "0.12",
    "win_rate": "0.50",
}


# ---------------------------------------------------------------------------
# (a) read_metrics_csv
# ---------------------------------------------------------------------------


class TestReadMetricsCsv:
    def test_happy_path_returns_dict(self, tmp_path):
        csv_file = tmp_path / "metrics.csv"
        _write_metrics_csv(csv_file, GOOD_METRICS)
        result = read_metrics_csv(csv_file)
        assert result is not None
        assert isinstance(result, dict)

    def test_happy_path_numeric_conversion(self, tmp_path):
        csv_file = tmp_path / "metrics.csv"
        _write_metrics_csv(csv_file, GOOD_METRICS)
        result = read_metrics_csv(csv_file)
        assert result["sharpe"] == pytest.approx(2.1)
        assert result["trade_count"] == pytest.approx(150)

    def test_happy_path_all_keys_present(self, tmp_path):
        csv_file = tmp_path / "metrics.csv"
        _write_metrics_csv(csv_file, GOOD_METRICS)
        result = read_metrics_csv(csv_file)
        for key in GOOD_METRICS:
            assert key in result

    def test_missing_file_returns_none(self, tmp_path):
        csv_file = tmp_path / "nonexistent.csv"
        assert read_metrics_csv(csv_file) is None

    def test_empty_csv_returns_none(self, tmp_path):
        """CSV with only a header and no data rows returns None."""
        csv_file = tmp_path / "metrics.csv"
        csv_file.write_text("sharpe,max_drawdown\n", encoding="utf-8")
        assert read_metrics_csv(csv_file) is None

    def test_malformed_csv_returns_none(self, tmp_path):
        """A file that raises an exception during parsing returns None."""
        csv_file = tmp_path / "metrics.csv"
        # Write binary garbage that will raise on csv parsing.
        csv_file.write_bytes(b"\x00\xff\xfe")
        # read_metrics_csv should catch exceptions and return None.
        result = read_metrics_csv(csv_file)
        assert result is None

    def test_non_numeric_string_preserved(self, tmp_path):
        csv_file = tmp_path / "metrics.csv"
        row = {"label": "foo", "sharpe": "1.5"}
        _write_metrics_csv(csv_file, row)
        result = read_metrics_csv(csv_file)
        assert result["label"] == "foo"
        assert result["sharpe"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# (b) build_diagnosis_prompt
# ---------------------------------------------------------------------------


class TestBuildDiagnosisPrompt:
    def test_contains_strategy_id(self):
        prompt = build_diagnosis_prompt("my_strategy_id", {})
        assert "my_strategy_id" in prompt

    def test_contains_recommended_action_keyword(self):
        prompt = build_diagnosis_prompt("s1", {})
        assert "recommended_action" in prompt

    def test_contains_metrics_json(self):
        metrics = {"btc_base": {"sharpe": 1.5, "trade_count": 120}}
        prompt = build_diagnosis_prompt("s1", metrics)
        assert "btc_base" in prompt
        assert "sharpe" in prompt

    def test_contains_valid_action_options(self):
        prompt = build_diagnosis_prompt("s1", {})
        assert "proceed" in prompt
        assert "back_to_stage_2" in prompt
        assert "back_to_stage_4" in prompt

    def test_prompt_is_string(self):
        prompt = build_diagnosis_prompt("s1", {"run": {"sharpe": 2.0}})
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_empty_metrics_dict_no_crash(self):
        prompt = build_diagnosis_prompt("s1", {})
        assert isinstance(prompt, str)

    def test_metrics_json_injected(self):
        metrics = {"run_a": {"sharpe": 1.8, "trade_count": 200}}
        prompt = build_diagnosis_prompt("my_strat", metrics)
        # The metrics JSON should appear in the prompt.
        assert "1.8" in prompt or "run_a" in prompt


# ---------------------------------------------------------------------------
# (c) parse_diagnosis_response
# ---------------------------------------------------------------------------


class TestParseDiagnosisResponse:
    def _wrap(self, obj: dict) -> str:
        """Wrap a dict in a ```json ... ``` block."""
        return f"Some preamble\n```json\n{json.dumps(obj)}\n```\nSome suffix"

    def test_valid_proceed(self):
        stdout = self._wrap({"recommended_action": "proceed", "summary": "All good.", "findings": []})
        result = parse_diagnosis_response(stdout)
        assert result is not None
        assert result["recommended_action"] == "proceed"

    def test_valid_back_to_stage_2(self):
        stdout = self._wrap({"recommended_action": "back_to_stage_2", "summary": "Bad.", "findings": ["low sharpe"]})
        result = parse_diagnosis_response(stdout)
        assert result is not None
        assert result["recommended_action"] == "back_to_stage_2"

    def test_valid_back_to_stage_4(self):
        stdout = self._wrap({"recommended_action": "back_to_stage_4", "summary": "Needs opt.", "findings": []})
        result = parse_diagnosis_response(stdout)
        assert result is not None
        assert result["recommended_action"] == "back_to_stage_4"

    def test_no_json_block_returns_none(self):
        assert parse_diagnosis_response("No json block here.") is None

    def test_empty_stdout_returns_none(self):
        assert parse_diagnosis_response("") is None

    def test_none_stdout_returns_none(self):
        assert parse_diagnosis_response(None) is None

    def test_invalid_json_in_block_returns_none(self):
        stdout = "```json\n{not valid json}\n```"
        assert parse_diagnosis_response(stdout) is None

    def test_missing_recommended_action_returns_none(self):
        stdout = self._wrap({"summary": "ok", "findings": []})
        assert parse_diagnosis_response(stdout) is None

    def test_invalid_recommended_action_value_returns_none(self):
        stdout = self._wrap({"recommended_action": "do_something_else", "summary": "bad"})
        assert parse_diagnosis_response(stdout) is None

    def test_findings_preserved(self):
        stdout = self._wrap({"recommended_action": "proceed", "findings": ["f1", "f2"]})
        result = parse_diagnosis_response(stdout)
        assert result["findings"] == ["f1", "f2"]

    def test_summary_preserved(self):
        stdout = self._wrap({"recommended_action": "proceed", "summary": "My summary."})
        result = parse_diagnosis_response(stdout)
        assert result["summary"] == "My summary."


# ---------------------------------------------------------------------------
# (d) rule_based_action
# ---------------------------------------------------------------------------


class TestRuleBasedAction:
    def test_good_metrics_returns_proceed(self):
        metrics = {"base": {"sharpe": 2.0, "max_drawdown": 0.08, "trade_count": 150}}
        assert rule_based_action(metrics) == RecommendedAction.PROCEED

    def test_negative_sharpe_returns_back_to_stage_2(self):
        metrics = {"base": {"sharpe": -0.5, "max_drawdown": 0.20, "trade_count": 80}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_2

    def test_very_few_trades_returns_back_to_stage_4(self):
        """Low trade count is a parameter problem (entry too strict), not concept failure."""
        metrics = {"base": {"sharpe": 1.2, "max_drawdown": 0.05, "trade_count": 5}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_4

    def test_low_sharpe_returns_back_to_stage_4(self):
        metrics = {"base": {"sharpe": 0.5, "max_drawdown": 0.05, "trade_count": 100}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_4

    def test_high_drawdown_returns_back_to_stage_4(self):
        metrics = {"base": {"sharpe": 1.2, "max_drawdown": 0.20, "trade_count": 100}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_4

    def test_low_trade_count_returns_back_to_stage_4(self):
        metrics = {"base": {"sharpe": 1.2, "max_drawdown": 0.05, "trade_count": 30}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_4

    def test_empty_metrics_returns_fallback(self):
        result = rule_based_action({})
        # Should not crash; returns a valid RecommendedAction.
        assert isinstance(result, RecommendedAction)

    def test_none_sharpe_does_not_crash(self):
        metrics = {"base": {"sharpe": None, "max_drawdown": 0.08, "trade_count": 200}}
        result = rule_based_action(metrics)
        assert isinstance(result, RecommendedAction)

    def test_missing_all_values_does_not_crash(self):
        metrics = {"base": {}}
        result = rule_based_action(metrics)
        assert isinstance(result, RecommendedAction)

    def test_exact_boundary_trade_count_19_stage4(self):
        """trade_count=19 → back_to_stage_4 (under the 50 trade gate, tune-able)."""
        metrics = {"base": {"sharpe": 1.5, "max_drawdown": 0.05, "trade_count": 19}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_4

    def test_exact_boundary_trade_count_20_not_stage2(self):
        """trade_count == 20 should NOT trigger back_to_stage_2."""
        metrics = {"base": {"sharpe": 1.5, "max_drawdown": 0.05, "trade_count": 20}}
        result = rule_based_action(metrics)
        assert result != RecommendedAction.BACK_TO_STAGE_2

    def test_zero_sharpe_not_back_to_stage_2(self):
        """sharpe == 0 is not < 0, so should not be back_to_stage_2."""
        metrics = {"base": {"sharpe": 0.0, "max_drawdown": 0.05, "trade_count": 100}}
        result = rule_based_action(metrics)
        assert result != RecommendedAction.BACK_TO_STAGE_2

    def test_negative_drawdown_convention_triggers_stage4(self):
        """Drawdown stored as negative (engine convention) should still trigger back_to_stage_4."""
        metrics = {"base": {"sharpe": 1.2, "max_drawdown": -0.20, "trade_count": 100}}
        assert rule_based_action(metrics) == RecommendedAction.BACK_TO_STAGE_4

    def test_negative_large_drawdown_still_detected(self):
        """Catastrophic negative drawdown (-0.35) should not be diagnosed as proceed."""
        metrics = {"base": {"sharpe": 1.5, "max_drawdown": -0.35, "trade_count": 150}}
        result = rule_based_action(metrics)
        assert result != RecommendedAction.PROCEED


# ---------------------------------------------------------------------------
# (d2) rule_based_action with optimization_metrics override
# ---------------------------------------------------------------------------


class TestRuleBasedActionWithOptimization:
    """When stage-4 best metrics are supplied, routing must reflect the tuned
    combo's edge — not the untuned base_run."""

    def test_negative_base_positive_opt_routes_to_stage_4_not_2(self):
        """Stage-4 already found positive sharpe — base failure is param-only."""
        base = {"base": {"sharpe": -0.3, "max_drawdown": -0.20, "trade_count": 350}}
        opt = {"sharpe": 0.56, "max_drawdown": -0.08, "trade_count": 242}
        assert rule_based_action(base, optimization_metrics=opt) == RecommendedAction.BACK_TO_STAGE_4

    def test_negative_base_strong_opt_proceeds(self):
        """Stage-4 sharpe >= 1.0 with good drawdown / trades → proceed regardless of base."""
        base = {"base": {"sharpe": -0.1, "max_drawdown": -0.10, "trade_count": 100}}
        opt = {"sharpe": 1.6, "max_drawdown": -0.07, "trade_count": 200}
        assert rule_based_action(base, optimization_metrics=opt) == RecommendedAction.PROCEED

    def test_zero_opt_sharpe_falls_through_to_base(self):
        """When stage-4 best sharpe is not positive, the override does NOT kick in
        (the stage-4 evidence is too weak to overturn the base-run verdict)."""
        base = {"base": {"sharpe": -0.5, "max_drawdown": -0.20, "trade_count": 50}}
        opt = {"sharpe": 0.0, "max_drawdown": -0.10, "trade_count": 100}
        # Base sharpe < 0 → would normally route to stage_2; opt sharpe=0 not positive.
        assert rule_based_action(base, optimization_metrics=opt) == RecommendedAction.BACK_TO_STAGE_2

    def test_no_optimization_uses_base_behaviour(self):
        """optimization_metrics=None preserves the pre-existing semantics."""
        base = {"base": {"sharpe": -0.3, "max_drawdown": -0.20, "trade_count": 350}}
        assert rule_based_action(base) == RecommendedAction.BACK_TO_STAGE_2

    def test_opt_low_trades_routes_to_stage_4(self):
        """Even positive stage-4 sharpe with trade_count < 50 still needs more tuning."""
        base = {"base": {"sharpe": 0.5, "max_drawdown": -0.05, "trade_count": 80}}
        opt = {"sharpe": 1.8, "max_drawdown": -0.05, "trade_count": 12}
        assert rule_based_action(base, optimization_metrics=opt) == RecommendedAction.BACK_TO_STAGE_4

    def test_opt_high_drawdown_routes_to_stage_4(self):
        """Stage-4 sharpe high but drawdown > 15% → still needs more tuning."""
        opt = {"sharpe": 1.6, "max_drawdown": -0.22, "trade_count": 200}
        assert rule_based_action({}, optimization_metrics=opt) == RecommendedAction.BACK_TO_STAGE_4


# ---------------------------------------------------------------------------
# (d3) read_optimization_best — disk reader
# ---------------------------------------------------------------------------


class TestReadOptimizationBest:
    def test_missing_optimization_returns_none(self, tmp_path):
        runs_root = tmp_path / "runs"
        assert read_optimization_best(tmp_path / "missing.json", runs_root) is None

    def test_invalid_json_returns_none(self, tmp_path):
        opt_path = tmp_path / "opt.json"
        opt_path.write_text("{not json", encoding="utf-8")
        assert read_optimization_best(opt_path, tmp_path) is None

    def test_missing_source_run_returns_none(self, tmp_path):
        opt_path = tmp_path / "opt.json"
        opt_path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        assert read_optimization_best(opt_path, tmp_path) is None

    def test_referenced_metrics_missing_returns_none(self, tmp_path):
        opt_path = tmp_path / "opt.json"
        opt_path.write_text(json.dumps({"source_run": "nonexistent_run"}), encoding="utf-8")
        assert read_optimization_best(opt_path, tmp_path / "runs") is None

    def test_returns_referenced_metrics(self, tmp_path):
        runs_root = tmp_path / "runs"
        metrics_csv = runs_root / "sweep_033" / "artifacts" / "metrics.csv"
        metrics_csv.parent.mkdir(parents=True, exist_ok=True)
        metrics_csv.write_text(
            "sharpe,max_drawdown,trade_count\n0.561,-0.08,242\n", encoding="utf-8"
        )
        opt_path = tmp_path / "opt.json"
        opt_path.write_text(json.dumps({"source_run": "sweep_033"}), encoding="utf-8")
        result = read_optimization_best(opt_path, runs_root)
        assert result is not None
        assert result["sharpe"] == pytest.approx(0.561)
        assert int(result["trade_count"]) == 242


# ---------------------------------------------------------------------------
# (e) build_diagnosis_block
# ---------------------------------------------------------------------------


class TestBuildDiagnosisBlock:
    def test_basic_structure(self):
        block = build_diagnosis_block(
            strategy_id="btc_s1",
            base_run="btc_s1_base",
            recommended_action=RecommendedAction.PROCEED,
            summary="All good.",
            findings=["sharpe is healthy"],
        )
        assert block["source_run"] == "btc_s1_base"
        assert block["recommended_action"] == "proceed"
        assert block["summary"] == "All good."
        assert block["findings"] == ["sharpe is healthy"]

    def test_none_base_run(self):
        block = build_diagnosis_block(
            strategy_id="s1",
            base_run=None,
            recommended_action=RecommendedAction.BACK_TO_STAGE_2,
            summary=None,
            findings=[],
        )
        assert block["source_run"] is None
        assert block["recommended_action"] == "back_to_stage_2"

    def test_validates_against_diagnosisblock(self):
        block = build_diagnosis_block(
            strategy_id="s1",
            base_run="run_a",
            recommended_action=RecommendedAction.BACK_TO_STAGE_4,
            summary="Needs optimization.",
            findings=["drawdown too high"],
        )
        # Should not raise.
        validated = DiagnosisBlock.model_validate(block)
        assert validated.recommended_action == RecommendedAction.BACK_TO_STAGE_4

    def test_empty_findings_list(self):
        block = build_diagnosis_block(
            strategy_id="s1",
            base_run="run_a",
            recommended_action=RecommendedAction.PROCEED,
            summary=None,
            findings=[],
        )
        assert block["findings"] == []

    def test_multiple_findings(self):
        findings = ["sharpe 2.1", "drawdown 7%", "150 trades"]
        block = build_diagnosis_block(
            strategy_id="s1",
            base_run="run_a",
            recommended_action=RecommendedAction.PROCEED,
            summary="OK",
            findings=findings,
        )
        assert block["findings"] == findings


# ---------------------------------------------------------------------------
# (f) verify_diagnosis
# ---------------------------------------------------------------------------


class TestVerifyDiagnosis:
    def _write_valid_diagnosis(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        block = {
            "source_run": "btc_s1_base",
            "recommended_action": "proceed",
            "summary": "All good.",
            "findings": [],
        }
        path.write_text(json.dumps(block), encoding="utf-8")

    def test_valid_file_returns_ok_true(self, tmp_path):
        path = tmp_path / "s1" / "diagnosis.json"
        self._write_valid_diagnosis(path)
        result = verify_diagnosis(path)
        assert result.ok is True

    def test_missing_file_returns_ok_false(self, tmp_path):
        path = tmp_path / "s1" / "diagnosis.json"
        result = verify_diagnosis(path)
        assert result.ok is False
        assert result.error is not None

    def test_missing_file_error_mentions_path(self, tmp_path):
        path = tmp_path / "s1" / "diagnosis.json"
        result = verify_diagnosis(path)
        assert "diagnosis.json" in result.error

    def test_invalid_json_returns_ok_false(self, tmp_path):
        path = tmp_path / "s1" / "diagnosis.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json}", encoding="utf-8")
        result = verify_diagnosis(path)
        assert result.ok is False

    def test_schema_validation_failure_returns_ok_false(self, tmp_path):
        """A JSON file with an invalid recommended_action fails schema validation."""
        path = tmp_path / "s1" / "diagnosis.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        bad_block = {
            "source_run": "run_a",
            "recommended_action": "invalid_action_xyz",
            "summary": None,
            "findings": [],
        }
        path.write_text(json.dumps(bad_block), encoding="utf-8")
        result = verify_diagnosis(path)
        assert result.ok is False

    def test_extra_field_fails_schema_validation(self, tmp_path):
        """DiagnosisBlock has extra='forbid', so extra fields should fail."""
        path = tmp_path / "s1" / "diagnosis.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        bad_block = {
            "source_run": "run_a",
            "recommended_action": "proceed",
            "summary": None,
            "findings": [],
            "unexpected_extra_field": "boom",
        }
        path.write_text(json.dumps(bad_block), encoding="utf-8")
        result = verify_diagnosis(path)
        assert result.ok is False

    def test_strategy_id_from_parent_dir(self, tmp_path):
        path = tmp_path / "my_strategy_id" / "diagnosis.json"
        self._write_valid_diagnosis(path)
        result = verify_diagnosis(path)
        assert result.strategy_id == "my_strategy_id"


# ---------------------------------------------------------------------------
# (g) compute_exit_code
# ---------------------------------------------------------------------------


class TestComputeExitCode:
    def test_empty_list_returns_1(self):
        assert compute_exit_code([]) == 1

    def test_all_ok_returns_0(self):
        results = [
            DiagnosisCheckResult(strategy_id="s1", ok=True),
            DiagnosisCheckResult(strategy_id="s2", ok=True),
        ]
        assert compute_exit_code(results) == 0

    def test_any_fail_returns_1(self):
        results = [
            DiagnosisCheckResult(strategy_id="s1", ok=True),
            DiagnosisCheckResult(strategy_id="s2", ok=False, error="failed"),
        ]
        assert compute_exit_code(results) == 1

    def test_all_fail_returns_1(self):
        results = [
            DiagnosisCheckResult(strategy_id="s1", ok=False, error="err1"),
            DiagnosisCheckResult(strategy_id="s2", ok=False, error="err2"),
        ]
        assert compute_exit_code(results) == 1

    def test_single_ok_returns_0(self):
        assert compute_exit_code([DiagnosisCheckResult(strategy_id="s1", ok=True)]) == 0

    def test_single_fail_returns_1(self):
        assert compute_exit_code([DiagnosisCheckResult(strategy_id="s1", ok=False, error="x")]) == 1


# ---------------------------------------------------------------------------
# (h) print_summary — smoke tests
# ---------------------------------------------------------------------------


class TestPrintSummary:
    def test_empty_list_no_crash(self, capsys):
        print_summary([])
        captured = capsys.readouterr()
        assert "no strategies" in captured.out.lower() or "0/0" in captured.out

    def test_all_ok_no_crash(self, capsys):
        results = [DiagnosisCheckResult(strategy_id="s1", ok=True)]
        print_summary(results)
        captured = capsys.readouterr()
        assert "s1" in captured.out
        assert "OK" in captured.out

    def test_fail_in_summary(self, capsys):
        results = [DiagnosisCheckResult(strategy_id="s2", ok=False, error="missing")]
        print_summary(results)
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "s2" in captured.out

    def test_passed_count_displayed(self, capsys):
        results = [
            DiagnosisCheckResult(strategy_id="s1", ok=True),
            DiagnosisCheckResult(strategy_id="s2", ok=False, error="err"),
        ]
        print_summary(results)
        captured = capsys.readouterr()
        assert "1/2" in captured.out


# ---------------------------------------------------------------------------
# (i) Integration-level: _diagnose_strategy with mocked subprocess
# ---------------------------------------------------------------------------


class TestDiagnoseStrategyIntegration:
    """Test _diagnose_strategy with mocked run_vibe_trading_diagnose."""

    def _setup_base_run(self, runs_root: Path, run_name: str, metrics: dict) -> Path:
        """Write a metrics.csv for the given run_name."""
        artifacts = runs_root / run_name / "artifacts"
        metrics_csv = artifacts / "metrics.csv"
        _write_metrics_csv(metrics_csv, metrics)
        return metrics_csv

    def test_valid_llm_response_writes_diagnosis(self, tmp_path):
        """LLM returns a valid JSON block -> diagnosis.json is written."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "btc_s1_base", GOOD_METRICS)

        entry = _make_strategy_entry(base_run="btc_s1_base")
        cfg = _make_research_config()

        llm_stdout = (
            "Some preamble\n"
            '```json\n{"recommended_action": "proceed", "summary": "Looks good.", "findings": ["sharpe 2.1"]}\n```\n'
        )

        with patch("pipeline.stage3_diagnose.run_vibe_trading_diagnose", return_value=llm_stdout):
            result = _diagnose_strategy(
                strategy_id="btc_s1_test",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        assert result.ok is True
        diag_path = manifests_dir / "btc_s1_test" / "diagnosis.json"
        assert diag_path.exists()
        data = json.loads(diag_path.read_text())
        assert data["recommended_action"] == "proceed"
        assert data["summary"] == "Looks good."

    def test_invalid_llm_response_falls_back_to_rule_based(self, tmp_path):
        """LLM returns garbage -> rule-based fallback is used."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "btc_s1_base", POOR_METRICS)

        entry = _make_strategy_entry(base_run="btc_s1_base")
        cfg = _make_research_config()

        with patch("pipeline.stage3_diagnose.run_vibe_trading_diagnose", return_value="No JSON here at all"):
            result = _diagnose_strategy(
                strategy_id="btc_s1_test",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        assert result.ok is True
        diag_path = manifests_dir / "btc_s1_test" / "diagnosis.json"
        data = json.loads(diag_path.read_text())
        # With poor metrics (sharpe=-0.3), rule-based should give back_to_stage_2.
        assert data["recommended_action"] == "back_to_stage_2"

    def test_empty_llm_response_falls_back_to_rule_based(self, tmp_path):
        """LLM returns empty string -> rule-based fallback is used."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "btc_s1_base", MEDIOCRE_METRICS)

        entry = _make_strategy_entry(base_run="btc_s1_base")
        cfg = _make_research_config()

        with patch("pipeline.stage3_diagnose.run_vibe_trading_diagnose", return_value=""):
            result = _diagnose_strategy(
                strategy_id="btc_s1_test",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        assert result.ok is True
        diag_path = manifests_dir / "btc_s1_test" / "diagnosis.json"
        data = json.loads(diag_path.read_text())
        # Mediocre metrics (sharpe=0.8, drawdown=0.12, trades=60) -> back_to_stage_4.
        assert data["recommended_action"] == "back_to_stage_4"

    def test_null_base_run_returns_failure(self, tmp_path):
        """entry.base_run is None -> skip with failure result."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"

        entry = _make_strategy_entry(base_run=None)
        cfg = _make_research_config()

        result = _diagnose_strategy(
            strategy_id="btc_s1_test",
            entry=entry,
            cfg=cfg,
            runs_root=runs_root,
            manifests_dir=manifests_dir,
        )
        assert result.ok is False
        assert "base_run" in result.error.lower() or "null" in result.error.lower()

    def test_missing_metrics_csv_returns_failure(self, tmp_path):
        """metrics.csv absent -> failure result."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        # Do NOT create the metrics.csv.

        entry = _make_strategy_entry(base_run="btc_s1_base")
        cfg = _make_research_config()

        result = _diagnose_strategy(
            strategy_id="btc_s1_test",
            entry=entry,
            cfg=cfg,
            runs_root=runs_root,
            manifests_dir=manifests_dir,
        )
        assert result.ok is False
        assert "metrics.csv" in result.error

    def test_oos_run_metrics_collected(self, tmp_path):
        """oos_runs[0] metrics are collected and included in the prompt."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "btc_s1_base", GOOD_METRICS)
        self._setup_base_run(runs_root, "btc_s1_oos", GOOD_METRICS)

        entry = _make_strategy_entry(base_run="btc_s1_base", oos_runs=["btc_s1_oos"])
        cfg = _make_research_config()

        llm_stdout = '```json\n{"recommended_action": "proceed", "summary": "Good.", "findings": []}\n```'

        with patch("pipeline.stage3_diagnose.run_vibe_trading_diagnose", return_value=llm_stdout) as mock_run:
            result = _diagnose_strategy(
                strategy_id="btc_s1_test",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        assert result.ok is True
        # The prompt passed to the LLM should contain both run names.
        call_args = mock_run.call_args[0][0]
        assert "btc_s1_base" in call_args
        assert "btc_s1_oos" in call_args

    def test_diagnosis_json_validates_against_schema(self, tmp_path):
        """The written diagnosis.json must validate against DiagnosisBlock."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "btc_s1_base", GOOD_METRICS)

        entry = _make_strategy_entry(base_run="btc_s1_base")
        cfg = _make_research_config()

        llm_stdout = '```json\n{"recommended_action": "proceed", "summary": "Fine.", "findings": ["f1"]}\n```'

        with patch("pipeline.stage3_diagnose.run_vibe_trading_diagnose", return_value=llm_stdout):
            result = _diagnose_strategy(
                strategy_id="btc_s1_test",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        assert result.ok is True
        diag_path = manifests_dir / "btc_s1_test" / "diagnosis.json"
        raw = diag_path.read_text(encoding="utf-8")
        # Must not raise:
        validated = DiagnosisBlock.model_validate_json(raw)
        assert validated.recommended_action == RecommendedAction.PROCEED

    def test_source_run_matches_base_run(self, tmp_path):
        """diagnosis.json source_run must equal entry.base_run."""
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "my_base_run", GOOD_METRICS)

        entry = _make_strategy_entry(base_run="my_base_run")
        cfg = _make_research_config()

        with patch("pipeline.stage3_diagnose.run_vibe_trading_diagnose", return_value=""):
            _diagnose_strategy(
                strategy_id="s1",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        diag_path = manifests_dir / "s1" / "diagnosis.json"
        data = json.loads(diag_path.read_text())
        assert data["source_run"] == "my_base_run"

    def test_llm_timeout_falls_back_to_rule_based(self, tmp_path):
        """Subprocess timeout -> rule-based fallback (no crash)."""
        import subprocess as _subprocess
        runs_root = tmp_path / "runs"
        manifests_dir = tmp_path / "manifests"
        self._setup_base_run(runs_root, "btc_s1_base", GOOD_METRICS)

        entry = _make_strategy_entry(base_run="btc_s1_base")
        cfg = _make_research_config()

        with patch(
            "pipeline.stage3_diagnose.run_vibe_trading_diagnose",
            side_effect=_subprocess.TimeoutExpired(cmd="vibe-trading", timeout=300),
        ):
            result = _diagnose_strategy(
                strategy_id="btc_s1_test",
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                manifests_dir=manifests_dir,
            )

        assert result.ok is True
        diag_path = manifests_dir / "btc_s1_test" / "diagnosis.json"
        data = json.loads(diag_path.read_text())
        # Rule-based on good metrics -> proceed.
        assert data["recommended_action"] == "proceed"


# ---------------------------------------------------------------------------
# (j) read_walk_forward_metrics — walk-forward holdout loader
# ---------------------------------------------------------------------------


class TestReadWalkForwardMetrics:
    """Read the walk-forward holdout run's metrics, tolerant of every missing
    or unparseable input mode."""

    WF_METRICS = {
        "sharpe": "1.02",
        "max_drawdown": "-0.091",
        "trade_count": "49",
        "total_return": "0.094",
    }

    def test_returns_metrics_when_run_and_file_exist(self, tmp_path):
        runs_root = tmp_path / "runs"
        metrics_csv = runs_root / "eth_s5_holdout" / "artifacts" / "metrics.csv"
        _write_metrics_csv(metrics_csv, self.WF_METRICS)

        entry = {"walk_forward_runs": ["eth_s5_holdout"]}
        result = read_walk_forward_metrics(entry, runs_root)

        assert result is not None
        assert "sharpe" in result
        assert "max_drawdown" in result
        assert "trade_count" in result
        assert result["sharpe"] == pytest.approx(1.02)
        assert result["max_drawdown"] == pytest.approx(-0.091)
        assert int(result["trade_count"]) == 49

    def test_empty_walk_forward_runs_returns_none(self, tmp_path):
        entry = {"walk_forward_runs": []}
        assert read_walk_forward_metrics(entry, tmp_path / "runs") is None

    def test_missing_walk_forward_runs_key_returns_none(self, tmp_path):
        entry = {"base_run": "btc_s1_base"}  # no walk_forward_runs key at all
        assert read_walk_forward_metrics(entry, tmp_path / "runs") is None

    def test_nonexistent_run_dir_returns_none(self, tmp_path):
        runs_root = tmp_path / "runs"
        # runs_root itself is never created.
        entry = {"walk_forward_runs": ["does_not_exist_run"]}
        assert read_walk_forward_metrics(entry, runs_root) is None

    def test_run_dir_exists_but_metrics_csv_missing_returns_none(self, tmp_path):
        runs_root = tmp_path / "runs"
        artifacts = runs_root / "eth_s5_holdout" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        # Intentionally do not create metrics.csv.

        entry = {"walk_forward_runs": ["eth_s5_holdout"]}
        assert read_walk_forward_metrics(entry, runs_root) is None


# ---------------------------------------------------------------------------
# (j) build_diagnosis_prompt with walk-forward (OOS-aware)
# ---------------------------------------------------------------------------


class TestBuildPromptWithWalkForward:
    """Prompt builder must change shape when walk-forward metrics are provided."""

    def _train_metrics(self) -> dict:
        return {"base_run": {"sharpe": 1.55, "max_drawdown": -0.38, "trade_count": 120}}

    def _wf_metrics(self) -> dict:
        return {"sharpe": 1.02, "max_drawdown": -0.091, "trade_count": 49}

    def test_walk_forward_present_marks_oos_authoritative(self):
        """Non-None walk_forward → prompt contains 'Walk-Forward' + 'OOS' + 'authoritative'."""
        prompt = build_diagnosis_prompt(
            "eth_s5_half_size",
            self._train_metrics(),
            walk_forward_metrics=self._wf_metrics(),
        )
        assert "Walk-Forward" in prompt
        assert "OOS" in prompt
        # 'authoritative' check is case-insensitive (may appear as
        # 'AUTHORITATIVE' in section header).
        assert "authoritative" in prompt.lower()

    def test_walk_forward_none_omits_oos_section(self):
        """None walk_forward → prompt MUST NOT mention Walk-Forward / authoritative."""
        prompt = build_diagnosis_prompt(
            "eth_s5_half_size",
            self._train_metrics(),
            walk_forward_metrics=None,
        )
        assert "Walk-Forward" not in prompt
        assert "authoritative" not in prompt.lower()

    def test_walk_forward_values_appear_in_prompt(self):
        """walk_forward dict values (sharpe / dd / trade_count) appear in prompt JSON."""
        wf = {"sharpe": 1.02, "max_drawdown": -0.091, "trade_count": 49}
        prompt = build_diagnosis_prompt(
            "eth_s5_half_size",
            self._train_metrics(),
            walk_forward_metrics=wf,
        )
        assert "1.02" in prompt
        assert "-0.091" in prompt
        assert "49" in prompt

    def test_task_instruction_mentions_oos_authoritative(self):
        """Task block must explicitly say OOS is authoritative + train is for overfit detection."""
        prompt = build_diagnosis_prompt(
            "eth_s5_half_size",
            self._train_metrics(),
            walk_forward_metrics=self._wf_metrics(),
        )
        lower = prompt.lower()
        # Task block must signal both the verdict authority and the overfit role.
        assert "authoritative" in lower
        assert "overfit" in lower

    def test_walk_forward_with_optimization_still_renders_both(self):
        """When both opt and wf provided, prompt contains both sections + OOS authority."""
        opt = {"sharpe": 1.4, "max_drawdown": -0.12, "trade_count": 80}
        prompt = build_diagnosis_prompt(
            "eth_s5_half_size",
            self._train_metrics(),
            optimization_metrics=opt,
            walk_forward_metrics=self._wf_metrics(),
        )
        assert "Stage-4 Best Combo" in prompt
        assert "Walk-Forward" in prompt
        assert "authoritative" in prompt.lower()


# ---------------------------------------------------------------------------
# (k) rule_based_action with walk-forward (OOS-aware)
# ---------------------------------------------------------------------------


class TestRuleBasedActionOOS:
    """When walk_forward_metrics is provided, OOS path is authoritative."""

    def _train_pass(self) -> dict:
        """Train metrics that on their own would return PROCEED — to prove OOS overrides."""
        return {"base": {"sharpe": 2.5, "max_drawdown": -0.05, "trade_count": 200}}

    def test_oos_good_metrics_proceed(self):
        """wf sharpe 1.02 / dd -0.091 / trades 49 → PROCEED (eth_s5 case)."""
        wf = {"sharpe": 1.02, "max_drawdown": -0.091, "trade_count": 49}
        result = rule_based_action({}, walk_forward_metrics=wf)
        assert result == RecommendedAction.PROCEED

    def test_oos_low_sharpe_or_high_dd_routes_to_stage_4(self):
        """wf sharpe 0.25 / dd -0.26 / trades 89 → BACK_TO_STAGE_4."""
        wf = {"sharpe": 0.25, "max_drawdown": -0.26, "trade_count": 89}
        result = rule_based_action({}, walk_forward_metrics=wf)
        assert result == RecommendedAction.BACK_TO_STAGE_4

    def test_oos_negative_sharpe_routes_to_stage_2(self):
        """wf sharpe -4.16 → BACK_TO_STAGE_2 (concept-level OOS failure)."""
        wf = {"sharpe": -4.16, "max_drawdown": -0.50, "trade_count": 60}
        result = rule_based_action({}, walk_forward_metrics=wf)
        assert result == RecommendedAction.BACK_TO_STAGE_2

    def test_walk_forward_none_preserves_train_path(self):
        """walk_forward_metrics=None → train-anchored path unchanged (backwards compat)."""
        # Reuse a scenario from TestRuleBasedAction: bad train → BACK_TO_STAGE_2.
        bad_train = {"base": {"sharpe": -0.5, "max_drawdown": 0.20, "trade_count": 80}}
        assert (
            rule_based_action(bad_train, walk_forward_metrics=None)
            == RecommendedAction.BACK_TO_STAGE_2
        )
        # And a good-train scenario remains PROCEED.
        good_train = {"base": {"sharpe": 2.0, "max_drawdown": 0.08, "trade_count": 150}}
        assert (
            rule_based_action(good_train, walk_forward_metrics=None)
            == RecommendedAction.PROCEED
        )

    def test_oos_trade_gate_at_30_inclusive(self):
        """wf sharpe 1.10 / trades 35 → PROCEED (35 ≥ 30 gate, looser than train's 50)."""
        wf = {"sharpe": 1.10, "max_drawdown": -0.08, "trade_count": 35}
        result = rule_based_action({}, walk_forward_metrics=wf)
        assert result == RecommendedAction.PROCEED

    def test_oos_overrides_train(self):
        """When wf is bad, train being great must NOT save it — OOS is authoritative."""
        wf_bad = {"sharpe": -1.0, "max_drawdown": -0.4, "trade_count": 40}
        result = rule_based_action(self._train_pass(), walk_forward_metrics=wf_bad)
        assert result == RecommendedAction.BACK_TO_STAGE_2

    def test_oos_overrides_optimization(self):
        """Even with strong stage-4 opt metrics, weak OOS wins."""
        opt = {"sharpe": 1.8, "max_drawdown": -0.09, "trade_count": 150}
        wf_weak = {"sharpe": 0.4, "max_drawdown": -0.08, "trade_count": 60}
        result = rule_based_action(
            self._train_pass(),
            optimization_metrics=opt,
            walk_forward_metrics=wf_weak,
        )
        assert result == RecommendedAction.BACK_TO_STAGE_4

    def test_oos_trade_below_30_routes_to_stage_4(self):
        """Edge: wf trades 29 < 30 → BACK_TO_STAGE_4 even with good sharpe/dd."""
        wf = {"sharpe": 1.5, "max_drawdown": -0.08, "trade_count": 29}
        result = rule_based_action({}, walk_forward_metrics=wf)
        assert result == RecommendedAction.BACK_TO_STAGE_4
