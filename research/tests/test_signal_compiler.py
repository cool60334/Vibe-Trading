"""Unit tests for research/lib/signal_compiler.py (Task 3).

Coverage:
  (a) compile_strategy(valid_spec) → ast.parse() doesn't raise
  (b) _validate_source_string passes (no exception)
  (c) rendered source contains "class SignalEngine" and "def generate"
  (d) "funding_zscore_30d <= -1.5 persist 2/3" → ".rolling(3).sum() >= 2"
  (e) two conditions in entry_long joined with "&"
  (f) each ExitRule type translates correctly
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirror what signal_compiler.py does so tests can run from any cwd
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_RESEARCH_DIR = _HERE.parents[1]          # research/
_REPO_ROOT = _RESEARCH_DIR.parent         # repo root
_AGENT_DIR = _REPO_ROOT / "agent"

for _p in (str(_RESEARCH_DIR), str(_REPO_ROOT), str(_AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dashboard.server.schemas import (
    EntryBlock,
    IndicatorSpec,
    StrategySpec,
    _ExitTimeBased,
    _ExitTakeProfit,
    _ExitStopLoss,
    _ExitSignalInvalidation,
)
from lib.signal_compiler import (
    _render_condition,
    _render_entry_block,
    _render_exit_state_machine,
    _render_indicator_load,
    _validate_source_string,
    compile_strategy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_spec(
    entry_long_conds=None,
    entry_short_conds=None,
    exit_rules=None,
    smoothing="none",
    entry_logic="all",
):
    """Build a minimal valid StrategySpec."""
    indicators = {
        "funding_rate": IndicatorSpec(source="stage1:funding_rate", smoothing=smoothing),
        "basis": IndicatorSpec(source="stage1:basis", smoothing="none"),
    }

    entry_long = None
    if entry_long_conds is not None:
        entry_long = EntryBlock(
            description="Long entry",
            conditions=entry_long_conds,
            logic=entry_logic,
        )

    entry_short = None
    if entry_short_conds is not None:
        entry_short = EntryBlock(
            description="Short entry",
            conditions=entry_short_conds,
            logic=entry_logic,
        )

    if exit_rules is None:
        exit_rules = [_ExitTimeBased(condition="time_based", max_hold_hours=48)]

    return StrategySpec(
        name="test_strategy",
        archetype="contrarian",
        symbol="ETH-USDT-SWAP",
        timeframe_signal="1H",
        indicators=indicators,
        entry_long=entry_long,
        entry_short=entry_short,
        exit_rules=exit_rules,
    )


@pytest.fixture
def basic_spec():
    return _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )


# ---------------------------------------------------------------------------
# (a) compile_strategy — ast.parse succeeds
# ---------------------------------------------------------------------------

def test_compile_strategy_parses_cleanly(basic_spec):
    """Compiled source must be valid Python (ast.parse doesn't raise)."""
    source = compile_strategy(basic_spec, yaml_hash="deadbeef")
    tree = ast.parse(source)  # should not raise
    assert tree is not None


# ---------------------------------------------------------------------------
# (b) _validate_source_string passes
# ---------------------------------------------------------------------------

def test_validate_source_string_passes(basic_spec):
    """Compiled source must pass the backtest AST scrubber."""
    source = compile_strategy(basic_spec, yaml_hash="abc123")
    # Should not raise
    _validate_source_string(source, label="test")


# ---------------------------------------------------------------------------
# (c) rendered source structure
# ---------------------------------------------------------------------------

def test_rendered_source_contains_class_and_method(basic_spec):
    source = compile_strategy(basic_spec)
    assert "class SignalEngine" in source
    assert "def generate" in source


def test_rendered_source_contains_yaml_hash():
    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
    )
    source = compile_strategy(spec, yaml_hash="myhash123")
    assert "# yaml-hash: myhash123" in source


# ---------------------------------------------------------------------------
# (d) zscore condition with persist suffix
# ---------------------------------------------------------------------------

def test_zscore_persist_condition():
    """'funding_zscore_30d <= -1.5 persist 2/3' must produce .rolling(3).sum() >= 2"""
    cond = "funding_zscore_30d <= -1.5 persist 2/3"
    rendered = _render_condition(cond)
    assert ".rolling(3).sum() >= 2" in rendered
    assert "rolling(30*24" in rendered
    assert "-1.5" in rendered


def test_zscore_persist_in_compiled_output():
    # Convention: indicator key "funding_rate" → condition "funding_rate_zscore_30d"
    spec = _make_spec(
        entry_long_conds=["funding_rate_zscore_30d <= -1.5 persist 2/3"],
    )
    source = compile_strategy(spec)
    assert ".rolling(3).sum() >= 2" in source


# ---------------------------------------------------------------------------
# (e) two conditions in entry_long joined with "&"
# ---------------------------------------------------------------------------

def test_two_conditions_joined_with_and():
    spec = _make_spec(
        entry_long_conds=[
            "funding_rate_percentile_90d <= 20.0",
            "basis_percentile_90d <= 20.0",
        ],
    )
    source = compile_strategy(spec)
    # Find the entry_long line
    entry_line = next(
        line for line in source.splitlines() if "entry_long" in line and "=" in line
    )
    assert " & " in entry_line


def test_two_conditions_joined_with_or_when_logic_any():
    """logic='any' joins conditions with | instead of &."""
    spec = _make_spec(
        entry_long_conds=[
            "funding_rate_percentile_90d <= 20.0",
            "basis_percentile_90d <= 20.0",
        ],
        entry_short_conds=[
            "funding_rate_percentile_90d >= 80.0",
            "basis_percentile_90d >= 80.0",
        ],
        entry_logic="any",
    )
    source = compile_strategy(spec)
    long_line = next(
        line for line in source.splitlines() if "entry_long" in line and "=" in line
    )
    short_line = next(
        line for line in source.splitlines() if "entry_short" in line and "=" in line
    )
    assert " | " in long_line
    assert " & " not in long_line
    assert " | " in short_line


def test_invalid_logic_rejected_by_schema():
    """Pydantic Literal must reject unknown logic values."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        EntryBlock(
            description="bad",
            conditions=["funding_rate_percentile_90d <= 20.0"],
            logic="majority",
        )


def test_default_logic_is_all():
    """When logic is omitted, the entry block must default to AND semantics."""
    block = EntryBlock(
        description="default",
        conditions=["funding_rate_percentile_90d <= 20.0"],
    )
    assert block.logic == "all"


# ---------------------------------------------------------------------------
# (f) ExitRule types translate correctly
# ---------------------------------------------------------------------------

def test_exit_time_based():
    rules = [_ExitTimeBased(condition="time_based", max_hold_hours=72)]
    code = _render_exit_state_machine(rules)
    assert "bars_held >= 72" in code


def test_exit_take_profit():
    rules = [_ExitTakeProfit(condition="take_profit_pct", value=5.0)]
    code = _render_exit_state_machine(rules)
    assert "pnl_pct >= 5.0 / 100" in code


def test_exit_stop_loss():
    rules = [_ExitStopLoss(condition="stop_loss_pct", value=3.0)]
    code = _render_exit_state_machine(rules)
    assert "pnl_pct <= -3.0 / 100" in code


def test_exit_signal_invalidation():
    rules = [
        _ExitSignalInvalidation(
            condition="signal_invalidation",
            expression="funding_rate_percentile_90d between 40,60",
        )
    ]
    code = _render_exit_state_machine(rules)
    assert "funding_rate" in code
    # The pre-computed series is referenced as _inv_pct_0.iloc[bar_i]
    assert "_inv_pct_0" in code
    assert "40 <=" in code
    assert "60" in code


def test_all_exit_rules_in_compiled_output():
    """All four exit rule types survive the full compile pipeline."""
    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        exit_rules=[
            _ExitTimeBased(condition="time_based", max_hold_hours=24),
            _ExitTakeProfit(condition="take_profit_pct", value=10.0),
            _ExitStopLoss(condition="stop_loss_pct", value=5.0),
            _ExitSignalInvalidation(
                condition="signal_invalidation",
                expression="funding_rate_percentile_90d between 40,60",
            ),
        ],
    )
    source = compile_strategy(spec)
    assert "bars_held >= 24" in source
    assert "pnl_pct >= 10.0 / 100" in source
    assert "pnl_pct <= -5.0 / 100" in source
    assert "_inv_pct" in source


# ---------------------------------------------------------------------------
# Indicator rendering tests
# ---------------------------------------------------------------------------

def test_indicator_load_no_smoothing():
    spec = IndicatorSpec(source="stage1:funding_rate", smoothing="none")
    code = _render_indicator_load("funding_rate", spec)
    assert 'funding_rate = _factors["funding_rate"]' in code
    # One newline: reindex line appended after assignment (no smoothing line)
    assert code.count("\n") == 1


def test_indicator_load_sma():
    spec = IndicatorSpec(source="stage1:funding_rate", smoothing="sma_3")
    code = _render_indicator_load("funding_rate", spec)
    assert ".rolling(3, min_periods=1).mean()" in code


def test_indicator_load_ema():
    spec = IndicatorSpec(source="stage1:basis", smoothing="ema_12")
    code = _render_indicator_load("basis", spec)
    assert ".ewm(span=12, adjust=False).mean()" in code


# ---------------------------------------------------------------------------
# Entry block edge cases
# ---------------------------------------------------------------------------

def test_entry_block_none_produces_false_series():
    code = _render_entry_block("long", None)
    assert "pd.Series(False, index=ohlcv.index)" in code
    assert "entry_long" in code


def test_percentile_condition_rendering():
    cond = "funding_rate_percentile_90d <= 20.0"
    rendered = _render_condition(cond)
    assert "rolling(90*24" in rendered
    assert "rank(pct=True)*100" in rendered
    assert "<= 20.0" in rendered


def test_raw_condition_rendering():
    cond = "funding_rate <= 0.01"
    rendered = _render_condition(cond)
    assert "(funding_rate <= 0.01)" in rendered


# ---------------------------------------------------------------------------
# StrategySpec new fields: regime_filter and size_mult
# ---------------------------------------------------------------------------

def test_strategy_spec_defaults():
    """Minimal YAML dict without regime_filter/size_mult uses default values."""
    from pydantic import ValidationError

    data = {
        "name": "test_strategy",
        "archetype": "contrarian",
        "symbol": "ETH-USDT-SWAP",
        "timeframe_signal": "1H",
        "indicators": {
            "funding_rate": {"source": "stage1:funding_rate", "smoothing": "none"},
        },
        "exit_rules": [{"condition": "time_based", "max_hold_hours": 48}],
    }
    spec = StrategySpec.model_validate(data)
    assert spec.regime_filter is False
    assert spec.size_mult == 1.0


def test_size_mult_out_of_range():
    """size_mult values outside (0, 1] must raise ValidationError."""
    from pydantic import ValidationError

    base = {
        "name": "test_strategy",
        "archetype": "contrarian",
        "symbol": "ETH-USDT-SWAP",
        "timeframe_signal": "1H",
        "indicators": {
            "funding_rate": {"source": "stage1:funding_rate", "smoothing": "none"},
        },
        "exit_rules": [{"condition": "time_based", "max_hold_hours": 48}],
    }

    for bad_value in (1.5, 0, -0.1):
        data = {**base, "size_mult": bad_value}
        with pytest.raises(ValidationError, match=r"size_mult|greater_than|less_than"):
            StrategySpec.model_validate(data)


def test_size_mult_valid_boundary():
    """size_mult=0.01 and size_mult=1.0 must both pass validation."""
    base = {
        "name": "test_strategy",
        "archetype": "contrarian",
        "symbol": "ETH-USDT-SWAP",
        "timeframe_signal": "1H",
        "indicators": {
            "funding_rate": {"source": "stage1:funding_rate", "smoothing": "none"},
        },
        "exit_rules": [{"condition": "time_based", "max_hold_hours": 48}],
    }

    spec_low = StrategySpec.model_validate({**base, "size_mult": 0.01})
    assert spec_low.size_mult == 0.01

    spec_high = StrategySpec.model_validate({**base, "size_mult": 1.0})
    assert spec_high.size_mult == 1.0


# ---------------------------------------------------------------------------
# size_mult compiler tests (Task 2)
# ---------------------------------------------------------------------------

def test_size_mult_045_in_compiled_code():
    """Compiled source for size_mult=0.45 must contain float(position) * 0.45."""
    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    spec = spec.model_copy(update={"size_mult": 0.45})
    source = compile_strategy(spec)
    assert "float(position) * 0.45" in source


def test_size_mult_045_engine_signal(tmp_path):
    """Exec compiled code with size_mult=0.45; signal at an entry bar equals 0.45.

    The generated engine uses __file__ and load_factor_values(symbol); we write
    the source to a temp file and monkey-patch load_factor_values so the test is
    self-contained.
    """
    import importlib.util
    import pandas as pd
    import numpy as np
    from unittest.mock import patch

    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    spec = spec.model_copy(update={"size_mult": 0.45})
    source = compile_strategy(spec)

    # Write generated source to a temp file so __file__ is defined
    engine_file = tmp_path / "signal_engine.py"
    engine_file.write_text(source, encoding="utf-8")

    # Build synthetic OHLCV with enough hourly bars for the 90d rolling rank.
    # min_periods = 90*24//2 = 1080, so we need at least ~2500 bars to get any
    # non-NaN percentile values. Use 3000 bars to be safe.
    N = 3000
    idx = pd.date_range("2024-01-01", periods=N, freq="1h")
    close = pd.Series(np.linspace(100, 110, N), index=idx)
    ohlcv = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )

    # Factor data: funding_rate cycles so that some bars hit <= 20th percentile.
    # Use a sine wave: values at the trough will be ranked in the bottom 20%.
    funding_rate = pd.Series(
        np.sin(np.linspace(0, 20 * np.pi, N)),
        index=idx,
        name="funding_rate",
    )
    basis = pd.Series(0.0, index=idx, name="basis")
    factors_df = pd.DataFrame({"funding_rate": funding_rate, "basis": basis}, index=idx)

    # Load the written module
    spec_mod = importlib.util.spec_from_file_location("signal_engine_test", engine_file)
    module = importlib.util.module_from_spec(spec_mod)

    symbol = spec.symbol  # e.g. "ETH-USDT-SWAP"
    data_map = {symbol: ohlcv}

    with patch("lib.factor_io.load_factor_values", return_value=factors_df):
        spec_mod.loader.exec_module(module)
        engine = module.SignalEngine()
        result = engine.generate(data_map)

    signal = result[symbol]

    # Find bars where signal is non-zero (long or short)
    nonzero = signal[signal != 0.0]
    assert len(nonzero) > 0, "Expected at least one non-zero signal bar"
    # All non-zero signal values must be ±0.45
    for val in nonzero:
        assert abs(abs(val) - 0.45) < 1e-9, f"Expected ±0.45, got {val}"


def test_size_mult_default_no_change():
    """Default size_mult=1.0 must produce exactly 'signal.iloc[bar_i] = float(position)' with no '* 1.0'."""
    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    # Default size_mult is 1.0
    assert spec.size_mult == 1.0
    source = compile_strategy(spec)
    assert "signal.iloc[bar_i] = float(position)" in source
    assert "* 1.0" not in source


# ---------------------------------------------------------------------------
# regime_filter compiler tests (Task 3)
# ---------------------------------------------------------------------------

def test_regime_filter_false_no_regime_code():
    """Compile with regime_filter=False (default): output must contain no regime code."""
    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    assert spec.regime_filter is False
    source = compile_strategy(spec)
    assert "_load_regime_series" not in source
    assert "regime" not in source


def test_regime_filter_true_has_regime_code():
    """Compile with regime_filter=True: output must contain all required regime code."""
    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    spec = spec.model_copy(update={"regime_filter": True})
    source = compile_strategy(spec)
    assert "import json" in source
    assert "def _load_regime_series(" in source
    assert 'entry_long = entry_long & (regime != "bear")' in source
    assert 'entry_short = entry_short & (regime != "bull")' in source
    # The path pattern for the json file must reference symbol_short
    assert "regime_" in source and ".json" in source


def test_regime_mask_blocks_bear_long(tmp_path):
    """On a bear bar, entry_long is blocked; entry_short is NOT blocked."""
    import importlib.util
    import json
    import numpy as np
    import pandas as pd
    from unittest.mock import patch

    spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    spec = spec.model_copy(update={"regime_filter": True})
    source = compile_strategy(spec)

    # Write compiled source deep enough that here.parents[3] resolves to tmp_path root
    engine_dir = tmp_path / "a" / "b" / "c" / "d"
    engine_dir.mkdir(parents=True)
    engine_file = engine_dir / "signal_engine.py"
    engine_file.write_text(source, encoding="utf-8")

    # Create regime json: one bear date covering the middle of our data
    manifests_dir = tmp_path / "research" / "manifests"
    manifests_dir.mkdir(parents=True)
    regime_json = manifests_dir / "regime_eth.json"
    # "bear" for 2024-02-01, "bull" for 2024-01-01 (beginning of data)
    regime_json.write_text(
        json.dumps({
            "breakdown": [
                {"date": "2024-01-01", "regime": "bull"},
                {"date": "2024-02-01", "regime": "bear"},
            ]
        }),
        encoding="utf-8",
    )

    # Build synthetic OHLCV
    N = 3000
    idx = pd.date_range("2024-01-01", periods=N, freq="1h")
    close = pd.Series(np.linspace(100, 110, N), index=idx)
    ohlcv = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": 1.0},
        index=idx,
    )

    # Factor data: funding_rate at bottom 20th percentile for first half → entry_long fires
    # and at top 80th percentile for second half → entry_short fires
    funding_rate = pd.Series(np.sin(np.linspace(0, 20 * np.pi, N)), index=idx, name="funding_rate")
    basis = pd.Series(0.0, index=idx, name="basis")
    factors_df = pd.DataFrame({"funding_rate": funding_rate, "basis": basis}, index=idx)

    symbol = spec.symbol
    data_map = {symbol: ohlcv}

    spec_mod = importlib.util.spec_from_file_location("signal_engine_regime_test", engine_file)
    module = importlib.util.module_from_spec(spec_mod)

    # Monkeypatch _load_regime_series to return all-"bear" so ALL long entries are blocked
    def _all_bear(symbol_short, target_index):
        return pd.Series("bear", index=target_index)

    with patch("lib.factor_io.load_factor_values", return_value=factors_df):
        spec_mod.loader.exec_module(module)
        # Override _load_regime_series AFTER module is loaded but INSIDE the patch context
        module._load_regime_series = _all_bear
        engine = module.SignalEngine()
        result = engine.generate(data_map)

    signal = result[symbol]

    # With all-bear regime: no long positions allowed (signal never == +1.0)
    assert (signal > 0).sum() == 0, "Expected no long positions in all-bear regime"
    # Short positions are still allowed in bear regime
    # (entry_short fires at top 80th percentile; some bars will qualify)
    # We don't assert shorts must fire (depends on data), but signal must not error


def test_regime_missing_json_fail_soft(tmp_path):
    """When regime json file is missing, the engine must not raise and produce same
    signals as regime_filter=False (neutral regime = no masking)."""
    import importlib.util
    import numpy as np
    import pandas as pd
    from unittest.mock import patch

    base_spec = _make_spec(
        entry_long_conds=["funding_rate_percentile_90d <= 20.0"],
        entry_short_conds=["funding_rate_percentile_90d >= 80.0"],
        exit_rules=[_ExitTimeBased(condition="time_based", max_hold_hours=48)],
    )
    spec_with_regime = base_spec.model_copy(update={"regime_filter": True})

    source_no_regime = compile_strategy(base_spec)
    source_with_regime = compile_strategy(spec_with_regime)

    # Build synthetic data
    N = 3000
    idx = pd.date_range("2024-01-01", periods=N, freq="1h")
    close = pd.Series(np.linspace(100, 110, N), index=idx)
    ohlcv = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": 1.0},
        index=idx,
    )
    funding_rate = pd.Series(np.sin(np.linspace(0, 20 * np.pi, N)), index=idx, name="funding_rate")
    basis = pd.Series(0.0, index=idx, name="basis")
    factors_df = pd.DataFrame({"funding_rate": funding_rate, "basis": basis}, index=idx)

    symbol = base_spec.symbol
    data_map = {symbol: ohlcv}

    def _run_engine(source: str, engine_path: Path) -> "pd.Series":
        engine_path.write_text(source, encoding="utf-8")
        mod_spec = importlib.util.spec_from_file_location("signal_engine_tmp", engine_path)
        mod = importlib.util.module_from_spec(mod_spec)
        with patch("lib.factor_io.load_factor_values", return_value=factors_df):
            mod_spec.loader.exec_module(mod)
            eng = mod.SignalEngine()
            return eng.generate(data_map)[symbol]

    no_regime_file = tmp_path / "no_regime_engine.py"
    with_regime_file = tmp_path / "with_regime_engine.py"

    signal_no_regime = _run_engine(source_no_regime, no_regime_file)

    # For the regime engine: _load_regime_series will look for regime_eth.json
    # relative to __file__ (parents[3]). Since tmp_path has no research/manifests/
    # directory, the file won't exist → fail-soft → all-neutral → same as no-regime.
    signal_with_regime = _run_engine(source_with_regime, with_regime_file)

    pd.testing.assert_series_equal(
        signal_no_regime,
        signal_with_regime,
        check_names=False,
        obj="regime_filter missing-json fail-soft",
    )
