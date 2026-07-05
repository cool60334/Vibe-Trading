"""Tests for the append-only research ledger (pure JSONL, no external deps).

Run from repo root:  python -m pytest research/tests/test_research_ledger.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_RESEARCH_DIR = Path(__file__).resolve().parents[1]
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.research_ledger import (  # noqa: E402
    append_event,
    count_events,
    read_events,
    research_accounting_block,
)


class TestAppendReadRoundtrip:
    def test_roundtrip_order_and_fields(self, tmp_path):
        append_event(
            tmp_path,
            kind="sweep",
            symbol="eth",
            strategy_id="eth_s5",
            detail={"n_trials": 60},
        )
        append_event(tmp_path, kind="oos_eval", symbol="eth")

        events = read_events(tmp_path)

        assert len(events) == 2
        assert events[0]["kind"] == "sweep"
        assert events[0]["detail"]["n_trials"] == 60
        assert events[1]["kind"] == "oos_eval"
        assert events[0]["ts"].endswith("+00:00")
        assert events[1]["ts"].endswith("+00:00")


class TestCountEvents:
    def test_filters_by_kind_and_symbol(self, tmp_path):
        for _ in range(3):
            append_event(tmp_path, kind="oos_eval", symbol="eth")
        append_event(tmp_path, kind="oos_eval", symbol="sol")
        append_event(tmp_path, kind="sweep", symbol="eth")

        assert count_events(tmp_path, kind="oos_eval", symbol="eth") == 3
        assert count_events(tmp_path, kind="oos_eval") == 4
        assert count_events(tmp_path) == 5


class TestReadEventsCorruptLines:
    def test_skips_corrupt_lines(self, tmp_path):
        append_event(tmp_path, kind="sweep", symbol="eth")

        ledger_file = tmp_path / "research_ledger.jsonl"
        with ledger_file.open("a", encoding="utf-8") as f:
            f.write("{not json}\n")

        append_event(tmp_path, kind="oos_eval", symbol="eth")

        events = read_events(tmp_path)
        assert len(events) == 2


class TestAppendEventFailSoft:
    def test_never_raises_on_unwritable_dir(self, tmp_path):
        bad_dir = tmp_path / "no_such_subdir_without_parents" / "x"
        # Must not raise, even though bad_dir's parent doesn't exist.
        append_event(bad_dir, kind="sweep", symbol="eth")


class TestResearchAccountingBlock:
    def test_shape(self, tmp_path):
        append_event(
            tmp_path, kind="factor_screen", symbol="eth", detail={"n_features": 27}
        )
        append_event(tmp_path, kind="sweep", symbol="eth")
        append_event(tmp_path, kind="oos_eval", symbol="eth")
        append_event(tmp_path, kind="oos_eval", symbol="eth")

        block = research_accounting_block(tmp_path, "eth")

        assert block == {
            "factor_screens_symbol": 1,
            "sweeps_symbol": 1,
            "oos_evals_symbol": 2,
            "final_holdout_evals_symbol": 0,
        }
