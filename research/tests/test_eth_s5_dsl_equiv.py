"""Task 4 acceptance test — DSL regime_filter + size_mult equivalent to eth_s5_half_size.

Validates that compiling eth_s5_dsl_equiv.yaml (with regime_filter: true and
size_mult: 0.45) produces a signal_engine.py whose backtest results match the
hand-written eth_s5_half_size reference runs within tolerance.

Reference values (from runs/eth_s5_half_size_oos and runs/eth_s5_half_size_train):

  OOS  (2025-01-01 → 2026-06-02):
    sharpe       = 1.0157874592604321
    max_drawdown = -0.09128048816641941
    trade_count  = 49

  Train (2022-06-11 → 2025-01-01):
    sharpe       = 1.2386463971329293
    max_drawdown = -0.14889666790038583
    trade_count  = 86

Tolerances:
  sharpe:       ±0.05
  max_drawdown: ±0.005  (0.5 percentage points)
  trade_count:  exact

Marked @pytest.mark.slow so it is excluded from the fast test suite.
Run explicitly with:
    pytest research/tests/test_eth_s5_dsl_equiv.py -v
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_RESEARCH_DIR = _HERE.parents[1]       # research/
_REPO_ROOT = _RESEARCH_DIR.parent      # repo root
_AGENT_DIR = _REPO_ROOT / "agent"
_DASHBOARD_SERVER = _REPO_ROOT / "dashboard" / "server"

for _p in (str(_RESEARCH_DIR), str(_REPO_ROOT), str(_AGENT_DIR), str(_DASHBOARD_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dashboard.server.schemas import StrategySpec
from lib.signal_compiler import compile_strategy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FIXTURE_YAML = _RESEARCH_DIR / "tests" / "fixtures" / "eth_s5_dsl_equiv.yaml"

# Reference metrics
REF_OOS = {
    "sharpe": 1.0157874592604321,
    "max_drawdown": -0.09128048816641941,
    "trade_count": 49,
}
REF_TRAIN = {
    "sharpe": 1.2386463971329293,
    "max_drawdown": -0.14889666790038583,
    "trade_count": 86,
}

SHARPE_TOL = 0.05
DD_TOL = 0.005   # 0.5 percentage points (absolute difference on the fraction)
TRADE_EXACT = True

OOS_CONFIG = {
    "codes": ["ETH-USDT-SWAP"],
    "start_date": "2025-01-01",
    "end_date": "2026-06-02",
    "source": "okx",
    "interval": "1H",
    "engine": "daily",
}

TRAIN_CONFIG = {
    "codes": ["ETH-USDT-SWAP"],
    "start_date": "2022-06-11",
    "end_date": "2025-01-01",
    "source": "okx",
    "interval": "1H",
    "engine": "daily",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile_dsl_engine() -> str:
    """Load fixture YAML, validate StrategySpec, compile to source string."""
    with FIXTURE_YAML.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    spec = StrategySpec.model_validate(raw)
    assert spec.regime_filter is True, "regime_filter should be True in fixture"
    assert spec.size_mult == pytest.approx(0.45), "size_mult should be 0.45 in fixture"
    return compile_strategy(spec)


def _setup_run_dir(run_name: str, config: dict, source: str) -> Path:
    """Create a run directory under runs/ with config.json and signal_engine.py."""
    run_dir = _REPO_ROOT / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(run_dir / "artifacts", ignore_errors=True)
    code_dir = run_dir / "code"
    code_dir.mkdir(exist_ok=True)
    (code_dir / "signal_engine.py").write_text(source, encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    return run_dir


def _run_backtest(run_dir: Path) -> None:
    """Invoke backtest.runner as a subprocess (matches stage3 pattern)."""
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.runner", str(run_dir)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Backtest runner failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout[-2000:]}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )


def _read_metrics(run_dir: Path) -> dict:
    """Read artifacts/metrics.csv and return a dict of {column: value}."""
    metrics_csv = run_dir / "artifacts" / "metrics.csv"
    with metrics_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, f"metrics.csv is empty: {metrics_csv}"
    row = rows[0]
    return {k: float(v) for k, v in row.items() if v not in ("", None)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_dsl_oos_matches_reference():
    """DSL-compiled engine OOS run matches eth_s5_half_size_oos within tolerance."""
    source = _compile_dsl_engine()
    run_dir = _setup_run_dir("eth_s5_dsl_equiv_oos", OOS_CONFIG, source)
    _run_backtest(run_dir)
    m = _read_metrics(run_dir)

    sharpe = m["sharpe"]
    max_dd = m["max_drawdown"]
    trades = int(m["trade_count"])

    assert abs(sharpe - REF_OOS["sharpe"]) <= SHARPE_TOL, (
        f"OOS sharpe {sharpe:.4f} deviates from reference "
        f"{REF_OOS['sharpe']:.4f} by more than {SHARPE_TOL}"
    )
    assert abs(max_dd - REF_OOS["max_drawdown"]) <= DD_TOL, (
        f"OOS max_drawdown {max_dd:.4f} deviates from reference "
        f"{REF_OOS['max_drawdown']:.4f} by more than {DD_TOL}"
    )
    if TRADE_EXACT:
        assert trades == REF_OOS["trade_count"], (
            f"OOS trade_count {trades} != reference {REF_OOS['trade_count']}"
        )


@pytest.mark.slow
def test_dsl_train_matches_reference():
    """DSL-compiled engine train run matches eth_s5_half_size_train within tolerance."""
    source = _compile_dsl_engine()
    run_dir = _setup_run_dir("eth_s5_dsl_equiv_train", TRAIN_CONFIG, source)
    _run_backtest(run_dir)
    m = _read_metrics(run_dir)

    sharpe = m["sharpe"]
    max_dd = m["max_drawdown"]
    trades = int(m["trade_count"])

    assert abs(sharpe - REF_TRAIN["sharpe"]) <= SHARPE_TOL, (
        f"Train sharpe {sharpe:.4f} deviates from reference "
        f"{REF_TRAIN['sharpe']:.4f} by more than {SHARPE_TOL}"
    )
    assert abs(max_dd - REF_TRAIN["max_drawdown"]) <= DD_TOL, (
        f"Train max_drawdown {max_dd:.4f} deviates from reference "
        f"{REF_TRAIN['max_drawdown']:.4f} by more than {DD_TOL}"
    )
    if TRADE_EXACT:
        assert trades == REF_TRAIN["trade_count"], (
            f"Train trade_count {trades} != reference {REF_TRAIN['trade_count']}"
        )
