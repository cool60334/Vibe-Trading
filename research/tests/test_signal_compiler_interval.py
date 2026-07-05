"""Interval-awareness of the signal compiler.

The DSL says `percentile_90d` = 90 DAYS; the compiler used to hard-code
90*24 BARS, silently shrinking every window at sub-hour intervals. At 1H
the output must stay byte-identical to the legacy renderer.
"""
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
_REPO = _RESEARCH.parent
for p in (str(_RESEARCH), str(_REPO / "dashboard" / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas import StrategySpec  # noqa: E402
from lib.signal_compiler import compile_strategy  # noqa: E402


def _make_spec() -> StrategySpec:
    return StrategySpec.model_validate({
        "name": "iv_test",
        "archetype": "single_factor",
        "hypothesis": "interval test",
        "symbol": "ETH-USDT-SWAP",
        "timeframe_signal": "8h",
        "hold_period": {"min_hours": 24, "max_hours": 168},
        "indicators": {
            "funding_z": {"source": "stage1:funding_z", "smoothing": "sma_3"},
        },
        "entry_long": {
            "description": "d", "logic": "all",
            "conditions": ["funding_z_percentile_90d <= 15 persist 2/3"],
        },
        "entry_short": {
            "description": "d", "logic": "all",
            "conditions": ["funding_z_percentile_90d >= 80 persist 2/3"],
        },
        "exit_rules": [
            {"condition": "time_based", "max_hold_hours": 168},
            {"condition": "signal_invalidation",
             "expression": "funding_z_percentile_90d between 40,60"},
        ],
        "position_sizing": {"method": "fixed_risk",
                            "risk_per_trade_pct": 1.5, "leverage": 1.0},
        "parameter_search_ranges": {},
    })


def test_default_interval_matches_explicit_1h_byte_identical():
    spec = _make_spec()
    assert compile_strategy(spec) == compile_strategy(spec, interval="1H")


def test_1h_renders_legacy_24_bars_per_day():
    src = compile_strategy(_make_spec(), interval="1H")
    assert "rolling(90*24, min_periods=90*24//2)" in src
    assert "bars_held >= 168" in src


def test_30m_scales_windows_and_hold():
    src = compile_strategy(_make_spec(), interval="30m")
    assert "rolling(90*48, min_periods=90*48//2)" in src   # 48 bars/day
    assert "90*24" not in src
    assert "bars_held >= 336" in src                        # 168h * 2 bars/h


def test_15m_scales_windows_and_hold():
    src = compile_strategy(_make_spec(), interval="15m")
    assert "rolling(90*96, min_periods=90*96//2)" in src
    assert "bars_held >= 672" in src


def test_invalidation_hoist_scales_too():
    src = compile_strategy(_make_spec(), interval="30m")
    assert "_inv_pct_1 = funding_z.rolling(90*48" in src or "rolling(90*48" in src


def test_stage4_resolves_manifests_via_active_dir(monkeypatch):
    from pipeline.stage4_optimize import _resolve_manifests_dir

    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    assert _resolve_manifests_dir().name == "manifests"

    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    assert _resolve_manifests_dir().name == "30m"


def test_stage4_cpcv_uses_same_resolver(monkeypatch):
    import pipeline.stage4_cpcv as cpcv
    from pipeline.stage4_optimize import _resolve_manifests_dir

    assert cpcv._resolve_manifests_dir is _resolve_manifests_dir
