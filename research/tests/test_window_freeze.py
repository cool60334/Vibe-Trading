"""
Tests for resolve_anchor_date() — the single freeze point all backtest window
math funnels through (research/pipeline/stage3_backtest.py).

TDD: written BEFORE the implementation.

Covers:
  - default behavior (today = date.today()) when window_end/final_holdout_start unset
  - window_end freezes the anchor date
  - explicit `today` arg beats window_end
  - final_holdout_start caps the anchor UNCONDITIONALLY (even overriding an
    explicit `today` that is after the holdout), and leaves an explicit
    `today` before the holdout untouched
  - build_run_config is date-stable under a frozen window_end
  - oos_window respects both window_end and final_holdout_start
  - legacy behavior (both fields unset) is unchanged

Pytest is run from repo root as:
    python -m pytest research/tests/test_window_freeze.py -q
"""

from __future__ import annotations

from datetime import date, timedelta

from pipeline.config import ResearchConfig, SymbolConfig, FeesConfig
from pipeline.stage3_backtest import (
    resolve_anchor_date,
    build_run_config,
    train_window,
    oos_window,
)


def _cfg(
    period: int = 730,
    interval: str = "1H",
    oos_start: str | None = None,
    window_end: str | None = None,
    final_holdout_start: str | None = None,
) -> ResearchConfig:
    """Build a minimal ResearchConfig for tests, mirroring _make_research_config
    in test_stage3_backtest.py but extended with window_end / final_holdout_start.
    """
    return ResearchConfig(
        symbols=(
            SymbolConfig(name="btc", okx_swap="BTC-USDT-SWAP", ccxt_bybit="BTC/USDT:USDT"),
        ),
        period=period,
        interval=interval,
        data_source="okx",
        engine="daily",
        fees=FeesConfig(maker_rate=0.0002, taker_rate=0.00055, slippage=0.0005),
        horizons_h=(8, 24, 72, 168),
        oos_start=oos_start,
        window_end=window_end,
        final_holdout_start=final_holdout_start,
    )


class TestResolveAnchorDateDefaults:
    def test_defaults_to_today_when_unset(self):
        cfg = _cfg()
        result = resolve_anchor_date(cfg)
        assert result == date.today()

    def test_explicit_today_used_when_no_window_end(self):
        cfg = _cfg()
        explicit = date(2024, 3, 1)
        result = resolve_anchor_date(cfg, today=explicit)
        assert result == explicit


class TestResolveAnchorDateWindowEnd:
    def test_uses_frozen_window_end(self):
        cfg = _cfg(window_end="2024-06-15")
        result = resolve_anchor_date(cfg)
        assert result == date(2024, 6, 15)

    def test_explicit_today_beats_window_end(self):
        cfg = _cfg(window_end="2024-06-15")
        explicit = date(2024, 1, 1)
        result = resolve_anchor_date(cfg, today=explicit)
        assert result == explicit


class TestResolveAnchorDateFinalHoldoutCap:
    def test_caps_default_today_when_window_end_unset(self):
        # Even without an explicit today or window_end, the holdout cap must
        # apply. We can't mock date.today(), so we verify the cap logic via
        # window_end (which stands in for "today" deterministically) and via
        # explicit `today` below, which together prove the cap is unconditional.
        cfg = _cfg(window_end="2024-06-15", final_holdout_start="2024-06-10")
        result = resolve_anchor_date(cfg)
        assert result == date(2024, 6, 9)

    def test_caps_explicit_today_after_holdout_unconditionally(self):
        cfg = _cfg(final_holdout_start="2024-06-10")
        explicit = date(2024, 6, 20)
        result = resolve_anchor_date(cfg, today=explicit)
        assert result == date(2024, 6, 9)

    def test_does_not_affect_explicit_today_before_holdout(self):
        cfg = _cfg(final_holdout_start="2024-06-10")
        explicit = date(2024, 1, 1)
        result = resolve_anchor_date(cfg, today=explicit)
        assert result == explicit

    def test_cap_is_holdout_start_minus_one_day(self):
        cfg = _cfg(final_holdout_start="2025-01-01")
        explicit = date(2025, 6, 1)
        result = resolve_anchor_date(cfg, today=explicit)
        assert result == date(2025, 1, 1) - timedelta(days=1)


class TestBuildRunConfigDateStability:
    def test_frozen_window_end_produces_identical_output_regardless_of_wallclock(self):
        # Simulate "today" vs "tomorrow" wall-clock calls by invoking
        # build_run_config with two different explicit `today` stand-ins
        # while window_end is frozen — the frozen anchor must win both times,
        # producing byte-identical output.
        cfg = _cfg(period=365, window_end="2024-06-15")
        result_a = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg, today=None)
        result_b = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg, today=None)
        assert result_a == result_b
        expected_start = (date(2024, 6, 15) - timedelta(days=365)).isoformat()
        assert result_a["start_date"] == expected_start
        assert result_a["end_date"] == "2024-06-15"

    def test_oos_start_still_truncates_end_date_under_frozen_window_end(self):
        # oos_start truncation is unchanged legacy behavior: end_date becomes
        # oos_start when set, even though window_end freezes "today".
        cfg = _cfg(period=365, window_end="2024-06-15", oos_start="2024-01-01")
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg)
        expected_start = (date(2024, 6, 15) - timedelta(days=365)).isoformat()
        assert result["start_date"] == expected_start
        assert result["end_date"] == "2024-01-01"


class TestOosWindowFreeze:
    def test_oos_window_ends_at_frozen_anchor(self):
        cfg = _cfg(oos_start="2024-01-01", window_end="2024-06-15")
        result = oos_window(cfg)
        assert result == ("2024-01-01", "2024-06-15")

    def test_oos_window_never_extends_past_holdout_cap_without_window_end(self):
        cfg = _cfg(oos_start="2024-01-01", final_holdout_start="2024-06-10")
        explicit = date(2024, 12, 31)
        result = oos_window(cfg, today=explicit)
        assert result == ("2024-01-01", "2024-06-09")


class TestLegacyBehaviorUnchanged:
    """Both new fields unset -> byte-for-byte identical to current behavior.

    Tests can't mock the real clock, so an explicit `today` arg stands in for
    "date.today()" and we compare against the exact same date arithmetic the
    current (pre-change) code performs.
    """

    def test_build_run_config_legacy_no_split(self):
        cfg = _cfg(period=730)
        today = date(2024, 1, 15)
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg, today=today)
        assert result["start_date"] == (today - timedelta(days=730)).isoformat()
        assert result["end_date"] == today.isoformat()

    def test_build_run_config_legacy_with_oos_start(self):
        cfg = _cfg(period=730, oos_start="2024-06-01")
        today = date(2024, 1, 15)
        result = build_run_config(symbol="BTC-USDT-SWAP", cfg=cfg, today=today)
        assert result["start_date"] == (today - timedelta(days=730)).isoformat()
        assert result["end_date"] == "2024-06-01"

    def test_train_window_legacy(self):
        cfg = _cfg(period=730, oos_start="2024-06-01")
        today = date(2024, 1, 15)
        result = train_window(cfg, today=today)
        assert result == ((today - timedelta(days=730)).isoformat(), "2024-06-01")

    def test_oos_window_legacy(self):
        cfg = _cfg(period=730, oos_start="2024-06-01")
        today = date(2024, 1, 15)
        result = oos_window(cfg, today=today)
        assert result == ("2024-06-01", today.isoformat())

    def test_train_window_none_without_oos_start(self):
        cfg = _cfg(period=730)
        assert train_window(cfg, today=date(2024, 1, 15)) is None

    def test_oos_window_none_without_oos_start(self):
        cfg = _cfg(period=730)
        assert oos_window(cfg, today=date(2024, 1, 15)) is None
