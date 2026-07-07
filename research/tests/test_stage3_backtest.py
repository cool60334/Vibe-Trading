"""
Tests for research/pipeline/stage3_backtest.py pure-logic helpers.

TDD: these tests are written BEFORE the implementation.

Stage 3 calls subprocess (backtest runner) and filesystem I/O — none of that
is tested via actual processes. Only the pure, deterministic logic is exercised:

  (a) build_run_config()             — correct config.json dict for a run
  (b) find_signal_engine()           — returns path if source exists, else None
  (c) build_stub_signal_engine()     — valid Python with SignalEngine.generate
  (d) stub passes AST validator      — agent/backtest/runner._validate_signal_engine_source
  (e) verify_run_artifacts()         — checks artifacts/ dir has at least one .csv
  (f) compute_exit_code()            — 0 on full success, 1 on any failure
  (g) list_pending_runs()            — parses strategy_runs.json entries correctly
  (h) symbol_to_short()              — "BTC-USDT-SWAP" -> "btc"

Pytest is run from research/ as:
    cd research && python -m pytest tests/
"""

from __future__ import annotations

import ast
import dataclasses
import json
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Bootstrap: research/ must be on sys.path.
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_DIR = _THIS_FILE.parents[1]   # research/
_REPO_ROOT = _RESEARCH_DIR.parent       # repo root

for _p in (_RESEARCH_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from pipeline.stage3_backtest import (   # noqa: E402
    BacktestRunResult,
    _run_stress_for_strategy,
    _setup_run_dir,
    build_run_config,
    build_stub_signal_engine,
    check_archetype_misfit,
    compute_exit_code,
    find_signal_engine,
    list_pending_runs,
    print_summary,
    stress_run_plan,
    symbol_to_short,
    verify_run_artifacts,
    window_is_valid,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_research_config(period: int = 730, interval: str = "1H"):
    """Return a minimal ResearchConfig-like namespace for tests."""
    from pipeline.config import ResearchConfig, SymbolConfig, FeesConfig
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
):
    """Return a minimal StrategyRunsEntry-like object."""
    from pipeline.strategy_runs import StrategyRunsEntry
    return StrategyRunsEntry(
        symbol=symbol,
        spec_yaml="research/strategies/strategy_S1.yaml",
        base_run=base_run,
        regime_runs=types.MappingProxyType(regime_runs or {}),
        stress_runs=types.MappingProxyType({}),
        sweep_run=None,
    )


# ---------------------------------------------------------------------------
# (a) build_run_config
# ---------------------------------------------------------------------------

class TestBuildRunConfig:
    """build_run_config(symbol, cfg) -> dict conforming to BacktestConfigSchema."""

    def test_base_run_structure(self):
        cfg = _make_research_config(period=730, interval="1H")
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        assert "codes" in result
        assert "start_date" in result
        assert "end_date" in result
        assert "source" in result
        assert "interval" in result
        assert "engine" in result

    def test_codes_contains_symbol(self):
        cfg = _make_research_config()
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        assert result["codes"] == ["BTC-USDT-SWAP"]

    def test_source_always_okx(self):
        cfg = _make_research_config()
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        assert result["source"] == "okx"

    def test_engine_always_daily(self):
        cfg = _make_research_config()
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        assert result["engine"] == "daily"

    def test_interval_from_config(self):
        cfg = _make_research_config(interval="4H")
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        assert result["interval"] == "4H"

    def test_end_date_is_today(self):
        fixed_today = date(2024, 1, 15)
        cfg = _make_research_config(period=730)
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg, today=fixed_today)
        assert result["end_date"] == fixed_today.isoformat()

    def test_start_date_is_period_days_before_today(self):
        period = 730
        fixed_today = date(2024, 1, 15)
        cfg = _make_research_config(period=period)
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg, today=fixed_today)
        expected_start = (fixed_today - timedelta(days=period)).isoformat()
        assert result["start_date"] == expected_start

    def test_oos_run_config_structure(self):
        """OOS run config uses the same schema; structure must be identical."""
        cfg = _make_research_config(period=365, interval="1H")
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        for key in ("codes", "start_date", "end_date", "source", "interval", "engine"):
            assert key in result, f"Missing key: {key}"

    def test_start_date_before_end_date(self):
        cfg = _make_research_config(period=730)
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        assert result["start_date"] < result["end_date"]


# ---------------------------------------------------------------------------
# (b) find_signal_engine
# ---------------------------------------------------------------------------

class TestFindSignalEngine:
    """find_signal_engine(strategies_code_dir, strategy_id) -> Path | None."""

    def test_found(self, tmp_path):
        strategy_id = "btc_s1_test"
        code_dir = tmp_path / "strategies" / "code" / strategy_id
        code_dir.mkdir(parents=True)
        se_file = code_dir / "signal_engine.py"
        se_file.write_text("class SignalEngine:\n    def generate(self, data_map): return {}\n")
        result = find_signal_engine(tmp_path / "strategies" / "code", strategy_id)
        assert result == se_file

    def test_not_found(self, tmp_path):
        result = find_signal_engine(tmp_path / "strategies" / "code", "btc_s1_nonexistent")
        assert result is None

    def test_returns_path_object(self, tmp_path):
        strategy_id = "btc_s1_test"
        code_dir = tmp_path / "strategies" / "code" / strategy_id
        code_dir.mkdir(parents=True)
        se_file = code_dir / "signal_engine.py"
        se_file.write_text("class SignalEngine: pass\n")
        result = find_signal_engine(tmp_path / "strategies" / "code", strategy_id)
        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# (c) build_stub_signal_engine
# ---------------------------------------------------------------------------

class TestBuildStubSignalEngine:
    """build_stub_signal_engine() -> str with valid Python."""

    def test_returns_string(self):
        result = build_stub_signal_engine()
        assert isinstance(result, str)

    def test_valid_python_syntax(self):
        source = build_stub_signal_engine()
        # Must not raise SyntaxError
        tree = ast.parse(source)
        assert tree is not None

    def test_contains_signal_engine_class(self):
        source = build_stub_signal_engine()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SignalEngine" in class_names

    def test_contains_generate_method(self):
        source = build_stub_signal_engine()
        tree = ast.parse(source)
        method_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        assert "generate" in method_names

    def test_generate_takes_data_map_param(self):
        source = build_stub_signal_engine()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "generate":
                arg_names = [a.arg for a in node.args.args]
                assert "data_map" in arg_names
                return
        pytest.fail("generate() method not found")


# ---------------------------------------------------------------------------
# (d) stub passes AST validator
# ---------------------------------------------------------------------------

class TestStubPassesASTValidator:
    """Confirm build_stub_signal_engine() is accepted by runner._validate_signal_engine_source."""

    def test_stub_passes_validator(self, tmp_path):
        # Add agent/ to sys.path for this test
        agent_dir = _REPO_ROOT / "agent"
        agent_dir_str = str(agent_dir)
        had_agent = agent_dir_str in sys.path
        if not had_agent:
            sys.path.insert(0, agent_dir_str)
        try:
            try:
                from backtest.runner import _validate_signal_engine_source
            except ImportError:
                pytest.skip("agent/backtest/runner not available")
            stub_source = build_stub_signal_engine()
            se_file = tmp_path / "signal_engine.py"
            se_file.write_text(stub_source, encoding="utf-8")
            # Must not raise
            _validate_signal_engine_source(se_file)
        finally:
            if not had_agent and agent_dir_str in sys.path:
                sys.path.remove(agent_dir_str)


# ---------------------------------------------------------------------------
# (e) verify_run_artifacts
# ---------------------------------------------------------------------------

class TestVerifyRunArtifacts:
    """verify_run_artifacts(run_dir) -> BacktestRunResult."""

    def test_pass_with_csv(self, tmp_path):
        run_dir = tmp_path / "btc_s1_base"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "metrics.csv").write_text("sharpe,0.5\n")
        result = verify_run_artifacts(run_dir)
        assert result.ok is True

    def test_fail_no_artifacts_dir(self, tmp_path):
        run_dir = tmp_path / "btc_s1_base"
        run_dir.mkdir(parents=True)
        result = verify_run_artifacts(run_dir)
        assert result.ok is False
        assert "artifacts" in result.error.lower() or result.error

    def test_fail_empty_artifacts_dir(self, tmp_path):
        run_dir = tmp_path / "btc_s1_base"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        # No files in artifacts
        result = verify_run_artifacts(run_dir)
        assert result.ok is False

    def test_fail_no_csv_in_artifacts(self, tmp_path):
        run_dir = tmp_path / "btc_s1_base"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "notes.txt").write_text("no csv here")
        result = verify_run_artifacts(run_dir)
        assert result.ok is False

    def test_window_valid_true_for_ordered_dates(self):
        assert window_is_valid({"start_date": "2022-06-11", "end_date": "2025-01-01"}) is True

    def test_window_valid_false_for_inverted_dates(self):
        # regime span entirely after the train cap -> start > end after clipping
        assert window_is_valid({"start_date": "2026-01-15", "end_date": "2025-01-01"}) is False

    def test_window_valid_true_when_equal(self):
        assert window_is_valid({"start_date": "2025-01-01", "end_date": "2025-01-01"}) is True

    def test_result_contains_run_name(self, tmp_path):
        run_dir = tmp_path / "btc_s1_base"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "metrics.csv").write_text("sharpe,0.5\n")
        result = verify_run_artifacts(run_dir)
        assert result.run_name == "btc_s1_base"


# ---------------------------------------------------------------------------
# (e2) _setup_run_dir clears stale artifacts (stale-PASS guard)
# ---------------------------------------------------------------------------

class TestSetupRunDirClearsStaleArtifacts:
    """_setup_run_dir must wipe a prior run's artifacts/ before re-running.

    Bug: _setup_run_dir did mkdir(exist_ok=True) and never cleared artifacts/.
    If the backtest runner then failed AFTER setup, verify_run_artifacts() would
    find the PREVIOUS run's stale .csv and report a false PASS.
    """

    def test_clears_stale_artifacts(self, tmp_path, monkeypatch):
        # _setup_run_dir prints paths relative to _REPO_ROOT; point it at tmp_path
        # so the cosmetic logging does not crash on an out-of-repo path.
        monkeypatch.setattr("pipeline.stage3_backtest._REPO_ROOT", tmp_path)
        run_dir = tmp_path / "btc_s1_base"
        stale = run_dir / "artifacts"
        stale.mkdir(parents=True)
        (stale / "metrics.csv").write_text("sharpe,9.9\n")  # leftover from a prior run

        code_src = tmp_path / "code_src"  # no signal_engine for this id -> stub path
        _setup_run_dir(run_dir, {"codes": ["BTC-USDT-SWAP"]}, code_src, "btc_test")

        # Stale artifact must be gone so a later-failing runner can't falsely PASS.
        assert not (run_dir / "artifacts" / "metrics.csv").exists()

    def test_still_writes_config_and_signal_engine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pipeline.stage3_backtest._REPO_ROOT", tmp_path)
        run_dir = tmp_path / "btc_s1_base"
        code_src = tmp_path / "code_src"
        _setup_run_dir(run_dir, {"codes": ["BTC-USDT-SWAP"]}, code_src, "btc_test")

        assert (run_dir / "config.json").exists()
        assert (run_dir / "code" / "signal_engine.py").exists()


# ---------------------------------------------------------------------------
# (f) compute_exit_code
# ---------------------------------------------------------------------------

class TestComputeExitCode:
    """compute_exit_code(results) -> 0 on success, 1 on any failure."""

    def _ok_result(self, name: str = "btc_s1_base") -> BacktestRunResult:
        return BacktestRunResult(run_name=name, ok=True, error=None)

    def _fail_result(self, name: str = "btc_s1_base") -> BacktestRunResult:
        return BacktestRunResult(run_name=name, ok=False, error="no artifacts")

    def test_all_ok_returns_0(self):
        results = [self._ok_result("run1"), self._ok_result("run2")]
        assert compute_exit_code(results) == 0

    def test_any_fail_returns_1(self):
        results = [self._ok_result("run1"), self._fail_result("run2")]
        assert compute_exit_code(results) == 1

    def test_all_fail_returns_1(self):
        results = [self._fail_result("run1"), self._fail_result("run2")]
        assert compute_exit_code(results) == 1

    def test_empty_returns_1(self):
        assert compute_exit_code([]) == 1

    def test_single_ok_returns_0(self):
        assert compute_exit_code([self._ok_result()]) == 0

    def test_single_fail_returns_1(self):
        assert compute_exit_code([self._fail_result()]) == 1


# ---------------------------------------------------------------------------
# (g) list_pending_runs
# ---------------------------------------------------------------------------

class TestListPendingRuns:
    """list_pending_runs(strategy_id, entry) -> list of (run_name, strategy_id, symbol) tuples."""

    def test_base_run_included(self):
        entry = _make_strategy_entry(base_run="btc_s1_base")
        runs = list_pending_runs("btc_s1_test", entry)
        run_names = [r[0] for r in runs]
        assert "btc_s1_base" in run_names

    def test_null_base_run_skipped(self):
        entry = _make_strategy_entry(base_run=None)
        runs = list_pending_runs("btc_s1_test", entry)
        # None should not appear
        for run_name, *_ in runs:
            assert run_name is not None

    def test_regime_runs_included(self):
        entry = _make_strategy_entry(
            base_run=None,
            regime_runs={"bull": "btc_s1_bull", "bear": "btc_s1_bear"},
        )
        runs = list_pending_runs("btc_s1_test", entry)
        run_names = [r[0] for r in runs]
        assert "btc_s1_bull" in run_names
        assert "btc_s1_bear" in run_names

    def test_stress_and_sweep_not_included(self):
        """stress_runs and sweep_run are NOT processed by stage3."""
        from pipeline.strategy_runs import StrategyRunsEntry
        entry = StrategyRunsEntry(
            symbol="BTC-USDT-SWAP",
            spec_yaml="research/strategies/strategy_S1.yaml",
            base_run=None,
            regime_runs=types.MappingProxyType({}),
            stress_runs=types.MappingProxyType({"3x_fees": "btc_s1_base_stress"}),
            sweep_run="btc_s1_sweep",
        )
        runs = list_pending_runs("btc_s1_test", entry)
        run_names = [r[0] for r in runs]
        assert "btc_s1_base_stress" not in run_names
        assert "btc_s1_sweep" not in run_names

    def test_each_run_tuple_has_strategy_id_and_symbol(self):
        entry = _make_strategy_entry(base_run="btc_s1_base", symbol="BTC-USDT-SWAP")
        runs = list_pending_runs("btc_s1_test", entry)
        for run_name, strategy_id, symbol, _role in runs:
            assert strategy_id == "btc_s1_test"
            assert symbol == "BTC-USDT-SWAP"

    def test_mixed_runs_included(self):
        entry = _make_strategy_entry(
            base_run="btc_s1_base",
            regime_runs={"bull": "btc_s1_bull"},
        )
        runs = list_pending_runs("btc_s1_test", entry)
        run_names = [r[0] for r in runs]
        assert "btc_s1_base" in run_names
        assert "btc_s1_bull" in run_names
        assert len(run_names) == 2


# ---------------------------------------------------------------------------
# (h) symbol_to_short
# ---------------------------------------------------------------------------

class TestSymbolToShort:
    """symbol_to_short(symbol) -> short lowercase name."""

    def test_btc_usdt_swap(self):
        assert symbol_to_short("BTC-USDT-SWAP") == "btc"

    def test_eth_usdt_swap(self):
        assert symbol_to_short("ETH-USDT-SWAP") == "eth"

    def test_sol_usdt_swap(self):
        assert symbol_to_short("SOL-USDT-SWAP") == "sol"

    def test_lowercase_input(self):
        assert symbol_to_short("btc-usdt-swap") == "btc"

    def test_returns_lowercase(self):
        result = symbol_to_short("BTC-USDT-SWAP")
        assert result == result.lower()

    def test_symbol_to_short_no_hyphen(self):
        assert symbol_to_short("BTC") == "btc"


# ---------------------------------------------------------------------------
# (i) check_archetype_misfit (fail-fast guard after base run)
# ---------------------------------------------------------------------------

class TestArchetypeMisfitGuard:
    """check_archetype_misfit(run_dir) -> bool.

    Returns True when base run is hopeless (sharpe < -2 OR trades_per_year > 1000).
    Returns False (fail-open) when metrics are absent.
    """

    def _write_metrics(self, run_dir: Path, sharpe: float, trade_count: int,
                       start: str = "2023-01-01", end: str = "2024-01-01") -> None:
        """Write minimal metrics.csv and config.json to a run directory."""
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        header = "final_value,total_return,annual_return,max_drawdown,sharpe,calmar,sortino,win_rate,profit_loss_ratio,profit_factor,max_consecutive_loss,avg_holding_days,trade_count,benchmark_return,excess_return,information_ratio"
        row = f"1000.0,0.1,0.1,-0.05,{sharpe},1.0,1.2,0.55,1.1,1.2,3,2.0,{trade_count},0.05,0.05,0.5"
        (artifacts / "metrics.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")
        config = {
            "codes": ["BTC-USDT-SWAP"],
            "start_date": start,
            "end_date": end,
            "source": "okx",
            "interval": "1H",
            "engine": "daily",
        }
        (run_dir / "config.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    def test_guard_fires_negative_sharpe(self, tmp_path):
        """sharpe < -2 => archetype_misfit=True."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        self._write_metrics(run_dir, sharpe=-3.0, trade_count=50)
        assert check_archetype_misfit(run_dir) is True

    def test_guard_fires_high_trades(self, tmp_path):
        """trades_per_year > 1000 => archetype_misfit=True."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        # 1 year window, 1500 trades => 1500 per year
        self._write_metrics(run_dir, sharpe=0.5, trade_count=1500,
                            start="2023-01-01", end="2024-01-01")
        assert check_archetype_misfit(run_dir) is True

    def test_guard_passes_normal(self, tmp_path):
        """sharpe=0.5, trades=80 => archetype_misfit=False."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        self._write_metrics(run_dir, sharpe=0.5, trade_count=80)
        assert check_archetype_misfit(run_dir) is False

    def test_guard_passes_negative_but_within_threshold(self, tmp_path):
        """sharpe=-1.5 (not < -2) => archetype_misfit=False."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        self._write_metrics(run_dir, sharpe=-1.5, trade_count=50)
        assert check_archetype_misfit(run_dir) is False

    def test_guard_passes_many_trades_not_over_1000(self, tmp_path):
        """999 trades/year => archetype_misfit=False."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        # 1 year window, 999 trades => 999 per year
        self._write_metrics(run_dir, sharpe=0.5, trade_count=999,
                            start="2023-01-01", end="2024-01-01")
        assert check_archetype_misfit(run_dir) is False

    def test_guard_passes_missing_metrics(self, tmp_path):
        """Missing metrics => fail-open (not misfit), do not skip runs."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        # No artifacts dir, no metrics.csv
        assert check_archetype_misfit(run_dir) is False

    def test_guard_passes_missing_artifacts_dir(self, tmp_path):
        """artifacts/ dir absent => fail-open."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        assert check_archetype_misfit(run_dir) is False

    def test_guard_fires_exact_boundary_sharpe(self, tmp_path):
        """sharpe exactly -2 is NOT a misfit (guard fires on strictly < -2)."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        self._write_metrics(run_dir, sharpe=-2.0, trade_count=50)
        assert check_archetype_misfit(run_dir) is False

    def test_guard_fires_exact_boundary_trades(self, tmp_path):
        """trades_per_year exactly 1000 is NOT a misfit (guard fires on strictly > 1000)."""
        run_dir = tmp_path / "btc_test_base"
        run_dir.mkdir()
        # 2 year window (~730 days), 2000 trades => ~1000/year => NOT misfit
        self._write_metrics(run_dir, sharpe=0.5, trade_count=2000,
                            start="2022-01-01", end="2024-01-01")
        # 2000 trades / (730/365.25 years) ≈ 1000.68 => still slightly over;
        # use 1990 trades (< 1000/yr) to confirm boundary is not misfit
        (run_dir / "artifacts" / "metrics.csv").write_text(
            "final_value,total_return,annual_return,max_drawdown,sharpe,calmar,sortino,"
            "win_rate,profit_loss_ratio,profit_factor,max_consecutive_loss,avg_holding_days,"
            "trade_count,benchmark_return,excess_return,information_ratio\n"
            "1000.0,0.1,0.1,-0.05,0.5,1.0,1.2,0.55,1.1,1.2,3,2.0,1990,0.05,0.05,0.5\n",
            encoding="utf-8",
        )
        # 1990 trades / (730/365.25 ≈ 1.998 years) ≈ 996 trades/year => not misfit
        assert check_archetype_misfit(run_dir) is False


# ---------------------------------------------------------------------------
# (i) build_run_config with fee_multiplier
# ---------------------------------------------------------------------------

class TestBuildRunConfigFees:
    """build_run_config(symbol, cfg, fee_multiplier=) -> dict with scaled fees."""

    def _cfg(self):
        return _make_research_config(period=730, interval="1H")

    def test_no_multiplier_realistic_default_has_fee_keys(self):
        """A1 cost-model default (legacy_costs unset): realistic-cost keys are
        wired from cfg.fees even with no fee_multiplier — this is the pipeline
        opting into the Tasks 1-2 realistic cost model by default, independent
        of the fee_multiplier stress path. 'funding_rate' is a stress-only key
        (from DEFAULT_FEES) and is NOT part of the realistic-cost block, so it
        must still be absent here.
        """
        cfg = self._cfg()
        c = build_run_config("BTC-USDT-SWAP", cfg, today=date(2026, 1, 1))
        assert c["maker_rate"] == cfg.fees.maker_rate
        assert c["taker_rate"] == cfg.fees.taker_rate
        assert c["slippage"] == cfg.fees.slippage
        assert "funding_rate" not in c
        assert c["cost_model_version"] == "v2_realistic"

    def test_no_multiplier_legacy_costs_has_no_fee_keys(self):
        """legacy_costs=True suppresses all realistic-cost keys even with no
        fee_multiplier, so the engine falls back to its own hardcoded defaults."""
        cfg = self._cfg()
        object.__setattr__(cfg, "legacy_costs", True)
        c = build_run_config("BTC-USDT-SWAP", cfg, today=date(2026, 1, 1))
        for k in ("maker_rate", "taker_rate", "slippage", "funding_rate"):
            assert k not in c
        assert c["cost_model_version"] == "v1_legacy"

    def test_multiplier_3x_adds_scaled_fees(self):
        """When fee_multiplier=3.0, all fee keys are scaled by 3x.

        Under the realistic-cost default (legacy_costs unset), the realistic
        block scales cfg.fees (the config-driven rate) by the stress
        multiplier, not the engine's hardcoded DEFAULT_FEES baseline —
        otherwise the realistic block would silently overwrite the
        multiplier's effect back to the unstressed base rate. 'funding_rate'
        has no cfg.fees equivalent, so it still comes from DEFAULT_FEES.
        """
        cfg = self._cfg()
        c = build_run_config("BTC-USDT-SWAP", cfg, today=date(2026, 1, 1), fee_multiplier=3.0)
        assert c["taker_rate"] == cfg.fees.taker_rate * 3.0
        assert c["maker_rate"] == cfg.fees.maker_rate * 3.0
        assert c["slippage"] == cfg.fees.slippage * 3.0
        assert c["funding_rate"] == 0.0001 * 3.0


# ---------------------------------------------------------------------------
# (j) stress_run_plan
# ---------------------------------------------------------------------------

class TestStressRunPlan:
    def test_split_yields_train_and_oos_each_2x_3x(self):
        cfg = _make_research_config(period=730, interval="1H")
        cfg = dataclasses.replace(cfg, oos_start="2025-01-01")  # enable walk-forward split
        plan = stress_run_plan("eth_s5_half_size", cfg, today=date(2026, 1, 1))
        names = [p[0] for p in plan]
        labels = [p[1] for p in plan]
        assert names == [
            "eth_s5_half_size_stress_train_2x",
            "eth_s5_half_size_stress_train_3x",
            "eth_s5_half_size_stress_oos_2x",
            "eth_s5_half_size_stress_oos_3x",
        ]
        assert labels == ["2x_fees_train", "3x_fees_train", "2x_fees_oos", "3x_fees_oos"]
        # multiplier in tuple position 3
        assert [p[3] for p in plan] == [2.0, 3.0, 2.0, 3.0]

    def test_no_split_yields_full_window_only(self):
        cfg = _make_research_config(period=730, interval="1H")
        cfg = dataclasses.replace(cfg, oos_start=None)
        plan = stress_run_plan("btc_s9", cfg, today=date(2026, 1, 1))
        assert [p[0] for p in plan] == ["btc_s9_stress_full_2x", "btc_s9_stress_full_3x"]
        assert [p[1] for p in plan] == ["2x_fees_full", "3x_fees_full"]

    def test_labels_parse_back_to_multiplier(self):
        import re
        cfg = dataclasses.replace(_make_research_config(), oos_start="2025-01-01")
        for _name, label, _window, mult in stress_run_plan("x", cfg, today=date(2026, 1, 1)):
            m = re.search(r"(\d+(?:\.\d+)?)x", label.lower())
            assert m and float(m.group(1)) == mult


class TestLagStressRunPlan:
    def test_split_yields_train_and_oos_each_lag(self):
        from pipeline.stage3_backtest import lag_stress_run_plan
        cfg = _make_research_config(period=730, interval="1H")
        cfg = dataclasses.replace(cfg, oos_start="2025-01-01", lag_stress_bars=(1, 2))
        plan = lag_stress_run_plan("eth_s5_half_size", cfg, today=date(2026, 1, 1))
        assert plan == [
            ("eth_s5_half_size_lagstress_train_lag1", "lag1_train", "train", 1),
            ("eth_s5_half_size_lagstress_train_lag2", "lag2_train", "train", 2),
            ("eth_s5_half_size_lagstress_oos_lag1", "lag1_oos", "oos", 1),
            ("eth_s5_half_size_lagstress_oos_lag2", "lag2_oos", "oos", 2),
        ]

    def test_no_split_yields_full_window_only(self):
        from pipeline.stage3_backtest import lag_stress_run_plan
        cfg = _make_research_config(period=730, interval="1H")
        cfg = dataclasses.replace(cfg, oos_start=None)
        plan = lag_stress_run_plan("btc_s9", cfg, today=date(2026, 1, 1))
        assert len(plan) == len(cfg.lag_stress_bars)
        assert all(p[2] == "full" for p in plan)
        assert [p[0] for p in plan] == [
            f"btc_s9_lagstress_full_lag{lag}" for lag in cfg.lag_stress_bars
        ]
        assert [p[1] for p in plan] == [
            f"lag{lag}_full" for lag in cfg.lag_stress_bars
        ]


class TestPrintSummarySkippedWindow:
    """A window-skipped run must render [SKIP], not [OK], while ok stays True."""

    def test_skipped_window_renders_skip_not_ok(self, capsys):
        results = [BacktestRunResult(run_name="btc_s1_bear", ok=True, skipped_window=True)]
        print_summary(results)
        out = capsys.readouterr().out
        assert "[SKIP]" in out
        assert "[OK] btc_s1_bear" not in out

    def test_skipped_window_keeps_ok_true_for_exit_code(self):
        r = BacktestRunResult(run_name="btc_s1_bear", ok=True, skipped_window=True)
        assert r.ok is True
        assert compute_exit_code([r]) == 0

    def test_plain_ok_still_renders_ok(self, capsys):
        print_summary([BacktestRunResult(run_name="btc_s1_base", ok=True)])
        out = capsys.readouterr().out
        assert "[OK] btc_s1_base" in out


class TestRunStressForStrategy:
    def test_registers_successful_stress_runs(self, tmp_path, monkeypatch):
        import subprocess
        from pipeline import stage3_backtest as s3

        # real strategy_runs.json so update_stress_runs can write
        payload = {
            "btc_s9": {"symbol": "BTC-USDT-SWAP", "spec_yaml": "research/strategies/strategy_S1.yaml",
                        "base_run": "btc_s9_base", "regime_runs": {}, "stress_runs": {},
                        "sweep_run": None, "walk_forward_runs": []},
        }
        runs_json = tmp_path / "strategy_runs.json"
        runs_json.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr("pipeline.strategy_runs._DEFAULT_JSON_PATH", runs_json)

        # stub the shell helpers: pretend every backtest succeeds with an artifact
        monkeypatch.setattr(s3, "_setup_run_dir", lambda *a, **k: None)
        monkeypatch.setattr(
            s3, "_run_backtest",
            lambda run_dir: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(
            s3, "verify_run_artifacts",
            lambda run_dir: BacktestRunResult(run_name=run_dir.name, ok=True),
        )

        cfg = dataclasses.replace(_make_research_config(period=730, interval="1H"),
                                  oos_start="2025-01-01")
        registered = s3._run_stress_for_strategy(
            "btc_s9", "BTC-USDT-SWAP", cfg, tmp_path / "runs",
            tmp_path / "code", tmp_path / "manifests", today=date(2026, 1, 1),
        )
        assert set(registered) == {"2x_fees_train", "3x_fees_train", "2x_fees_oos", "3x_fees_oos"}
        on_disk = json.loads(runs_json.read_text())["btc_s9"]["stress_runs"]
        assert on_disk == registered

    def test_failed_stress_run_not_registered(self, tmp_path, monkeypatch):
        import subprocess
        from pipeline import stage3_backtest as s3
        payload = {"btc_s9": {"symbol": "BTC-USDT-SWAP", "spec_yaml": "x",
                               "base_run": "btc_s9_base", "regime_runs": {}, "stress_runs": {},
                               "sweep_run": None, "walk_forward_runs": []}}
        runs_json = tmp_path / "strategy_runs.json"
        runs_json.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr("pipeline.strategy_runs._DEFAULT_JSON_PATH", runs_json)
        monkeypatch.setattr(s3, "_setup_run_dir", lambda *a, **k: None)
        monkeypatch.setattr(
            s3, "_run_backtest",
            lambda run_dir: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
        )
        cfg = dataclasses.replace(_make_research_config(), oos_start=None)
        registered = s3._run_stress_for_strategy(
            "btc_s9", "BTC-USDT-SWAP", cfg, tmp_path / "runs",
            tmp_path / "code", tmp_path / "manifests", today=date(2026, 1, 1),
        )
        assert registered == {}


class TestRunLagStressForStrategy:
    """_run_lag_stress_for_strategy writes config.json + a compiled signal_engine.py
    per (window x lag) directly (no _setup_run_dir copy), then backtests it.

    compile_strategy() and StrategySpec.model_validate() are mocked so the test
    exercises only this function's own orchestration logic, not the compiler.
    """

    def _write_payload(self, tmp_path, strategy_id="btc_s9"):
        payload = {
            strategy_id: {
                "symbol": "BTC-USDT-SWAP",
                "spec_yaml": "research/strategies/strategy_S1.yaml",
                "base_run": f"{strategy_id}_base",
                "regime_runs": {},
                "stress_runs": {},
                "sweep_run": None,
                "walk_forward_runs": [],
            },
        }
        runs_json = tmp_path / "strategy_runs.json"
        runs_json.write_text(json.dumps(payload), encoding="utf-8")
        return runs_json

    def test_registers_successful_lag_stress_runs(self, tmp_path, monkeypatch):
        import subprocess
        from pipeline import stage3_backtest as s3

        runs_json = self._write_payload(tmp_path, "btc_s9")
        monkeypatch.setattr("pipeline.strategy_runs._DEFAULT_JSON_PATH", runs_json)

        # StrategySpec.model_validate: skip real schema validation entirely.
        monkeypatch.setattr(
            "schemas.StrategySpec.model_validate", lambda raw: MagicMock()
        )
        # compile_strategy: canned source string, independent of the real compiler.
        monkeypatch.setattr(
            "lib.signal_compiler.compile_strategy",
            lambda spec, lag_bars=0, **k: f"# lag={lag_bars}\nclass SignalEngine:\n    pass\n",
        )
        monkeypatch.setattr(
            s3, "_run_backtest",
            lambda run_dir: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(
            s3, "verify_run_artifacts",
            lambda run_dir: BacktestRunResult(run_name=run_dir.name, ok=True),
        )

        cfg = dataclasses.replace(
            _make_research_config(period=730, interval="1H"),
            oos_start="2025-01-01", lag_stress_bars=(1, 2),
        )
        registered = s3._run_lag_stress_for_strategy(
            "btc_s9", "BTC-USDT-SWAP", "research/strategies/strategy_S1.yaml",
            cfg, tmp_path / "runs", tmp_path / "strategies_code",
            today=date(2026, 1, 1),
        )
        assert registered == {
            "lag1_train": "btc_s9_lagstress_train_lag1",
            "lag2_train": "btc_s9_lagstress_train_lag2",
            "lag1_oos": "btc_s9_lagstress_oos_lag1",
            "lag2_oos": "btc_s9_lagstress_oos_lag2",
        }
        on_disk = json.loads(runs_json.read_text())["btc_s9"]["lag_stress_runs"]
        assert on_disk == registered
        # signal_engine.py was actually written from the mocked compile_strategy output
        se = (tmp_path / "runs" / "btc_s9_lagstress_train_lag1" / "code" / "signal_engine.py").read_text()
        assert "lag=1" in se

    def test_failed_lag_stress_run_not_registered(self, tmp_path, monkeypatch):
        import subprocess
        from pipeline import stage3_backtest as s3

        runs_json = self._write_payload(tmp_path, "btc_s9")
        monkeypatch.setattr("pipeline.strategy_runs._DEFAULT_JSON_PATH", runs_json)
        monkeypatch.setattr(
            "schemas.StrategySpec.model_validate", lambda raw: MagicMock()
        )
        monkeypatch.setattr(
            "lib.signal_compiler.compile_strategy",
            lambda spec, lag_bars=0, **k: "class SignalEngine:\n    pass\n",
        )
        monkeypatch.setattr(
            s3, "_run_backtest",
            lambda run_dir: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
        )
        # verify_run_artifacts shouldn't even matter here, but stub it ok=False too
        monkeypatch.setattr(
            s3, "verify_run_artifacts",
            lambda run_dir: BacktestRunResult(run_name=run_dir.name, ok=False),
        )

        cfg = dataclasses.replace(_make_research_config(), oos_start=None, lag_stress_bars=(1,))
        registered = s3._run_lag_stress_for_strategy(
            "btc_s9", "BTC-USDT-SWAP", "research/strategies/strategy_S1.yaml",
            cfg, tmp_path / "runs", tmp_path / "strategies_code",
            today=date(2026, 1, 1),
        )
        assert registered == {}
        # lag_stress_runs must not have been written when nothing registered
        on_disk = json.loads(runs_json.read_text())["btc_s9"]
        assert "lag_stress_runs" not in on_disk

    def test_skips_hand_written_signal_engine(self, tmp_path, monkeypatch):
        """A strategy with '# manual: do-not-overwrite' must be skipped entirely —
        no recompile, no backtest — because the compiled proxy would silently
        diverge from the real deployed engine (see eth_s5_half_size)."""
        from pipeline import stage3_backtest as s3

        runs_json = self._write_payload(tmp_path, "eth_s5")
        monkeypatch.setattr("pipeline.strategy_runs._DEFAULT_JSON_PATH", runs_json)

        # Write a hand-written signal_engine.py with the escape-hatch marker.
        code_dir = tmp_path / "strategies_code" / "eth_s5"
        code_dir.mkdir(parents=True)
        (code_dir / "signal_engine.py").write_text(
            "# manual: do-not-overwrite\nclass SignalEngine:\n    pass\n",
            encoding="utf-8",
        )

        # compile_strategy / StrategySpec.model_validate must NOT be called.
        compile_called = MagicMock()
        monkeypatch.setattr("lib.signal_compiler.compile_strategy", compile_called)
        validate_called = MagicMock()
        monkeypatch.setattr("schemas.StrategySpec.model_validate", validate_called)
        run_backtest_called = MagicMock()
        monkeypatch.setattr(s3, "_run_backtest", run_backtest_called)

        cfg = dataclasses.replace(_make_research_config(), oos_start=None, lag_stress_bars=(1,))
        registered = s3._run_lag_stress_for_strategy(
            "eth_s5", "ETH-USDT-SWAP", "research/strategies/strategy_eth_s5_half_size.yaml",
            cfg, tmp_path / "runs", tmp_path / "strategies_code",
            today=date(2026, 1, 1),
        )

        assert registered == {}
        compile_called.assert_not_called()
        validate_called.assert_not_called()
        run_backtest_called.assert_not_called()
        # lag_stress_runs must not have been written
        on_disk = json.loads(runs_json.read_text())["eth_s5"]
        assert "lag_stress_runs" not in on_disk


class TestResolveStopPct:
    def test_resolve_stop_pct_from_yaml(self, tmp_path):
        from pipeline.stage3_backtest import _resolve_stop_pct
        y = tmp_path / "s.yaml"
        y.write_text(
            "name: s\nexit_rules:\n"
            "- condition: take_profit_pct\n  value: 8.5\n"
            "- condition: stop_loss_pct\n  value: 3.0\n",
            encoding="utf-8",
        )
        assert _resolve_stop_pct(y, engine_path=None) == 3.0

    def test_resolve_stop_pct_absent_returns_none(self, tmp_path):
        from pipeline.stage3_backtest import _resolve_stop_pct
        y = tmp_path / "s.yaml"
        y.write_text(
            "name: s\nexit_rules:\n- condition: time_based\n  max_hold_hours: 168\n",
            encoding="utf-8",
        )
        assert _resolve_stop_pct(y, engine_path=None) is None

    def test_resolve_stop_pct_warns_on_engine_mismatch(self, tmp_path, capsys):
        from pipeline.stage3_backtest import _resolve_stop_pct
        y = tmp_path / "s.yaml"
        y.write_text("name: s\nexit_rules:\n- condition: stop_loss_pct\n  value: 3.0\n", encoding="utf-8")
        eng = tmp_path / "signal_engine.py"
        eng.write_text("class SignalEngine:\n    SL_PCT = 5.0\n", encoding="utf-8")
        val = _resolve_stop_pct(y, engine_path=eng)
        assert val == 3.0                         # YAML wins
        assert "mismatch" in capsys.readouterr().out.lower()
