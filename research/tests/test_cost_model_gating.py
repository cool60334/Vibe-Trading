"""
research/tests/test_cost_model_gating.py
─────────────────────────────────────────
Task 3 (A1 cost-model fix): pipeline-side wiring of the realistic cost model
added to the backtest engine in Tasks 1-2 (taker_both_legs, funding_series_path).

Covers:
  - Default (legacy_costs unset/False) build_run_config wires config fees,
    taker_both_legs=True, funding_series_path, and tags cost_model_version
    "v2_realistic".
  - legacy_costs=True suppresses all realistic-cost keys and tags
    cost_model_version "v1_legacy" so the engine falls back to its hardcoded
    legacy defaults (byte-for-byte reproduction of old runs).

Pytest is run from repo root as:
    python -m pytest research/tests/test_cost_model_gating.py -v
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from research.pipeline.config import load_config
from research.pipeline.stage3_backtest import build_run_config
from datetime import date

_TODAY = date(2024, 12, 31)


def test_realistic_default_wires_config_fees_and_funding():
    cfg = load_config()  # legacy_costs unset -> realistic
    rc = build_run_config("BTC-USDT-SWAP", cfg, today=_TODAY)
    assert rc["taker_rate"] == cfg.fees.taker_rate       # config fees wired
    assert rc["maker_rate"] == cfg.fees.maker_rate
    assert rc["taker_both_legs"] is True
    assert rc["funding_series_path"].endswith("features_btc.parquet")
    assert rc["cost_model_version"] == "v2_realistic"


def test_legacy_costs_suppresses_realistic_keys(monkeypatch):
    cfg = load_config()
    object.__setattr__(cfg, "legacy_costs", True)
    rc = build_run_config("BTC-USDT-SWAP", cfg, today=_TODAY)
    assert "taker_both_legs" not in rc                    # engine keeps hardcoded default
    assert "funding_series_path" not in rc
    assert "taker_rate" not in rc                          # no config-fee injection
    assert rc["cost_model_version"] == "v1_legacy"


from research.pipeline.stage5_select import assert_uniform_cost_model


def test_guard_passes_on_uniform_version():
    assert_uniform_cost_model([
        {"id": "a", "cost_model_version": "v2_realistic"},
        {"id": "b", "cost_model_version": "v2_realistic"},
    ])  # no raise


def test_guard_fails_on_mixed_version():
    import pytest
    with pytest.raises(ValueError, match="mixed cost_model_version"):
        assert_uniform_cost_model([
            {"id": "a", "cost_model_version": "v2_realistic"},
            {"id": "b", "cost_model_version": "v1_legacy"},
        ])
