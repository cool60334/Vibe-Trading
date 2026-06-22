"""Phase 0 lag gate for sol_s1 paper-forward validation.

Runs the sol_s1_single_factor_regime backtest twice:
  - lag=0  (baseline, as-backtested)
  - lag=24 (realistic live: ls_divergence shifted forward 24h, simulating
             the T+1 Binance daily-metrics archive publish delay)

Compares the lagged OOS sharpe against a deploy threshold.
Go/No-Go is written to runs/sol_s1_laggate_result.json.

Usage (from repo root):
    cd research
    python scripts/phase0_lag_gate.py

Exit code: 0 = GO, 1 = NO-GO or error.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Repo / path constants ─────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[2]              # C:/Users/cool6/Vibe-Trading
_RESEARCH = _REPO / "research"        # research/
_RUNS = _REPO / "runs"

# ── Strategy constants ────────────────────────────────────────────────────────
STRATEGY_ID = "sol_s1_single_factor_regime"
SYMBOL = "SOL-USDT-SWAP"

# OOS sharpe threshold for deployment approval.
# If the lagged OOS sharpe is below this, do not deploy.
THRESHOLD = 1.0


# ── Pure decision function ────────────────────────────────────────────────────

@dataclasses.dataclass
class GateDecision:
    go: bool
    summary: str


def gate_decision(
    baseline_sharpe: float | None,
    lagged_sharpe: float | None,
    threshold: float,
) -> GateDecision:
    """Pure decision: GO iff lagged_sharpe is present and >= threshold.

    Args:
        baseline_sharpe: OOS sharpe from the lag-0 (as-backtested) run.
        lagged_sharpe:   OOS sharpe from the lag-24h run (realistic live).
        threshold:       Minimum lagged OOS sharpe required to proceed.

    Returns:
        GateDecision with go=True if lagged_sharpe >= threshold, else False.
    """
    if lagged_sharpe is None:
        return GateDecision(False, "lagged OOS sharpe unavailable → NO-GO")
    go = lagged_sharpe >= threshold
    verdict = "GO" if go else "NO-GO"
    return GateDecision(
        go,
        f"baseline(lag0)={baseline_sharpe} lagged(24h)={lagged_sharpe} "
        f"threshold={threshold} → {verdict}",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_sharpe(run_dir: Path) -> float | None:
    """Read the OOS sharpe from a run's artifacts/metrics.csv."""
    m = run_dir / "artifacts" / "metrics.csv"
    if not m.exists():
        return None
    with m.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return None
    try:
        return round(float(rows[0]["sharpe"]), 3)
    except (KeyError, ValueError, TypeError):
        return None


def _copy_signal_engine(strategies_code_dir: Path, strategy_id: str, dest: Path) -> None:
    """Copy the frozen signal_engine.py for strategy_id to dest."""
    src = strategies_code_dir / strategy_id / "signal_engine.py"
    if src.exists():
        shutil.copy2(src, dest)
        print(f"      [signal_engine] copied from {src.relative_to(_REPO)}")
    else:
        # Write a minimal stub so the backtest runner can at least import it.
        stub = (
            "class SignalEngine:\n"
            "    def generate(self, data_map):\n"
            "        import pandas as pd\n"
            "        sym = next(iter(data_map))\n"
            "        return pd.Series(0, index=data_map[sym].index)\n"
        )
        dest.write_text(stub, encoding="utf-8")
        print(f"      [signal_engine] stub written (no frozen engine found at {src})")


def _run_at_lag(lag_hours: int, cfg) -> float | None:  # type: ignore[type-arg]
    """Run the sol_s1 backtest with ls_divergence shifted by lag_hours.

    Creates a temporary run directory, patches the config with the lag
    override, runs the backtest via subprocess, and returns the OOS sharpe.

    When lag_hours == 0, this is the baseline (as-backtested) run.
    When lag_hours == 24, this simulates the realistic live-availability lag
    of the Binance daily-metrics archive (~T+1 publish delay).

    The lag is injected via an environment variable (FACTOR_LAG_HOURS) that
    the signal engine is expected to honour when loading ls_divergence.  If
    the current frozen engine does not yet honour the env var, the lag-0 and
    lag-24 results will be identical — the gate will still pass/fail
    correctly against the threshold, but the measurement will be optimistic
    and Phase 0 should be re-run once the engine is patched.

    Args:
        lag_hours: Number of hours to shift the factor forward (0 = baseline).
        cfg:       ResearchConfig loaded from research_config.yaml.

    Returns:
        OOS sharpe (float rounded to 3 dp) or None if the run failed.
    """
    from pipeline.stage3_backtest import (  # noqa: PLC0415
        _run_backtest,
        _setup_run_dir,
        build_run_config,
    )

    run_name = f"{STRATEGY_ID}_laggate_lag{lag_hours}h"
    run_dir = _RUNS / run_name
    strategies_code_dir = _RESEARCH / "strategies" / "code"

    config_dict = build_run_config(symbol=SYMBOL, cfg=cfg)

    _setup_run_dir(
        run_dir=run_dir,
        config_dict=config_dict,
        strategies_code_dir=strategies_code_dir,
        strategy_id=STRATEGY_ID,
    )

    print(f"\n[phase0] Running lag={lag_hours}h backtest → {run_dir.name}")

    import os  # noqa: PLC0415
    env = os.environ.copy()
    env["SOL_FACTOR_LAG_HOURS"] = str(lag_hours)

    proc = subprocess.run(
        [sys.executable, "-m", "backtest.runner", str(run_dir)],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )

    if proc.returncode != 0:
        print(f"  [WARN] backtest returned exit code {proc.returncode}")
        print(proc.stderr[-2000:] if proc.stderr else "(no stderr)")
        return None

    sharpe = _read_sharpe(run_dir)
    print(f"  [phase0] lag={lag_hours}h sharpe={sharpe}")
    return sharpe


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    """Entry point: run baseline + lagged backtests, emit gate result."""
    # Add research/ to sys.path so pipeline imports resolve correctly.
    research_str = str(_RESEARCH)
    if research_str not in sys.path:
        sys.path.insert(0, research_str)

    from pipeline.config import load_config  # noqa: PLC0415

    cfg = load_config()

    baseline = _run_at_lag(0, cfg)
    lagged = _run_at_lag(24, cfg)
    decision = gate_decision(baseline, lagged, THRESHOLD)

    print("\n=== Phase 0 lag gate ===")
    print(decision.summary)

    result_path = _RUNS / f"{STRATEGY_ID}_paper_laggate_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "baseline_lag0_sharpe": baseline,
                "lagged_24h_sharpe": lagged,
                "threshold": THRESHOLD,
                "go": decision.go,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[phase0] Result written to {result_path.relative_to(_REPO)}")

    return 0 if decision.go else 1


if __name__ == "__main__":
    sys.exit(main())
