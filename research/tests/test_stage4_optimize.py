"""Tests for stage4_optimize deterministic grid-sweep helpers.

Covers the *pure* functions that drive the stage-4 sweep — no subprocess,
no backtest, no filesystem:

  - expand_param_ranges      [lo,hi,step] / [v] → discrete value lists
  - sample_combos            seeded cartesian sampling, centroid-first
  - _rewrite_percentile_condition / _rewrite_invalidation_lookback
  - apply_overrides_to_spec  spec override application
  - rank_combos              trade-count gate + sharpe ranking
  - build_optimization_block OptimizationBlock payload
  - ComboResult.sharpe / .trade_count properties

Pytest is run from research/ as:
    cd research && python -m pytest tests/test_stage4_optimize.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Bootstrap: research/ and dashboard/server/ must be on sys.path.
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_DIR = _THIS_FILE.parents[1]   # research/
_REPO_ROOT = _RESEARCH_DIR.parent       # repo root
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"
for _p in (_RESEARCH_DIR, _REPO_ROOT, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from pipeline.stage4_optimize import (  # noqa: E402
    OPTIMIZATION_METHOD,
    MIN_TRADE_COUNT_GATE,
    ComboResult,
    apply_overrides_to_spec,
    build_optimization_block,
    expand_param_ranges,
    rank_combos,
    sample_combos,
    _rewrite_invalidation_lookback,
    _rewrite_percentile_condition,
)
from pipeline.stage2_strategies import _default_spec_scaffold  # noqa: E402
from schemas import StrategySpec  # noqa: E402
from lib.signal_compiler import compile_strategy  # noqa: E402


# ---------------------------------------------------------------------------
# expand_param_ranges
# ---------------------------------------------------------------------------


class TestExpandParamRanges:
    def test_int_triple_expands_inclusive_with_int_values(self):
        out = expand_param_ranges({"lookback_days": [60, 120, 30]})
        assert out == {"lookback_days": [60, 90, 120]}
        assert all(isinstance(v, int) for v in out["lookback_days"])

    def test_float_triple_preserves_floats(self):
        out = expand_param_ranges({"tp_pct": [4.0, 7.0, 1.5]})
        assert out == {"tp_pct": [4.0, 5.5, 7.0]}

    def test_single_value_passthrough(self):
        assert expand_param_ranges({"x": [42]}) == {"x": [42]}

    def test_upper_bound_is_inclusive(self):
        # 75,80,85,90 — the epsilon in the loop must include the hi endpoint.
        assert expand_param_ranges({"p": [75, 90, 5]}) == {"p": [75, 80, 85, 90]}

    def test_malformed_specs_skipped(self):
        out = expand_param_ranges({
            "len2": [1, 2],          # wrong length
            "len4": [1, 2, 3, 4],    # wrong length
            "scalar": 5,             # not a list/tuple
            "nonnum": ["a", "b", "c"],
            "zero_step": [0, 10, 0],
            "neg_step": [0, 10, -1],
            "hi_lt_lo": [10, 0, 1],
            "good": [0, 2, 1],
        })
        assert out == {"good": [0, 1, 2]}

    def test_empty_input_returns_empty(self):
        assert expand_param_ranges({}) == {}


# ---------------------------------------------------------------------------
# sample_combos
# ---------------------------------------------------------------------------


class TestSampleCombos:
    def test_empty_expanded_returns_empty(self):
        assert sample_combos({}, max_n=10, seed=1) == []

    def test_full_grid_returned_when_smaller_than_max(self):
        expanded = {"a": [1, 2, 3]}
        combos = sample_combos(expanded, max_n=10, seed=7)
        assert len(combos) == 3
        # All distinct a-values present
        assert {c["a"] for c in combos} == {1, 2, 3}

    def test_centroid_is_first(self):
        # median of [1,2,3] is index 3//2 = 1 → value 2
        combos = sample_combos({"a": [1, 2, 3]}, max_n=10, seed=7)
        assert combos[0] == {"a": 2}

    def test_capped_at_max_n_and_centroid_first(self):
        expanded = {"a": [1, 2, 3, 4, 5], "b": [10, 20, 30]}  # 15 combos
        combos = sample_combos(expanded, max_n=5, seed=3)
        assert len(combos) == 5
        # centroid: a[5//2]=a[2]=3, b[3//2]=b[1]=20
        assert combos[0] == {"a": 3, "b": 20}

    def test_deterministic_for_same_seed(self):
        expanded = {"a": [1, 2, 3], "b": [4, 5, 6]}
        first = sample_combos(expanded, max_n=9, seed=99)
        second = sample_combos(expanded, max_n=9, seed=99)
        assert first == second

    def test_keys_are_sorted_within_each_combo(self):
        combos = sample_combos({"b": [1], "a": [2]}, max_n=1, seed=1)
        assert list(combos[0].keys()) == ["a", "b"]


# ---------------------------------------------------------------------------
# _rewrite_percentile_condition / _rewrite_invalidation_lookback
# ---------------------------------------------------------------------------


class TestRewritePercentileCondition:
    def test_override_value_only(self):
        out = _rewrite_percentile_condition(
            "funding_z_percentile_90d >= 80 persist 2/3", new_value=85
        )
        assert out == "funding_z_percentile_90d >= 85 persist 2/3"

    def test_override_lookback_only(self):
        out = _rewrite_percentile_condition(
            "funding_z_percentile_90d >= 80 persist 2/3", new_lookback=60
        )
        assert out == "funding_z_percentile_60d >= 80 persist 2/3"

    def test_override_persist(self):
        out = _rewrite_percentile_condition(
            "x_percentile_90d <= 20 persist 2/3", new_persist_m=3, new_persist_n=5
        )
        assert out == "x_percentile_90d <= 20 persist 3/5"

    def test_float_value_keeps_decimal(self):
        out = _rewrite_percentile_condition(
            "x_percentile_90d >= 80 persist 2/3", new_value=12.5
        )
        assert out == "x_percentile_90d >= 12.5 persist 2/3"

    def test_condition_without_persist_roundtrips(self):
        out = _rewrite_percentile_condition("x_percentile_30d < 50")
        assert out == "x_percentile_30d < 50"

    def test_non_percentile_condition_raises(self):
        with pytest.raises(ValueError):
            _rewrite_percentile_condition("close > sma_200")


class TestRewriteInvalidationLookback:
    def test_rewrites_lookback_keeps_band(self):
        out = _rewrite_invalidation_lookback(
            "funding_z_percentile_90d between 40,60", new_lookback=60
        )
        assert out == "funding_z_percentile_60d between 40,60"

    def test_non_invalidation_expr_raises(self):
        with pytest.raises(ValueError):
            _rewrite_invalidation_lookback("funding_z_percentile_90d <= 20", 60)


# ---------------------------------------------------------------------------
# apply_overrides_to_spec
# ---------------------------------------------------------------------------


def _base_spec() -> dict:
    return {
        "name": "test",
        "entry_long": {
            "logic": "all",
            "conditions": ["funding_z_percentile_90d <= 20 persist 2/3"],
        },
        "entry_short": {
            "logic": "all",
            "conditions": ["funding_z_percentile_90d >= 80 persist 2/3"],
        },
        "exit_rules": [
            {"condition": "time_based", "max_hold_hours": 120},
            {"condition": "take_profit_pct", "value": 6.0},
            {"condition": "stop_loss_pct", "value": 3.0},
            {"condition": "signal_invalidation",
             "expression": "funding_z_percentile_90d between 40,60"},
        ],
    }


class TestApplyOverridesToSpec:
    def test_low_extreme_uses_entry_low_pct(self):
        spec = apply_overrides_to_spec(
            _base_spec(), {"entry_low_pct": 15, "lookback_days": 60}
        )
        assert spec["entry_long"]["conditions"] == [
            "funding_z_percentile_60d <= 15 persist 2/3"
        ]

    def test_high_extreme_uses_entry_high_pct(self):
        spec = apply_overrides_to_spec(
            _base_spec(), {"entry_high_pct": 85, "lookback_days": 60}
        )
        assert spec["entry_short"]["conditions"] == [
            "funding_z_percentile_60d >= 85 persist 2/3"
        ]

    def test_persistence_overrides(self):
        spec = apply_overrides_to_spec(
            _base_spec(), {"persistence_min_hits": 3, "persistence_last_n": 5}
        )
        assert "persist 3/5" in spec["entry_long"]["conditions"][0]

    def test_exit_rule_overrides(self):
        spec = apply_overrides_to_spec(
            _base_spec(),
            {"hold_max_hours": 96, "tp_pct": 5.0, "sl_pct": 2.5, "lookback_days": 60},
        )
        rules = {r["condition"]: r for r in spec["exit_rules"]}
        assert rules["time_based"]["max_hold_hours"] == 96
        assert rules["take_profit_pct"]["value"] == 5.0
        assert rules["stop_loss_pct"]["value"] == 2.5
        assert rules["signal_invalidation"]["expression"] == \
            "funding_z_percentile_60d between 40,60"

    def test_does_not_mutate_base_spec(self):
        base = _base_spec()
        apply_overrides_to_spec(base, {"lookback_days": 30, "entry_low_pct": 5})
        # Originals intact (proves the deep copy)
        assert base["entry_long"]["conditions"] == [
            "funding_z_percentile_90d <= 20 persist 2/3"
        ]
        assert base["exit_rules"][0]["max_hold_hours"] == 120

    def test_unknown_keys_ignored(self):
        spec = apply_overrides_to_spec(_base_spec(), {"nonsense_key": 999})
        # Conditions are re-emitted unchanged when no relevant override is set
        assert spec["entry_long"]["conditions"] == [
            "funding_z_percentile_90d <= 20 persist 2/3"
        ]


# ---------------------------------------------------------------------------
# rank_combos
# ---------------------------------------------------------------------------


def _combo(idx: int, sharpe, trade_count, *, metrics: bool = True) -> ComboResult:
    m = None
    if metrics:
        m = {"sharpe": str(sharpe), "trade_count": str(trade_count)}
    return ComboResult(idx=idx, overrides={}, run_name=f"s_{idx:03d}", metrics=m)


class TestRankCombos:
    def test_gated_combos_sorted_by_sharpe_desc(self):
        combos = [
            _combo(0, 0.5, 20),
            _combo(1, 1.5, 5),    # below gate → excluded
            _combo(2, 0.8, 50),
            _combo(3, 0.0, 0, metrics=False),  # no metrics → excluded
        ]
        ranked = rank_combos(combos, min_trades=MIN_TRADE_COUNT_GATE)
        assert [c.idx for c in ranked] == [2, 0]

    def test_falls_back_to_all_valid_when_none_gated(self):
        combos = [
            _combo(0, 0.5, 3),
            _combo(1, 1.5, 5),
            _combo(2, 0.8, 2),
        ]
        ranked = rank_combos(combos, min_trades=MIN_TRADE_COUNT_GATE)
        # No combo passes the gate → fall back to all valid, sharpe desc
        assert [c.idx for c in ranked] == [1, 2, 0]

    def test_excludes_combos_without_metrics(self):
        combos = [_combo(0, 1.0, 50, metrics=False), _combo(1, 0.2, 50)]
        ranked = rank_combos(combos, min_trades=MIN_TRADE_COUNT_GATE)
        assert [c.idx for c in ranked] == [1]

    def test_empty_input_returns_empty(self):
        assert rank_combos([]) == []


# ---------------------------------------------------------------------------
# build_optimization_block
# ---------------------------------------------------------------------------


class TestBuildOptimizationBlock:
    def test_with_best_combo(self):
        best = ComboResult(
            idx=0,
            overrides={"lookback_days": 60, "tp_pct": 5.0, "bad": "x"},
            run_name="eth_s1_sweep_000",
            metrics={"sharpe": "1.2", "trade_count": "40"},
        )
        block = build_optimization_block(["b", "a"], best, "improved sharpe")
        assert block["source_run"] == "eth_s1_sweep_000"
        assert block["method"] == OPTIMIZATION_METHOD
        assert block["swept_params"] == ["a", "b"]  # sorted
        assert block["best_params"] == {"lookback_days": 60.0, "tp_pct": 5.0}  # non-numeric dropped
        assert block["improvement_summary"] == "improved sharpe"

    def test_with_no_best(self):
        block = build_optimization_block(["a"], None, "")
        assert block["source_run"] is None
        assert block["best_params"] == {}
        assert block["improvement_summary"] is None

    def test_validates_against_optimization_block_schema(self):
        from schemas import OptimizationBlock
        best = ComboResult(idx=1, overrides={"tp_pct": 4.0}, run_name="x_sweep_001",
                           metrics={"sharpe": "0.9", "trade_count": "30"})
        block = build_optimization_block(["tp_pct"], best, "ok")
        OptimizationBlock.model_validate(block)  # raises if invalid


# ---------------------------------------------------------------------------
# ComboResult properties
# ---------------------------------------------------------------------------


class TestComboResultProperties:
    def test_sharpe_and_trade_count_parse_strings(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"sharpe": "1.25", "trade_count": "42"})
        assert c.sharpe == 1.25
        assert c.trade_count == 42

    def test_none_metrics_gives_neg_inf_and_zero(self):
        c = ComboResult(idx=0, overrides={}, run_name="x", metrics=None)
        assert c.sharpe == float("-inf")
        assert c.trade_count == 0

    def test_bad_values_are_safe(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"sharpe": "n/a", "trade_count": "n/a"})
        assert c.sharpe == float("-inf")
        assert c.trade_count == 0

    def test_max_drawdown_parses_and_abs(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"sharpe": "1.0", "trade_count": "30",
                                 "max_drawdown": "-0.12"})
        assert c.max_drawdown == 0.12  # abs() applied

    def test_max_drawdown_missing_field_is_inf(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"sharpe": "1.0", "trade_count": "30"})
        assert c.max_drawdown == float("inf")

    def test_max_drawdown_none_metrics_is_inf(self):
        c = ComboResult(idx=0, overrides={}, run_name="x", metrics=None)
        assert c.max_drawdown == float("inf")

    def test_max_drawdown_unparseable_is_inf(self):
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"max_drawdown": "n/a"})
        assert c.max_drawdown == float("inf")

    def test_max_drawdown_nan_string_is_inf(self):
        # float("NaN") does not raise; abs(nan) is nan. Must be normalised to inf.
        c = ComboResult(idx=0, overrides={}, run_name="x",
                        metrics={"max_drawdown": "NaN"})
        assert c.max_drawdown == float("inf")


# ---------------------------------------------------------------------------
# size_mult in apply_overrides_to_spec + scaffold
# ---------------------------------------------------------------------------


def _full_spec() -> dict:
    """A spec dict that satisfies StrategySpec.model_validate (has all required fields)."""
    return {
        "name": "test_full",
        "archetype": "single_factor",
        "symbol": "BTC",
        "timeframe_signal": "1H",
        "indicators": {
            "funding_z": {"source": "stage1:funding_z", "smoothing": "none"},
        },
        "entry_long": {
            "description": "Long when funding rate is low",
            "logic": "all",
            "conditions": ["funding_z_percentile_90d <= 20 persist 2/3"],
        },
        "entry_short": {
            "description": "Short when funding rate is high",
            "logic": "all",
            "conditions": ["funding_z_percentile_90d >= 80 persist 2/3"],
        },
        "exit_rules": [
            {"condition": "time_based", "max_hold_hours": 120},
            {"condition": "take_profit_pct", "value": 6.0},
            {"condition": "stop_loss_pct", "value": 3.0},
            {"condition": "signal_invalidation",
             "expression": "funding_z_percentile_90d between 40,60"},
        ],
    }


class TestSizeMult:
    def test_apply_overrides_size_mult_0_6(self):
        """size_mult override sets top-level size_mult on the returned spec dict."""
        spec_dict = apply_overrides_to_spec(_base_spec(), {"size_mult": 0.6})
        assert spec_dict["size_mult"] == 0.6
        # Bonus: validate through StrategySpec using a complete spec
        full_result = apply_overrides_to_spec(_full_spec(), {"size_mult": 0.6})
        validated = StrategySpec.model_validate(full_result)
        assert validated.size_mult == 0.6

    def test_apply_overrides_size_mult_recompile_signal(self):
        """Compiled signal source contains the size_mult multiplier expression."""
        spec_dict = apply_overrides_to_spec(_full_spec(), {"size_mult": 0.6})
        compiled = compile_strategy(StrategySpec.model_validate(spec_dict))
        assert "float(position) * 0.6" in compiled

    def test_apply_overrides_no_size_mult_unchanged(self):
        """When size_mult is absent from overrides, spec size_mult is not set (defaults to 1.0)."""
        spec_dict = apply_overrides_to_spec(_base_spec(), {})
        # _base_spec() has no size_mult → should not be introduced by the override function
        assert spec_dict.get("size_mult", 1.0) == 1.0

    def test_scaffold_has_size_mult_range(self):
        """_default_spec_scaffold includes size_mult in parameter_search_ranges."""
        scaffold = _default_spec_scaffold(["some_factor"])
        assert "size_mult" in scaffold["parameter_search_ranges"]
        assert scaffold["parameter_search_ranges"]["size_mult"] == [0.4, 1.0, 0.2]
