"""Tests for stage4_optimize archetype_misfit guard.

These tests verify the guard logic that skips the sweep when a
manifests/<strategy_id>/archetype_misfit.json sentinel is present
with {"archetype_misfit": true}.

All tests use tmp_path; no real subprocess calls are made.
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

# dashboard/server/ must be on sys.path for schemas import inside stage4.
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"
if str(_DASHBOARD_SCHEMAS) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_SCHEMAS))

from pipeline.stage4_optimize import (  # noqa: E402
    OptimizationCheckResult,
    _optimize_strategy,
    compute_exit_code,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_research_config(period: int = 365, interval: str = "1H"):
    """Return a minimal ResearchConfig for tests."""
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


def _make_strategy_entry(symbol: str = "BTC-USDT-SWAP"):
    """Return a minimal StrategyRunsEntry for tests."""
    from pipeline.strategy_runs import StrategyRunsEntry
    return StrategyRunsEntry(
        symbol=symbol,
        spec_yaml="research/strategies/strategy_test.yaml",
        base_run="btc_s1_base",
        regime_runs=types.MappingProxyType({}),
        stress_runs=types.MappingProxyType({}),
        sweep_run=None,
    )


def _write_archetype_misfit(manifests_dir: Path, strategy_id: str, flag: bool) -> None:
    """Write an archetype_misfit.json sentinel to manifests/<strategy_id>/."""
    sentinel_dir = manifests_dir / strategy_id
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel = {
        "archetype_misfit": flag,
        "reason": "test: deeply negative sharpe" if flag else "test: no misfit",
        "sharpe": -5.0 if flag else 0.5,
        "trades_per_year": 12.0,
    }
    (sentinel_dir / "archetype_misfit.json").write_text(
        json.dumps(sentinel, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStage4ArchetypeMisfitGuard:
    """_optimize_strategy() archetype_misfit guard behaves correctly."""

    def test_sentinel_true_skips_sweep(self, tmp_path):
        """When archetype_misfit sentinel is True the sweep is skipped entirely."""
        strategy_id = "btc_s1_test_misfit"
        manifests_dir = tmp_path / "manifests"
        runs_root = tmp_path / "runs"
        strategies_dir = tmp_path / "strategies"
        runs_root.mkdir(parents=True)
        strategies_dir.mkdir(parents=True)

        # Write sentinel with archetype_misfit = True
        _write_archetype_misfit(manifests_dir, strategy_id, flag=True)

        cfg = _make_research_config()
        entry = _make_strategy_entry()

        # We should NOT reach any subprocess calls, so patch to catch accidents.
        with patch("pipeline.stage4_optimize._invoke_backtest") as mock_bt, \
             patch("pipeline.stage4_optimize._compile_signal_code") as mock_cc:

            result = _optimize_strategy(
                strategy_id=strategy_id,
                entry=entry,
                cfg=cfg,
                runs_root=runs_root,
                strategies_dir=strategies_dir,
                manifests_dir=manifests_dir,
                max_combos=10,
                seed=42,
                window=None,
                oos_win=None,
            )

        # Guard fired: result is not OK (skipped with an error message)
        assert isinstance(result, OptimizationCheckResult)
        assert result.strategy_id == strategy_id
        assert result.ok is False
        assert result.skipped is True  # intentional skip, must be non-fatal
        assert "archetype_misfit" in (result.error or "")

        # Sweep machinery was never invoked
        mock_bt.assert_not_called()
        mock_cc.assert_not_called()

    def test_sentinel_false_does_not_skip(self, tmp_path):
        """When archetype_misfit sentinel is False the sweep proceeds normally."""
        strategy_id = "btc_s1_test_ok"
        manifests_dir = tmp_path / "manifests"
        runs_root = tmp_path / "runs"
        strategies_dir = tmp_path / "strategies"
        runs_root.mkdir(parents=True)
        strategies_dir.mkdir(parents=True)

        # Write sentinel with archetype_misfit = False
        _write_archetype_misfit(manifests_dir, strategy_id, flag=False)

        # Provide NO diagnosis.json → the function will bail at the next gate
        # with an error about missing diagnosis.json — that's fine; it proves
        # the misfit guard did NOT short-circuit.
        cfg = _make_research_config()
        entry = _make_strategy_entry()

        result = _optimize_strategy(
            strategy_id=strategy_id,
            entry=entry,
            cfg=cfg,
            runs_root=runs_root,
            strategies_dir=strategies_dir,
            manifests_dir=manifests_dir,
            max_combos=10,
            seed=42,
            window=None,
            oos_win=None,
        )

        # The guard did NOT fire (ok=False, but the error is about diagnosis.json,
        # NOT about archetype_misfit).
        assert isinstance(result, OptimizationCheckResult)
        assert result.ok is False
        assert "archetype_misfit" not in (result.error or ""), (
            f"Guard fired unexpectedly; error: {result.error!r}"
        )
        assert "diagnosis" in (result.error or "").lower()

    def test_no_sentinel_does_not_skip(self, tmp_path):
        """When archetype_misfit sentinel is absent the sweep proceeds (fail-open)."""
        strategy_id = "btc_s1_test_no_sentinel"
        manifests_dir = tmp_path / "manifests"
        runs_root = tmp_path / "runs"
        strategies_dir = tmp_path / "strategies"
        runs_root.mkdir(parents=True)
        strategies_dir.mkdir(parents=True)

        # Deliberately do NOT write any sentinel file.
        cfg = _make_research_config()
        entry = _make_strategy_entry()

        result = _optimize_strategy(
            strategy_id=strategy_id,
            entry=entry,
            cfg=cfg,
            runs_root=runs_root,
            strategies_dir=strategies_dir,
            manifests_dir=manifests_dir,
            max_combos=10,
            seed=42,
            window=None,
            oos_win=None,
        )

        # Fail-open: guard did NOT fire, execution fell through to the next gate
        # (diagnosis.json missing), not to an archetype_misfit short-circuit.
        assert isinstance(result, OptimizationCheckResult)
        assert result.ok is False
        assert "archetype_misfit" not in (result.error or ""), (
            f"Guard incorrectly fired when sentinel was absent; error: {result.error!r}"
        )
        assert "diagnosis" in (result.error or "").lower()


class TestComputeExitCodeSkipSemantics:
    """compute_exit_code: archetype_misfit skips are non-fatal; real failures are fatal."""

    def _ok(self, sid="a"):
        return OptimizationCheckResult(strategy_id=sid, ok=True)

    def _skip(self, sid="m"):
        return OptimizationCheckResult(strategy_id=sid, ok=False, skipped=True, error="archetype_misfit")

    def _fail(self, sid="f"):
        return OptimizationCheckResult(strategy_id=sid, ok=False, error="diagnosis.json missing")

    def test_optimized_plus_misfit_skip_is_success(self):
        """≥1 optimized + only misfit skips → exit 0 (the BTC pipeline case)."""
        assert compute_exit_code([self._ok("s1"), self._ok("s2"), self._skip("s3"), self._skip("s4")]) == 0

    def test_real_failure_is_fatal(self):
        """A genuine failure (missing diagnosis/yaml/schema) still aborts."""
        assert compute_exit_code([self._ok(), self._fail()]) == 1

    def test_all_skipped_is_failure(self):
        """Nothing optimized → exit 1 even though all were 'skips'."""
        assert compute_exit_code([self._skip("a"), self._skip("b")]) == 1

    def test_empty_is_failure(self):
        assert compute_exit_code([]) == 1
