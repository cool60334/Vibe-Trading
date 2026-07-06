"""
research/pipeline/final_holdout.py
────────────────────────────────────
Manual, one-shot final-holdout CLI.

This is the ONLY code path in the entire repo allowed to backtest on
``[final_holdout_start, today]`` data — the window reserved by
``cfg.final_holdout_start`` (see ``resolve_anchor_date()`` in
``stage3_backtest.py``), which every AUTOMATIC pipeline stage is
unconditionally capped away from.

It is invoked manually by a human, exactly once per strategy, right before a
promote decision:

    python -m research.pipeline.final_holdout --strategy <strategy_id>

CRITICAL SAFETY CONSTRAINT
---------------------------
This module must NEVER be imported or called by any automatic pipeline
chain — no stage module, no job runner, no cron, no dashboard "run
pipeline" button/endpoint. A second (or third...) peek at the same holdout
window is selection bias; the loud "RE-PEEK WARNING" below exists precisely
because nothing else in the codebase should ever trigger it.

Usage
-----
    # From repo root:
    python -m research.pipeline.final_holdout --strategy eth_s5

    # From research/ directory:
    python -m pipeline.final_holdout --strategy eth_s5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# ── Path bootstrap ─────────────────────────────────────────────────────────
_THIS_FILE = Path(__file__).resolve()
_PIPELINE_DIR = _THIS_FILE.parent          # research/pipeline/
_RESEARCH_DIR = _PIPELINE_DIR.parent       # research/

for _p in (_RESEARCH_DIR,):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from pipeline.config import _REPO_ROOT, load_config  # noqa: E402
from pipeline.strategy_runs import load_strategy_runs  # noqa: E402
from pipeline.stage3_backtest import build_run_config, symbol_to_short  # noqa: E402
from pipeline.stage4_optimize import (  # noqa: E402
    _compile_signal_code,
    _invoke_backtest,
    _read_metrics,
    _scaffold_combo_run,
    apply_overrides_to_spec,
)

_REPO_ROOT_STR = str(_REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from lib.research_ledger import append_event, read_events  # noqa: E402


# ─── Pure-logic helpers (testable, network-free) ──────────────────────────


def holdout_window(final_holdout_start: str, today: "date | None" = None) -> tuple[str, str]:
    """Return (final_holdout_start, today ISO) — the window this CLI spends.

    Args:
        final_holdout_start: ISO date (YYYY-MM-DD) — cfg.final_holdout_start.
        today: Reference "today" date. Defaults to date.today() if not given.

    Returns:
        (final_holdout_start, today_iso) tuple.
    """
    if today is None:
        today = date.today()
    return (final_holdout_start, today.isoformat())


def prior_holdout_evals(manifests_dir: "str | Path", strategy_id: str) -> int:
    """Count prior ``kind == "final_holdout"`` ledger events for this strategy.

    A nonzero count means the holdout window has already been peeked at for
    this strategy — a second look is itself selection bias, and the caller
    must surface a loud warning rather than silently proceeding.

    Args:
        manifests_dir: research/manifests/ directory (or interval-namespaced
            subdirectory) containing research_ledger.jsonl.
        strategy_id: Strategy identifier to match against ledger events.

    Returns:
        Number of matching prior events (0 if none / ledger absent).
    """
    return sum(
        1
        for e in read_events(manifests_dir)
        if e.get("kind") == "final_holdout" and e.get("strategy_id") == strategy_id
    )


# ─── CLI entry point ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot final-holdout backtest. Manually invoked ONLY — never "
            "call this from an automatic pipeline stage, job runner, or cron."
        )
    )
    parser.add_argument("--strategy", required=True, help="strategy_id to evaluate")
    args = parser.parse_args()

    # ── 1. Config must reserve a final holdout ─────────────────────────────
    cfg = load_config()
    if not cfg.final_holdout_start:
        print(
            "[final_holdout] ERROR: cfg.final_holdout_start is not set in "
            "research_config.yaml — nothing is reserved as a final holdout. "
            "Refusing to proceed (this must never silently no-op)."
        )
        sys.exit(2)

    # ── 2. Resolve manifests dir ────────────────────────────────────────────
    from lib.timeframe import active_manifests_dir
    manifests_dir = active_manifests_dir()

    # ── 3. Look up the strategy ─────────────────────────────────────────────
    runs_map = load_strategy_runs()
    if args.strategy not in runs_map.entries:
        print(f"[final_holdout] ERROR: strategy_id {args.strategy!r} not found in strategy_runs.json")
        sys.exit(1)
    entry = runs_map.entries[args.strategy]

    # ── 4. Re-peek warning ───────────────────────────────────────────────────
    n_prior = prior_holdout_evals(manifests_dir, args.strategy)
    if n_prior > 0:
        print("!" * 70)
        print(f"!!  RE-PEEK WARNING: {args.strategy} has ALREADY been evaluated")
        print(f"!!  against the final holdout {n_prior} time(s) before.")
        print("!!  A second look at held-out data is itself selection bias.")
        print("!!  Treat this result as TAINTED, not a clean confirmation.")
        print("!" * 70)

    # ── 5. Load tuned params (fail-soft) ────────────────────────────────────
    best_params: dict = {}
    optimization_path = manifests_dir / args.strategy / "optimization.json"
    if optimization_path.exists():
        try:
            opt_data = json.loads(optimization_path.read_text(encoding="utf-8"))
            best_params = opt_data.get("best_params") or {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[final_holdout] WARN: could not read optimization.json ({exc}); using spec defaults")
            best_params = {}

    # ── 6. Load spec YAML, apply overrides, compile signal code ────────────
    import yaml as _yaml

    spec_path = _REPO_ROOT / entry.spec_yaml
    base_spec = _yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec_dict = apply_overrides_to_spec(base_spec, best_params) if best_params else base_spec
    signal_code = _compile_signal_code(spec_dict, interval=cfg.interval)

    # ── 7. Build run config, override window to the holdout ────────────────
    run_config = build_run_config(symbol=entry.symbol, cfg=cfg)
    start_iso, end_iso = holdout_window(cfg.final_holdout_start)
    run_config["start_date"], run_config["end_date"] = start_iso, end_iso

    # ── 8. Scaffold + invoke backtest ───────────────────────────────────────
    run_name = f"{args.strategy}_final_holdout"
    run_dir = _REPO_ROOT / "runs" / run_name
    _scaffold_combo_run(run_dir, run_config, signal_code)

    print(f"[final_holdout] {args.strategy}: backtesting holdout window {start_iso}..{end_iso}")
    proc = _invoke_backtest(run_dir)
    if proc.returncode != 0:
        print(
            f"[final_holdout] ERROR: backtest failed (exit {proc.returncode}): "
            f"{(proc.stderr or '')[:500]}"
        )
        sys.exit(1)

    metrics = _read_metrics(run_dir)
    if metrics is None:
        print("[final_holdout] ERROR: backtest produced no metrics.csv")
        sys.exit(1)

    sharpe = float(metrics.get("sharpe", 0) or 0)
    max_drawdown = float(metrics.get("max_drawdown", 0) or 0)
    trade_count = int(float(metrics.get("trade_count", 0) or 0))

    # ── 9. Write result + ledger event ──────────────────────────────────────
    result = {
        "strategy_id": args.strategy,
        "window": [start_iso, end_iso],
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "trade_count": trade_count,
        "best_params": best_params,
        "prior_evals": n_prior,
    }
    out_dir = manifests_dir / args.strategy
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "final_holdout.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    append_event(
        manifests_dir,
        kind="final_holdout",
        symbol=symbol_to_short(entry.symbol),
        strategy_id=args.strategy,
        detail={"window": [start_iso, end_iso], "sharpe": sharpe, "prior_evals": n_prior},
    )

    tainted = " [TAINTED: re-peek]" if n_prior > 0 else ""
    print(
        f"[final_holdout] {args.strategy}: sharpe={sharpe:+.3f} "
        f"max_drawdown={max_drawdown:.3f} trade_count={trade_count}{tainted}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
