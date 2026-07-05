"""Stage 4b — CPCV validation (opt-in).

python -m research.pipeline.stage4_cpcv --strategy <id> [--blocks 10 --k 3 --max 40]

Precomputes a combo x block per-bar-returns matrix (each block backtested
independently over [block_start - WARMUP_HOURS, block_end] at fixed initial_cash,
warm-up dropped), filters combos to a complete NxK matrix, then writes the CPCV
distribution to research/manifests/<id>/cpcv.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml as _yaml

_THIS = Path(__file__).resolve()
_RESEARCH = _THIS.parent.parent
for _p in (str(_RESEARCH), str(_RESEARCH.parent / "dashboard" / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipeline.config import _REPO_ROOT, load_config
from pipeline.stage3_backtest import build_run_config
from pipeline.stage4_optimize import (
    apply_overrides_to_spec,
    expand_param_ranges,
    sample_combos,
    _compile_signal_code,
    _invoke_backtest,
    _resolve_manifests_dir,
    _scaffold_combo_run,
)
from lib.cpcv import cpcv_distribution, combinatorial_splits, make_blocks
from lib.deflated_sharpe import bars_per_year

DEFAULT_BLOCKS, DEFAULT_K, DEFAULT_MAX = 10, 3, 40


def _max_lookback_hours(expanded: dict, horizons_h: list[int]) -> int:
    lbs = [float(x) for x in expanded.get("lookback_days", [])]
    days = max(lbs) if lbs else 0.0
    return max(int(days * 24), max(horizons_h))


def _hold_hours(base_spec: dict, horizons_h: list[int]) -> int:
    for r in base_spec.get("exit_rules", []):
        if r.get("condition") == "time_based":
            return int(r.get("max_hold_hours", max(horizons_h)))
    return max(horizons_h)


def _block_returns(run_dir: Path, block_start: str) -> pd.Series | None:
    """Read artifacts/equity.csv -> per-bar returns; drop the warm-up prefix
    (rows before block_start)."""
    eq_path = run_dir / "artifacts" / "equity.csv"
    if not eq_path.exists():
        return None
    df = pd.read_csv(eq_path, index_col=0, parse_dates=True)
    if "equity" not in df.columns or df.empty:
        return None
    ret = df["equity"].pct_change().dropna()
    return ret[ret.index >= pd.Timestamp(block_start)]


def run(strategy_id: str, n_blocks: int, k_test: int, max_combos: int) -> int:
    cfg = load_config()
    ppy = bars_per_year(cfg.interval)
    strategies_dir = _REPO_ROOT / "research" / "strategies"
    manifests_dir = _resolve_manifests_dir()
    runs_root = _REPO_ROOT / "runs"

    yaml_path = strategies_dir / f"strategy_{strategy_id}.yaml"
    base_spec = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    expanded = expand_param_ranges(base_spec.get("parameter_search_ranges", {}))
    if not expanded:
        print(f"[cpcv] {strategy_id}: no parameter_search_ranges — nothing to validate")
        return 1
    combos = sample_combos(expanded, max_n=max_combos, seed=42)

    # full history window
    base_cfg = build_run_config(symbol=_symbol_of(base_spec), cfg=cfg)
    hist_start = base_cfg["start_date"]
    # full history incl. OOS (CPCV re-splits everything)
    hist_end = date.today().isoformat() if cfg.oos_start else base_cfg["end_date"]
    blocks = make_blocks(hist_start, hist_end, n_blocks)

    warmup_h = max(_hold_hours(base_spec, cfg.horizons_h),
                   _max_lookback_hours(expanded, cfg.horizons_h))
    warmup_days = warmup_h // 24 + 1  # blocks are day-granular; round warm-up up to whole days
    purge_bars = embargo_bars = max(cfg.horizons_h)

    # ── precompute combo x block matrix ─────────────────────────────────────────
    matrix: dict[int, dict[int, pd.Series]] = {}
    for cid, overrides in enumerate(combos):
        spec_dict = apply_overrides_to_spec(base_spec, overrides)
        code = _compile_signal_code(spec_dict, interval=cfg.interval)
        matrix[cid] = {}
        for bid, (b_start, b_end) in enumerate(blocks):
            warm_start = (date.fromisoformat(b_start) - timedelta(days=warmup_days)).isoformat()
            run_cfg = build_run_config(symbol=_symbol_of(base_spec), cfg=cfg)
            run_cfg["start_date"], run_cfg["end_date"] = warm_start, b_end
            run_dir = runs_root / f"{strategy_id}_cpcv_{cid:03d}_b{bid}"
            _scaffold_combo_run(run_dir, run_cfg, code)
            proc = _invoke_backtest(run_dir)
            if proc.returncode == 0:
                r = _block_returns(run_dir, b_start)
                if r is not None and len(r):
                    matrix[cid][bid] = r
            else:
                print(f"  [cpcv] combo {cid:03d} block {bid} failed (exit {proc.returncode}): "
                      f"{(proc.stderr or '')[:200]}")

    # ── completeness filter: keep only combos with all N blocks (agy P5) ────────
    complete = {cid: bl for cid, bl in matrix.items() if len(bl) == n_blocks}
    if not complete:
        print(f"[cpcv] {strategy_id}: no combo produced all {n_blocks} blocks — abort")
        return 1
    print(f"[cpcv] {strategy_id}: {len(complete)}/{len(combos)} combos complete")

    splits = combinatorial_splits(n_blocks, k_test)
    dist = cpcv_distribution(complete, splits, purge_bars, embargo_bars, ppy)
    dist.pop("path_sharpes", None)  # keep cpcv.json small
    dist.update({"n_blocks": n_blocks, "k_test": k_test})

    out = manifests_dir / strategy_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "cpcv.json").write_text(json.dumps(dist, indent=2), encoding="utf-8")
    print(f"[cpcv] {strategy_id}: mean={dist['cpcv_mean_sharpe']} "
          f"p05={dist['cpcv_p05_sharpe']} paths={dist['n_paths']} -> cpcv.json")
    return 0


def _symbol_of(base_spec: dict) -> str:
    return str(base_spec.get("symbol", "BTC"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--blocks", type=int, default=DEFAULT_BLOCKS)
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--max", type=int, default=DEFAULT_MAX)
    args = ap.parse_args()
    sys.exit(run(args.strategy, args.blocks, args.k, args.max))


if __name__ == "__main__":
    main()
