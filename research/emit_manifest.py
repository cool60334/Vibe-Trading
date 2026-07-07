"""
research/emit_manifest.py
──────────────────────────
Strategy manifest emitter — Task 2.12.

Reads strategy_runs.json, aggregates metrics from multiple run directories,
computes gate pass/fail and red flags, and writes
    research/manifests/<strategy_id>/manifest.json
conforming to the StrategyManifest schema (dashboard/server/schemas.py).

Usage
-----
    # From repo root:
    python -m research.emit_manifest

    # From research/ directory:
    python emit_manifest.py
    python -m emit_manifest
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

# ── Path bootstrap ──────────────────────────────────────────────────────────────
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_DIR = _THIS_FILE.parent          # research/
_REPO_ROOT = _RESEARCH_DIR.parent          # repo root

for _p in (_RESEARCH_DIR,):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

# ── Dashboard schemas ───────────────────────────────────────────────────────────
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"
if str(_DASHBOARD_SCHEMAS) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_SCHEMAS))

# ── Internal imports ────────────────────────────────────────────────────────────
from pipeline.config import _REPO_ROOT as _CFG_REPO_ROOT, load_config  # noqa: E402
from pipeline.strategy_runs import StrategyRunsEntry, load_strategy_runs  # noqa: E402
from pipeline.stage3_backtest import symbol_to_short  # noqa: E402
from pipeline.stage3_diagnose import read_metrics_csv  # noqa: E402
from pipeline.stage4_optimize import expand_param_ranges  # noqa: E402
from lib.research_ledger import research_accounting_block  # noqa: E402

from schemas import (  # noqa: E402
    BacktestBlock,
    BacktestMetrics,
    BenchmarkBlock,
    CostStressBlock,
    CostStressLevel,
    CPCVBlock,
    DiagnosisBlock,
    FATAL_GATE_CHECKS,
    GATE_MAX_DRAWDOWN,
    GATE_MIN_CPCV_MEAN_SHARPE,
    GATE_MIN_CPCV_P05_SHARPE,
    GATE_MIN_DEFLATED_SHARPE,
    GATE_MIN_LAG_RETENTION,
    GATE_MIN_LAG_SHARPE,
    GATE_MIN_PROFIT_FACTOR,
    GATE_MIN_SHARPE,
    GATE_MIN_TRADES,
    GATE_MIN_WALK_FORWARD_SHARPE,
    GateBlock,
    GateThreshold,
    GenerationBlock,
    IntrabarAuditBlock,
    IntrabarAuditLevel,
    LagStressBlock,
    LagStressLevel,
    OptimizationBlock,
    RedFlagCode,
    RegimeMetrics,
    ReproducibilityBlock,
    SelectionManifest,
    SpecBlock,
    StrategyManifest,
)

# Use the config module's _REPO_ROOT as the canonical repo root.
# NOTE: this is its own module-level binding, independent of pipeline.stage5_select's
# `from pipeline.config import _REPO_ROOT` import. Patching one in a test does NOT
# patch the other — a test needing both emit_manifest and stage5_select to see a
# tmp_path repo root must patch both `emit_manifest._REPO_ROOT` and
# `pipeline.stage5_select._REPO_ROOT` explicitly.
_REPO_ROOT = _CFG_REPO_ROOT

# OOS trade gate — consistent with stage3-diag OOS fallback gate of 30
GATE_OOS_MIN_TRADES: int = 30


# ─── Data container ────────────────────────────────────────────────────────────


@dataclasses.dataclass
class ManifestCheckResult:
    """Result of verifying one strategy's manifest output."""

    strategy_id: str
    ok: bool
    error: Optional[str] = None


# ─── Pure-logic helpers ────────────────────────────────────────────────────────


def canonical_symbol(symbol: str) -> str:
    """Normalise a trading symbol to its base-coin form for dashboard grouping.

    The pipeline mixes conventions across runs — ``BTC``, ``BTC-USDT-SWAP``,
    ``BTC/USDT:USDT`` all denote the same coin. The dashboard groups strategies
    by ``symbol`` and joins them to factor manifests (keyed by base coin, e.g.
    ``factor_btc.json``), so manifests MUST use the base-coin form to avoid the
    coin appearing as two separate groups and to make the factor join resolve.

    Examples:
        ``BTC-USDT-SWAP`` -> ``BTC``;  ``BTC/USDT:USDT`` -> ``BTC``;  ``BTC`` -> ``BTC``.
    """
    s = symbol.strip().upper()
    for sep in ("-", "/"):
        if sep in s:
            return s.split(sep, 1)[0]
    return s


def metrics_csv_to_backtest_metrics(
    run_name: str,
    metrics_csv: Path,
) -> BacktestMetrics | None:
    """Read a metrics.csv and return a BacktestMetrics, or None if missing/unreadable.

    IMPORTANT: max_drawdown is stored as positive fraction in BacktestMetrics.
    The CSV may store it as a negative value; abs() is applied unconditionally.

    Args:
        run_name:    Run directory name (stored as source_run).
        metrics_csv: Path to the artifacts/metrics.csv file.

    Returns:
        BacktestMetrics if the CSV is readable; None otherwise.
    """
    row = read_metrics_csv(metrics_csv)
    if row is None:
        return None

    def _float(key: str) -> float | None:
        v = row.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def _int(key: str) -> int | None:
        v = row.get(key)
        if v is None:
            return None
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None

    raw_dd = _float("max_drawdown")
    max_dd = abs(raw_dd) if raw_dd is not None else None

    # Clamp to [0, 1] to satisfy BacktestMetrics schema (ge=0, le=1)
    if max_dd is not None and max_dd > 1.0:
        max_dd = 1.0

    sharpe = _float("sharpe")
    total_return = _float("total_return")
    win_rate = _float("win_rate")
    trades = _int("trades") if row.get("trades") is not None else _int("trade_count")
    profit_factor = _float("profit_factor")

    return BacktestMetrics(
        source_run=run_name,
        sharpe=sharpe,
        max_drawdown=max_dd,
        trades=trades,
        profit_factor=profit_factor,
        total_return=total_return,
        win_rate=win_rate,
    )


def compute_gate(
    backtest: BacktestBlock,
    optimization: "OptimizationBlock | None" = None,
    cpcv: "CPCVBlock | None" = None,
) -> GateBlock:
    """Compute GateBlock from a BacktestBlock.

    When OOS metrics are present, evaluates thresholds against OOS (held-out)
    data with walk-forward thresholds. Legacy path (no OOS) uses in-sample
    metrics and original thresholds unchanged.

    Args:
        backtest:     Assembled BacktestBlock (must have in_sample populated).
        optimization: Optional OptimizationBlock; when present and
                      deflated_sharpe is not None, a non-fatal DSR threshold
                      is appended.
        cpcv:         Optional CPCVBlock; when present, non-fatal CPCV
                      mean + p05 thresholds are appended.

    Returns:
        GateBlock with thresholds, overall_pass, fatal_fail, red_flags.
    """
    is_m = backtest.in_sample
    oos_m = backtest.oos

    oos_mode = oos_m is not None
    eval_metrics = oos_m if oos_mode else is_m

    thresholds: list[GateThreshold] = []

    # ── min_sharpe ──────────────────────────────────────────────────────────────
    sharpe_threshold = GATE_MIN_WALK_FORWARD_SHARPE if oos_mode else GATE_MIN_SHARPE
    sharpe_actual = eval_metrics.sharpe
    sharpe_passed = (sharpe_actual is not None) and (sharpe_actual >= sharpe_threshold)
    thresholds.append(GateThreshold(
        name="min_sharpe",
        threshold=sharpe_threshold,
        actual=sharpe_actual,
        passed=sharpe_passed,
        fatal=False,
    ))

    # ── max_drawdown ────────────────────────────────────────────────────────────
    dd_actual = eval_metrics.max_drawdown
    dd_passed = (dd_actual is not None) and (dd_actual <= GATE_MAX_DRAWDOWN)
    thresholds.append(GateThreshold(
        name="max_drawdown",
        threshold=GATE_MAX_DRAWDOWN,
        actual=dd_actual,
        passed=dd_passed,
        fatal=False,
    ))

    # ── min_trades ──────────────────────────────────────────────────────────────
    trades_threshold = GATE_OOS_MIN_TRADES if oos_mode else GATE_MIN_TRADES
    trades_actual = float(eval_metrics.trades) if eval_metrics.trades is not None else None
    trades_passed = (trades_actual is not None) and (trades_actual >= trades_threshold)
    thresholds.append(GateThreshold(
        name="min_trades",
        threshold=float(trades_threshold),
        actual=trades_actual,
        passed=trades_passed,
        fatal=False,
    ))

    # ── min_profit_factor ───────────────────────────────────────────────────────
    pf_actual = eval_metrics.profit_factor
    pf_passed = (pf_actual is not None) and (pf_actual >= GATE_MIN_PROFIT_FACTOR)
    thresholds.append(GateThreshold(
        name="min_profit_factor",
        threshold=GATE_MIN_PROFIT_FACTOR,
        actual=pf_actual,
        passed=pf_passed,
        fatal=False,
    ))

    # ── oos_sharpe_positive (FATAL) ─────────────────────────────────────────────
    oos_sharpe_actual = oos_m.sharpe if oos_m is not None else None
    oos_sharpe_passed = (oos_sharpe_actual is not None) and (oos_sharpe_actual > 0.0)
    thresholds.append(GateThreshold(
        name="oos_sharpe_positive",
        threshold=0.0,
        actual=oos_sharpe_actual,
        passed=oos_sharpe_passed,
        fatal=True,
    ))

    # ── alpha_not_fee_illusion (fatal only when stress data exists) ─────────────
    worst_stress_sharpe: float | None = None
    if backtest.cost_stress is not None and backtest.cost_stress.levels:
        stress_sharpes = [
            lvl.sharpe for lvl in backtest.cost_stress.levels if lvl.sharpe is not None
        ]
        if stress_sharpes:
            worst_stress_sharpe = min(stress_sharpes)
    has_stress = worst_stress_sharpe is not None
    afi_passed = has_stress and worst_stress_sharpe > 0.0
    thresholds.append(GateThreshold(
        name="alpha_not_fee_illusion",
        threshold=0.0,
        actual=worst_stress_sharpe,
        passed=afi_passed,
        fatal=has_stress,
    ))

    # ── deflated_sharpe (multiple-testing haircut; non-fatal) ───────────────────
    if optimization is not None and optimization.deflated_sharpe is not None:
        dsr_actual = optimization.deflated_sharpe
        thresholds.append(GateThreshold(
            name="deflated_sharpe",
            threshold=GATE_MIN_DEFLATED_SHARPE,
            actual=dsr_actual,
            passed=dsr_actual >= GATE_MIN_DEFLATED_SHARPE,
            fatal=False,
        ))

    # ── CPCV distribution gates (non-fatal) ─────────────────────────────────────
    if cpcv is not None:
        if cpcv.cpcv_mean_sharpe is not None:
            thresholds.append(GateThreshold(
                name="cpcv_mean_sharpe",
                threshold=GATE_MIN_CPCV_MEAN_SHARPE,
                actual=cpcv.cpcv_mean_sharpe,
                passed=cpcv.cpcv_mean_sharpe >= GATE_MIN_CPCV_MEAN_SHARPE,
                fatal=False,
            ))
        if cpcv.cpcv_p05_sharpe is not None:
            thresholds.append(GateThreshold(
                name="cpcv_p05_sharpe",
                threshold=GATE_MIN_CPCV_P05_SHARPE,
                actual=cpcv.cpcv_p05_sharpe,
                passed=cpcv.cpcv_p05_sharpe > GATE_MIN_CPCV_P05_SHARPE,
                fatal=False,
            ))

    # ── edge_survives_lag (execution-delay dual gate; non-fatal) ─────────────────
    if backtest.lag_stress is not None and backtest.lag_stress.levels:
        lvls = backtest.lag_stress.levels
        lag_sharpes = [l.sharpe for l in lvls if l.sharpe is not None]
        worst_lag = min(lag_sharpes) if lag_sharpes else None
        retentions: list[float] = []
        for l in lvls:
            base = is_m.sharpe if l.window in ("train", "full") else (backtest.oos.sharpe if backtest.oos else None)
            if l.sharpe is not None and base is not None and base > 0:
                retentions.append(l.sharpe / base)
        worst_ret = min(retentions) if retentions else None
        floor_ok = worst_lag is not None and worst_lag >= GATE_MIN_LAG_SHARPE
        ret_ok = worst_ret is None or worst_ret >= GATE_MIN_LAG_RETENTION
        thresholds.append(GateThreshold(
            name="edge_survives_lag", threshold=GATE_MIN_LAG_SHARPE,
            actual=worst_lag, passed=(floor_ok and ret_ok), fatal=False))

    overall_pass = all(t.passed for t in thresholds)
    fatal_fail = any(t.fatal and not t.passed for t in thresholds)

    red_flags = derive_red_flags(backtest)

    return GateBlock(
        source_run=is_m.source_run,
        thresholds=thresholds,
        overall_pass=overall_pass,
        fatal_fail=fatal_fail,
        red_flags=red_flags,
    )


def derive_red_flags(backtest: BacktestBlock) -> list[RedFlagCode]:
    """Derive red flag codes from a BacktestBlock.

    Args:
        backtest: Assembled BacktestBlock.

    Returns:
        Sorted list of RedFlagCode values.
    """
    flags: list[RedFlagCode] = []
    is_m = backtest.in_sample
    oos_m = backtest.oos

    is_sharpe = is_m.sharpe
    oos_sharpe = oos_m.sharpe if oos_m is not None else None
    trades = is_m.trades

    # oos_sharpe_far_below_is: oos_sharpe < 0.5 * is_sharpe (when IS > 0)
    if (
        is_sharpe is not None
        and oos_sharpe is not None
        and is_sharpe > 0
        and oos_sharpe < 0.5 * is_sharpe
    ):
        flags.append(RedFlagCode.OOS_SHARPE_FAR_BELOW_IS)

    # underperforms_hodl: strategy return did not beat buy-and-hold
    if backtest.benchmark is not None and backtest.benchmark.beats_hodl is not None:
        if not backtest.benchmark.beats_hodl:
            flags.append(RedFlagCode.UNDERPERFORMS_HODL)

    # too_few_trades: trades < 100
    if trades is not None and trades < GATE_MIN_TRADES:
        flags.append(RedFlagCode.TOO_FEW_TRADES)

    # alpha_is_fee_illusion: at fee_multiplier >= 2, sharpe drops > 50% from IS
    if (
        backtest.cost_stress is not None
        and backtest.cost_stress.levels
        and is_sharpe is not None
        and is_sharpe > 0
    ):
        for lvl in backtest.cost_stress.levels:
            if lvl.fee_multiplier >= 2.0 and lvl.sharpe is not None:
                drop_fraction = (is_sharpe - lvl.sharpe) / is_sharpe
                if drop_fraction > 0.5:
                    if RedFlagCode.ALPHA_IS_FEE_ILLUSION not in flags:
                        flags.append(RedFlagCode.ALPHA_IS_FEE_ILLUSION)
                    break

    # overfit_suspect: IS/OOS ratio > 2 (both positive) or OOS <= 0 while IS > 0
    if is_sharpe is not None and oos_sharpe is not None and is_sharpe > 0:
        if oos_sharpe > 0 and (is_sharpe / oos_sharpe) > 2.0:
            flags.append(RedFlagCode.OVERFIT_SUSPECT)
        elif oos_sharpe <= 0:
            flags.append(RedFlagCode.OVERFIT_SUSPECT)

    # regime_conditional: majority of regimes (> 50%) show non-positive sharpe
    if backtest.by_regime:
        regime_sharpes = [rm.sharpe for rm in backtest.by_regime if rm.sharpe is not None]
        if regime_sharpes:
            negative_count = sum(1 for s in regime_sharpes if s <= 0.0)
            if negative_count > len(regime_sharpes) / 2:
                flags.append(RedFlagCode.REGIME_CONDITIONAL)

    # Sort by value for deterministic output
    return sorted(set(flags), key=lambda x: x.value)


def cpcv_required_for(parameter_search_ranges: dict) -> bool:
    """True when the sweep grid has >1 combo (CPCV can re-select per path).
    A {} or single-value grid (<=1 combo) is exempt (agy: total_combos <= 1)."""
    expanded = expand_param_ranges(parameter_search_ranges or {})
    total = 1
    for vals in expanded.values():
        total *= max(len(vals), 1)
    return len(expanded) > 0 and total > 1


def compute_not_tested(gate: GateBlock, cpcv_required: bool) -> list[str]:
    """Names of required validations whose DATA is absent. Empty = all present."""
    by = {t.name: t for t in gate.thresholds}
    afi = by.get("alpha_not_fee_illusion")
    stress_present = afi is not None and afi.actual is not None
    cpcv_present = "cpcv_mean_sharpe" in by
    out: list[str] = []
    if not stress_present:
        out.append("cost_stress")
    if cpcv_required and not cpcv_present:
        out.append("cpcv")
    return out


def required_validations_ok(gate: GateBlock) -> bool:
    """True iff no required validation data is missing AND every present
    cost-stress / CPCV threshold passed. Drives selection + is the promote
    NOT_TESTED source of truth (via gate.not_tested for the missing case)."""
    if gate.not_tested:
        return False
    by = {t.name: t for t in gate.thresholds}
    afi = by.get("alpha_not_fee_illusion")
    if afi is None or not afi.passed:
        return False
    for name in ("cpcv_mean_sharpe", "cpcv_p05_sharpe"):
        t = by.get(name)
        if t is not None and not t.passed:
            return False
    return True


def _benchmark_from_row(source_run: str, row: dict) -> BenchmarkBlock | None:
    """Build BenchmarkBlock from a metrics.csv row dict.

    Returns None if the row has no benchmark_return column (run had no benchmark).

    Args:
        source_run: Run name stored in BenchmarkBlock.source_run.
        row:        Dict returned by read_metrics_csv().

    Returns:
        BenchmarkBlock if benchmark_return column present; None otherwise.
    """
    def _f(key: str) -> float | None:
        v = row.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    hodl_ret = _f("benchmark_return")
    if hodl_ret is None:
        return None

    strategy_ret = _f("total_return")
    excess = _f("excess_return")
    beats_hodl: bool | None = (
        (strategy_ret > hodl_ret)
        if strategy_ret is not None
        else None
    )
    return BenchmarkBlock(
        source_run=source_run,
        strategy_return=strategy_ret,
        hodl_return=hodl_ret,
        excess_return=excess,
        beats_hodl=beats_hodl,
    )


def build_spec_block(
    strategy_id: str,
    symbol: str,
    spec_yaml_path: Path | None,
) -> SpecBlock:
    """Build a SpecBlock from strategy metadata.

    Args:
        strategy_id:    Strategy identifier.
        symbol:         Trading symbol.
        spec_yaml_path: Absolute path to the spec YAML file (may be None).

    Returns:
        SpecBlock instance.
    """
    if spec_yaml_path is not None:
        try:
            spec_yaml_str = str(spec_yaml_path.relative_to(_REPO_ROOT))
            # Normalise to forward slashes for cross-platform JSON stability
            spec_yaml_str = spec_yaml_str.replace("\\", "/")
        except ValueError:
            spec_yaml_str = str(spec_yaml_path)
    else:
        spec_yaml_str = f"research/strategies/{strategy_id}.yaml"

    return SpecBlock(
        strategy_id=strategy_id,
        symbol=symbol,
        spec_yaml=spec_yaml_str,
    )


def read_cost_model_version(entry: StrategyRunsEntry, runs_root: Path) -> str:
    """Read cost_model_version from the base_run's config.json (agy #4).

    stage3_backtest.build_run_config() tags every run's config.json with
    "v2_realistic" (default, opts into the realistic cost model from Tasks
    1-2) or "v1_legacy" (debug backdoor via ``legacy_costs: true``). This is
    the field stage5_select.assert_uniform_cost_model() reads (via the
    manifest) to block cross-strategy comparisons that mix cost-model
    versions.

    Defaults to "v1_legacy" when base_run is unset, config.json is missing/
    unreadable, or config.json predates this field (runs from before this
    initiative) -- consistent with build_run_config's own legacy-costs
    default and the guard's fallback.

    Args:
        entry:     StrategyRunsEntry from strategy_runs.json.
        runs_root: <repo_root>/runs/ directory.

    Returns:
        "v2_realistic" or "v1_legacy" (or whatever string config.json has,
        defaulting to "v1_legacy" if the key or file is absent).
    """
    if entry.base_run is None:
        return "v1_legacy"
    config_path = runs_root / entry.base_run / "config.json"
    config_data = _load_json_block(config_path)
    if config_data is None:
        return "v1_legacy"
    return config_data.get("cost_model_version", "v1_legacy")


def build_backtest_block(
    entry: StrategyRunsEntry,
    runs_root: Path,
) -> BacktestBlock | None:
    """Aggregate backtest metrics from multiple run directories into a BacktestBlock.

    Returns None if base_run is missing or its metrics.csv is unreadable.

    Args:
        entry:     StrategyRunsEntry from strategy_runs.json.
        runs_root: <repo_root>/runs/ directory.

    Returns:
        BacktestBlock or None.
    """
    if entry.base_run is None:
        return None

    # ── In-sample from base_run ─────────────────────────────────────────────────
    base_metrics_path = runs_root / entry.base_run / "artifacts" / "metrics.csv"
    in_sample = metrics_csv_to_backtest_metrics(entry.base_run, base_metrics_path)
    if in_sample is None:
        return None

    # ── Benchmark (HODL comparison) from base_run ───────────────────────────────
    base_row = read_metrics_csv(base_metrics_path)
    benchmark: BenchmarkBlock | None = (
        _benchmark_from_row(entry.base_run, base_row) if base_row is not None else None
    )

    # ── OOS: oos_runs takes precedence over walk_forward_runs ───────────────
    oos: BacktestMetrics | None = None
    oos_run_name: str | None = None
    if entry.oos_runs:
        oos_run_name = entry.oos_runs[0]
    elif entry.walk_forward_runs:
        oos_run_name = entry.walk_forward_runs[0]
    if oos_run_name is not None:
        oos_metrics_path = runs_root / oos_run_name / "artifacts" / "metrics.csv"
        oos = metrics_csv_to_backtest_metrics(oos_run_name, oos_metrics_path)

    # ── By-regime from regime_runs ──────────────────────────────────────────────
    by_regime: list[RegimeMetrics] = []
    for regime_label, regime_run_name in entry.regime_runs.items():
        regime_metrics_path = runs_root / regime_run_name / "artifacts" / "metrics.csv"
        row = read_metrics_csv(regime_metrics_path)
        if row is None:
            continue

        def _float_r(key: str) -> float | None:
            v = row.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        def _int_r(key: str) -> int | None:
            v = row.get(key)
            if v is None:
                return None
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return None

        raw_dd = _float_r("max_drawdown")
        max_dd = abs(raw_dd) if raw_dd is not None else None

        trades_r = _int_r("trades") if row.get("trades") is not None else _int_r("trade_count")

        by_regime.append(RegimeMetrics(
            regime=regime_label,
            source_run=regime_run_name,
            sharpe=_float_r("sharpe"),
            max_drawdown=max_dd,
            total_return=_float_r("total_return"),
            trades=trades_r,
        ))

    # ── Cost-stress from stress_runs ─────────────────────────────────────────────
    cost_stress: CostStressBlock | None = None
    stress_levels: list[CostStressLevel] = []
    stress_source_run: str | None = None

    for stress_label, stress_run_name in entry.stress_runs.items():
        stress_metrics_path = runs_root / stress_run_name / "artifacts" / "metrics.csv"
        row = read_metrics_csv(stress_metrics_path)
        if row is None:
            continue
        if stress_source_run is None:
            stress_source_run = stress_run_name

        def _float_s(key: str, r: dict = row) -> float | None:
            v = r.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        # Parse fee_multiplier from label (e.g. "3x_fees" -> 3.0); fall back
        # to CSV column if label has no Nx pattern; default 1.0.
        label_match = re.search(r"(\d+(?:\.\d+)?)x", stress_label.lower())
        fee_mult: float
        if label_match:
            fee_mult = float(label_match.group(1))
        else:
            fee_mult_raw = _float_s("fee_multiplier")
            fee_mult = fee_mult_raw if fee_mult_raw is not None else 1.0

        stress_levels.append(CostStressLevel(
            label=stress_label,
            source_run=stress_run_name,
            fee_multiplier=fee_mult,
            sharpe=_float_s("sharpe"),
            total_return=_float_s("total_return"),
            profit_factor=_float_s("profit_factor"),
        ))

    if stress_levels and stress_source_run is not None:
        cost_stress = CostStressBlock(
            source_run=stress_source_run,
            levels=stress_levels,
        )

    # ── Lag-stress from lag_stress_runs ─────────────────────────────────────────
    lag_stress: LagStressBlock | None = None
    lag_levels: list[LagStressLevel] = []
    lag_source_run: str | None = None

    for lag_label, lag_run_name in entry.lag_stress_runs.items():
        lag_metrics_path = runs_root / lag_run_name / "artifacts" / "metrics.csv"
        row = read_metrics_csv(lag_metrics_path)
        if row is None:
            continue
        if lag_source_run is None:
            lag_source_run = lag_run_name

        # Labels are minted by stage3_backtest as f"lag{N}_{window}" (e.g.
        # "lag1_train", "lag2_oos") -- parse lag_bars/window from the LABEL,
        # not the run name (unlike cost_stress's fee-multiplier regex).
        label_match = re.match(r"lag(\d+)_(\w+)", lag_label)
        if not label_match:
            continue

        def _float_l(key: str, r: dict = row) -> float | None:
            v = r.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        lag_levels.append(LagStressLevel(
            label=lag_label,
            source_run=lag_run_name,
            lag_bars=int(label_match.group(1)),
            window=label_match.group(2),
            sharpe=_float_l("sharpe"),
        ))

    if lag_levels and lag_source_run is not None:
        lag_stress = LagStressBlock(
            source_run=lag_source_run,
            levels=lag_levels,
        )

    # ── Intrabar stop-audit from intrabar_audit_runs ─────────────────────────────
    intrabar_audit: IntrabarAuditBlock | None = None
    audit_levels: list[IntrabarAuditLevel] = []
    audit_source_run: str | None = None

    for _win, audit_run_name in entry.intrabar_audit_runs.items():
        audit_path = runs_root / audit_run_name / "artifacts" / "intrabar_audit.json"
        payload = _load_json_block(audit_path)
        if payload is None:
            continue
        if audit_source_run is None:
            audit_source_run = audit_run_name
        audit_levels.append(IntrabarAuditLevel(
            window=payload.get("window", _win),
            source_run=audit_run_name,
            n_trades=payload.get("n_trades"),
            n_breached=payload.get("n_breached"),
            breached_pct=payload.get("breached_pct"),
            n_optimistic=payload.get("n_optimistic"),
            mean_breach_depth_pct=payload.get("mean_breach_depth_pct"),
            max_breach_depth_pct=payload.get("max_breach_depth_pct"),
        ))

    if audit_levels and audit_source_run is not None:
        intrabar_audit = IntrabarAuditBlock(
            source_run=audit_source_run,
            levels=audit_levels,
        )

    return BacktestBlock(
        in_sample=in_sample,
        oos=oos,
        by_regime=by_regime,
        cost_stress=cost_stress,
        lag_stress=lag_stress,
        intrabar_audit=intrabar_audit,
        benchmark=benchmark,
    )


def _load_json_block(path: Path) -> dict | None:
    """Read a JSON file and return a dict, or None if missing/unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _determine_pipeline_stage(
    strategy_id: str,
    manifests_dir: Path,
) -> int:
    """Determine the highest pipeline stage completed for a strategy.

    Args:
        strategy_id:   Strategy identifier.
        manifests_dir: research/manifests/ directory.

    Returns:
        Integer 2-5: highest stage completed (2 = generation baseline).
    """
    strategy_dir = manifests_dir / strategy_id

    # Stage 5: selection.json exists and strategy selected=True
    selection_path = manifests_dir / "selection.json"
    if selection_path.exists():
        try:
            sel_data = json.loads(selection_path.read_text(encoding="utf-8"))
            sel_manifest = SelectionManifest.model_validate(sel_data)
            for entry in sel_manifest.ranking:
                if entry.strategy_id == strategy_id and entry.selected:
                    return 5
        except Exception:  # noqa: BLE001
            pass

    # Stage 4: optimization.json exists
    if (strategy_dir / "optimization.json").exists():
        return 4

    # Stage 3: diagnosis.json exists
    if (strategy_dir / "diagnosis.json").exists():
        return 3

    # Default to 2 (generation always precedes emit_manifest)
    return 2


def build_strategy_manifest(
    strategy_id: str,
    symbol: str,
    entry: StrategyRunsEntry,
    runs_root: Path,
    manifests_dir: Path,
) -> dict:
    """Assemble all blocks into a StrategyManifest dict.

    Loads generation.json, diagnosis.json, optimization.json if they exist.
    Computes gate and red_flags when a backtest block is available.

    Args:
        strategy_id:   Strategy identifier.
        symbol:        Trading symbol.
        entry:         StrategyRunsEntry from strategy_runs.json.
        runs_root:     <repo_root>/runs/ directory.
        manifests_dir: research/manifests/ directory.

    Returns:
        Plain dict ready for JSON serialisation (via Pydantic model_dump).
    """
    strategy_dir = manifests_dir / strategy_id

    # Short lowercase symbol (e.g. "eth") -- matches the key stage0a/stage4 write
    # to the research ledger (SymbolConfig.name / symbol_to_short()), NOT the
    # canonical_symbol() uppercase base-coin form used for dashboard grouping.
    short_symbol = symbol_to_short(symbol)

    # Canonicalise symbol to base coin (BTC-USDT-SWAP -> BTC) so the dashboard
    # groups by coin and the factor-manifest join (factor_<coin>.json) resolves.
    symbol = canonical_symbol(symbol)

    # ── SpecBlock ───────────────────────────────────────────────────────────────
    spec_yaml_path = _REPO_ROOT / entry.spec_yaml if entry.spec_yaml else None
    spec = build_spec_block(strategy_id, symbol, spec_yaml_path)

    # ── Optional JSON blocks ────────────────────────────────────────────────────
    generation_raw = _load_json_block(strategy_dir / "generation.json")
    generation: GenerationBlock | None = None
    if generation_raw is not None:
        try:
            generation = GenerationBlock.model_validate(generation_raw)
        except Exception:  # noqa: BLE001
            generation = None

    reproducibility_raw = _load_json_block(strategy_dir / "reproducibility.json")
    reproducibility: ReproducibilityBlock | None = None
    if reproducibility_raw is not None:
        try:
            reproducibility = ReproducibilityBlock.model_validate(reproducibility_raw)
        except Exception:  # noqa: BLE001
            reproducibility = None

    diagnosis_raw = _load_json_block(strategy_dir / "diagnosis.json")
    diagnosis: DiagnosisBlock | None = None
    if diagnosis_raw is not None:
        try:
            diagnosis = DiagnosisBlock.model_validate(diagnosis_raw)
        except Exception:  # noqa: BLE001
            diagnosis = None

    optimization_raw = _load_json_block(strategy_dir / "optimization.json")
    optimization: OptimizationBlock | None = None
    if optimization_raw is not None:
        try:
            optimization = OptimizationBlock.model_validate(optimization_raw)
        except Exception:  # noqa: BLE001
            optimization = None

    cpcv_raw = _load_json_block(strategy_dir / "cpcv.json")
    cpcv: CPCVBlock | None = None
    if cpcv_raw is not None:
        try:
            cpcv = CPCVBlock.model_validate(cpcv_raw)
        except Exception:  # noqa: BLE001
            cpcv = None

    # ── BacktestBlock ───────────────────────────────────────────────────────────
    backtest = build_backtest_block(entry, runs_root)

    # ── GateBlock ───────────────────────────────────────────────────────────────
    gate: GateBlock | None = None
    if backtest is not None:
        gate = compute_gate(backtest, optimization, cpcv)
        if spec_yaml_path is not None and spec_yaml_path.exists():
            spec_doc = yaml.safe_load(spec_yaml_path.read_text(encoding="utf-8")) or {}
            psr = spec_doc.get("parameter_search_ranges", {}) or {}
        else:
            psr = {}
        if not isinstance(psr, dict):
            psr = {}
        gate.not_tested = compute_not_tested(gate, cpcv_required_for(psr))

    # ── Research ledger accounting (funnel-wide trial counters) ─────────────────
    # Informational only: red_flags never feeds overall_pass/fatal_fail (those
    # are derived solely from thresholds[].passed/.fatal at GateBlock construction
    # time, above). This surfaces holdout-freshness debt without hard-blocking.
    accounting = research_accounting_block(manifests_dir, short_symbol)
    if gate is not None:
        oos_n = accounting.get("oos_evals_symbol", 0)
        if oos_n >= 4:
            # dict.fromkeys dedups while preserving insertion order -- existing
            # flags (in derive_red_flags' check-order) are left untouched; the
            # new flag is appended at the end, and this is a no-op if already
            # present. Do NOT alphabetically re-sort here (that would reorder
            # the pre-existing flags every time this fires).
            gate.red_flags = list(
                dict.fromkeys([*gate.red_flags, RedFlagCode.OOS_WINDOW_OVEREVALUATED])
            )

    # ── pipeline_stage ──────────────────────────────────────────────────────────
    pipeline_stage = _determine_pipeline_stage(strategy_id, manifests_dir)

    # ── cost_model_version (agy #4 — cross-strategy comparison guard) ──────────
    cost_model_version = read_cost_model_version(entry, runs_root)

    # ── Assemble manifest ───────────────────────────────────────────────────────
    manifest = StrategyManifest(
        strategy_id=strategy_id,
        symbol=symbol,
        generated_at=datetime.now(timezone.utc),
        pipeline_stage=pipeline_stage,
        spec=spec,
        generation=generation,
        reproducibility=reproducibility,
        backtest=backtest,
        optimization=optimization,
        cpcv=cpcv,
        diagnosis=diagnosis,
        gate=gate,
        research_accounting=accounting,
        cost_model_version=cost_model_version,
    )

    return json.loads(manifest.model_dump_json())


def emit_manifest_for_strategy(
    strategy_id: str,
    entry: StrategyRunsEntry,
    runs_root: Path,
    manifests_dir: Path,
) -> Path:
    """Build and write manifest.json for one strategy.

    Calls build_strategy_manifest(...), creates the output directory if
    needed, serialises the result to JSON, and returns the written path.

    Args:
        strategy_id:   Strategy identifier.
        entry:         StrategyRunsEntry from strategy_runs.json.
        runs_root:     <repo_root>/runs/ directory.
        manifests_dir: research/manifests/ directory.

    Returns:
        Path to the written manifest.json file.
    """
    manifest_dict = build_strategy_manifest(
        strategy_id=strategy_id,
        symbol=entry.symbol,
        entry=entry,
        runs_root=runs_root,
        manifests_dir=manifests_dir,
    )
    out_dir = manifests_dir / strategy_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "manifest.json"
    out_path.write_text(json.dumps(manifest_dict, indent=2, default=str), encoding="utf-8")
    return out_path


def verify_manifest(manifest_path: Path) -> ManifestCheckResult:
    """Verify that manifest.json exists and validates against StrategyManifest schema.

    Args:
        manifest_path: Path to the <strategy_id>/manifest.json file.

    Returns:
        ManifestCheckResult with ok=True if valid; ok=False otherwise.
    """
    strategy_id = manifest_path.parent.name

    if not manifest_path.exists():
        return ManifestCheckResult(
            strategy_id=strategy_id,
            ok=False,
            error=f"manifest.json missing: {manifest_path}",
        )

    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return ManifestCheckResult(
            strategy_id=strategy_id,
            ok=False,
            error=f"manifest.json invalid JSON: {exc}",
        )

    try:
        StrategyManifest.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        return ManifestCheckResult(
            strategy_id=strategy_id,
            ok=False,
            error=f"manifest.json schema validation failed: {exc}",
        )

    return ManifestCheckResult(strategy_id=strategy_id, ok=True)


def compute_exit_code(results: list[ManifestCheckResult]) -> int:
    """Return 0 if at least one result was produced and all are ok; 1 otherwise.

    Args:
        results: List of ManifestCheckResult from verify_manifest() calls.

    Returns:
        0 on full success (>=1 result, all ok); 1 otherwise.
    """
    if not results:
        return 1
    return 0 if all(r.ok for r in results) else 1


def print_summary(results: list[ManifestCheckResult]) -> None:
    """Print a human-readable per-strategy manifest summary to stdout.

    Args:
        results: List of ManifestCheckResult.
    """
    print("\n" + "=" * 60)
    print("Manifest emission output verification summary")
    print("=" * 60)
    if not results:
        print("  (no strategies were processed)")

    for r in results:
        status = "OK" if r.ok else "FAIL"
        if r.ok:
            msg = "manifest.json present and valid"
        else:
            msg = f"FAILED — {r.error}"
        print(f"  [{status}] {r.strategy_id}: {msg}")

    total = len(results)
    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{total} manifests passed.")
    if total == 0 or passed < total:
        print("Manifest emission FAILED: no strategies were processed or one or more failed.")
    else:
        print("Manifest emission PASSED: all strategies have valid manifest.json.")
    print("=" * 60)


# ─── Stage orchestration ───────────────────────────────────────────────────────


def main() -> None:
    """Manifest emission entry point: orchestrate, verify, report, exit."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Emit StrategyManifest JSON for one or all strategies.",
    )
    parser.add_argument(
        "--strategy-id",
        metavar="ID",
        help="Emit manifest for a single strategy ID only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate manifest construction but do not write any files.",
    )
    args = parser.parse_args()

    runs_map = load_strategy_runs()

    runs_root = _REPO_ROOT / "runs"
    from lib.timeframe import active_manifests_dir
    manifests_dir = active_manifests_dir()

    print("=" * 60)
    print("emit_manifest — Strategy Manifest Emitter")
    print("=" * 60)
    if args.dry_run:
        print("Mode:       DRY-RUN (no files written)")
    print(f"Strategies: {list(runs_map.entries.keys())}")
    print(f"Runs root:  {runs_root}")
    print(f"Manifests:  {manifests_dir}")

    all_results: list[ManifestCheckResult] = []

    if not runs_map.entries:
        print("[emit_manifest] WARNING: no strategies found in strategy_runs.json.")
        sys.exit(1)

    if args.strategy_id and args.strategy_id not in runs_map.entries:
        print(
            f"[emit_manifest] ERROR: strategy_id {args.strategy_id!r} not in strategy_runs.json",
            file=sys.stderr,
        )
        sys.exit(1)

    entries_to_process = (
        {args.strategy_id: runs_map.entries[args.strategy_id]}
        if args.strategy_id
        else dict(runs_map.entries)
    )

    for strategy_id, entry in entries_to_process.items():
        print(f"\n{'=' * 60}")
        print(f"[emit_manifest] Strategy: {strategy_id}")
        print(f"{'=' * 60}")

        try:
            if args.dry_run:
                # Validate manifest construction without writing any files.
                build_strategy_manifest(
                    strategy_id=strategy_id,
                    symbol=entry.symbol,
                    entry=entry,
                    runs_root=runs_root,
                    manifests_dir=manifests_dir,
                )
                print(f"  [DRY-RUN] manifest valid, not written")
                result = ManifestCheckResult(strategy_id=strategy_id, ok=True)
            else:
                out_path = emit_manifest_for_strategy(
                    strategy_id=strategy_id,
                    entry=entry,
                    runs_root=runs_root,
                    manifests_dir=manifests_dir,
                )
                print(f"  [OK] wrote {out_path}")
                result = verify_manifest(out_path)
        except Exception as exc:  # noqa: BLE001
            result = ManifestCheckResult(
                strategy_id=strategy_id,
                ok=False,
                error=f"unexpected error: {exc}",
            )
            print(f"  [ERROR] {strategy_id}: {exc}")

        all_results.append(result)

    print_summary(all_results)
    sys.exit(compute_exit_code(all_results))


if __name__ == "__main__":
    main()
