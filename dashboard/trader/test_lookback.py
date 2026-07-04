"""Tests for trader.lookback — derive required OHLCV bars from a strategy's YAML.

Root cause guarded here: live trader fed 200 bars to engines whose
``percentile_90d`` transforms need 2160 bars → all-NaN → signal stuck at 0.
The trader must derive the requirement from the strategy spec itself.
"""

import json

from trader.lookback import (
    LOOKBACK_BUFFER_BARS,
    required_bars_from_yaml_text,
    required_bars_for_strategy,
)


# ── required_bars_from_yaml_text ──────────────────────────────────────────────

_ETH_S5_LIKE_YAML = """\
name: eth_s5_half_size
entry_long:
  conditions:
  - stablecoin_supply_z_percentile_90d >= 70 persist 2/3
  - funding_z_percentile_90d <= 15 persist 2/3
exit_rules:
- condition: signal_invalidation
  expression: stablecoin_supply_z_percentile_90d between 40,60
"""


def test_percentile_90d_requires_full_window_plus_buffer():
    assert required_bars_from_yaml_text(_ETH_S5_LIKE_YAML) == 90 * 24 + LOOKBACK_BUFFER_BARS


def test_mixed_zscore_and_percentile_uses_max_days():
    text = (
        "conditions:\n"
        "- funding_z_zscore_120d <= -1.5\n"
        "- oi_z_percentile_30d >= 80\n"
    )
    assert required_bars_from_yaml_text(text) == 120 * 24 + LOOKBACK_BUFFER_BARS


def test_yaml_without_rolling_dsl_returns_none():
    assert required_bars_from_yaml_text("name: x\nsymbol: BTC-USDT-SWAP\n") is None


def test_empty_text_returns_none():
    assert required_bars_from_yaml_text("") is None


# ── required_bars_for_strategy ────────────────────────────────────────────────


def _make_repo(tmp_path, strategy_id="eth_s5_half_size", yaml_text=_ETH_S5_LIKE_YAML,
               spec_yaml="research/strategies/strategy_eth_s5_half_size.yaml",
               write_yaml=True):
    research = tmp_path / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "strategy_runs.json").write_text(
        json.dumps({strategy_id: {"symbol": "ETH-USDT-SWAP", "spec_yaml": spec_yaml}}),
        encoding="utf-8",
    )
    if write_yaml:
        yaml_path = tmp_path / spec_yaml
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(yaml_text, encoding="utf-8")
    return tmp_path


def test_for_strategy_reads_spec_yaml_via_strategy_runs(tmp_path):
    repo = _make_repo(tmp_path)
    assert required_bars_for_strategy(repo, "eth_s5_half_size") == 90 * 24 + LOOKBACK_BUFFER_BARS


def test_for_strategy_unknown_strategy_returns_none(tmp_path):
    repo = _make_repo(tmp_path)
    assert required_bars_for_strategy(repo, "nope") is None


def test_for_strategy_missing_yaml_returns_none(tmp_path):
    repo = _make_repo(tmp_path, write_yaml=False)
    assert required_bars_for_strategy(repo, "eth_s5_half_size") is None


def test_for_strategy_missing_strategy_runs_returns_none(tmp_path):
    assert required_bars_for_strategy(tmp_path, "eth_s5_half_size") is None


def test_for_strategy_yaml_without_dsl_returns_none(tmp_path):
    repo = _make_repo(tmp_path, yaml_text="name: x\n")
    assert required_bars_for_strategy(repo, "eth_s5_half_size") is None
