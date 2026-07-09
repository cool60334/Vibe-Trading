"""Task 4 acceptance test — DSL regime_filter + size_mult equivalent to eth_s5_half_size.

Validates that compiling eth_s5_dsl_equiv.yaml (with regime_filter: true and
size_mult: 0.45) produces a signal_engine.py whose backtest results match the
hand-written eth_s5_half_size reference runs within tolerance.

Reference values are the hand-written eth_s5_half_size engine's metrics, captured
on the FROZEN factor/regime fixture under research/tests/fixtures/manifests_eth_s5/
(NOT the live research/manifests/, which stage1 rewrites on every pipeline run —
the freeze-time parquet was gitignored and is unrecoverable). The compiled engine
is pointed at that fixture via the RESEARCH_MANIFESTS_DIR env var so this test is
hermetic and immune to future factor/regime revisions.

  OOS  (2025-01-01 → 2026-06-02):
    sharpe       = 0.8490191338430589
    max_drawdown = -0.0912804881958745
    trade_count  = 49

  Train (2022-06-11 → 2025-01-01):
    sharpe       = 0.8778799680805691
    max_drawdown = -0.13615947394033404
    trade_count  = 80

REF is bound to the fixture data version. If the fixture is regenerated, rerun the
hand-written eth_s5_half_size train/OOS backtests against it and re-freeze
REF_TRAIN/REF_OOS below.

REF re-frozen 2026-07-09 (regime look-ahead fix): research/lib/regime.py's
compute_regime()-derived daily labels were being ffill'd onto hourly bars with
~1 day of look-ahead (a label at day-D was assigned starting D 00:00, but it
depends on day-D's own end-of-day close, only knowable at ~D 23:59 -- see
ffill_regime_to() in research/lib/regime.py). eth_s5_half_size's manual
signal_engine.py had this exact bug in its own _load_regime_series() copy.
Fixing it changed real backtest results: Train sharpe 1.1633 -> 0.8779
(trade_count 82 -> 80), OOS sharpe 1.0158 -> 0.8490 (trade_count unchanged at
49). The strategy's true edge is meaningfully smaller than previously
measured -- the old numbers were partly a look-ahead artifact.

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
import os
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

# Frozen factor/regime snapshot — the compiled engine reads factor_values_eth.parquet
# and regime_eth.json from here (via RESEARCH_MANIFESTS_DIR) instead of the live
# research/manifests/ dir, keeping the backtest reproducible across pipeline reruns.
FIXTURE_MANIFESTS_DIR = _RESEARCH_DIR / "tests" / "fixtures" / "manifests_eth_s5"

# Reference metrics — hand-written eth_s5_half_size engine on the fixture above.
# Re-frozen 2026-07-09 after fixing a ~1-day regime look-ahead bug (see module
# docstring) — these are the honest, look-ahead-free numbers.
REF_OOS = {
    "sharpe": 0.8490191338430589,
    "max_drawdown": -0.0912804881958745,
    "trade_count": 49,
}
REF_TRAIN = {
    "sharpe": 0.8778799680805691,
    "max_drawdown": -0.13615947394033404,
    "trade_count": 80,
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
    """Invoke backtest.runner as a subprocess (matches stage3 pattern).

    RESEARCH_MANIFESTS_DIR points the compiled engine at the frozen fixture so
    factor + regime reads are hermetic (see FIXTURE_MANIFESTS_DIR).
    """
    env = {**os.environ, "RESEARCH_MANIFESTS_DIR": str(FIXTURE_MANIFESTS_DIR)}
    proc = subprocess.run(
        [sys.executable, "-m", "backtest.runner", str(run_dir)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
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
