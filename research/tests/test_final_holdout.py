"""
Tests for research/pipeline/final_holdout.py pure-logic helpers.

TDD: these tests are written BEFORE the implementation.

final_holdout.py is a MANUAL, one-shot CLI that deliberately "spends" the
reserved final-holdout window exactly once, for one strategy, right before a
promote decision. It backtests subprocess-invocation and filesystem I/O —
none of that is tested here. Only the two pure, deterministic helpers are
exercised:

  (a) holdout_window()        — (final_holdout_start, today) ISO date tuple
  (b) prior_holdout_evals()   — counts prior "final_holdout" ledger events
                                 for one strategy_id

Pytest is run from repo root as:
    python -m pytest research/tests/test_final_holdout.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Bootstrap: research/ must be on sys.path (same convention as
# test_stage3_backtest.py) so `pipeline.*` / `lib.*` imports resolve
# regardless of CWD or how pytest is invoked.
_THIS_FILE = Path(__file__).resolve()
_RESEARCH_DIR = _THIS_FILE.parents[1]   # research/
_REPO_ROOT = _RESEARCH_DIR.parent       # repo root

for _p in (_RESEARCH_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from pipeline.final_holdout import holdout_window, prior_holdout_evals  # noqa: E402
from lib.research_ledger import append_event  # noqa: E402


# ─── holdout_window() ──────────────────────────────────────────────────────


def test_holdout_window_explicit_today():
    result = holdout_window("2026-04-01", today=date(2026, 7, 5))
    assert result == ("2026-04-01", "2026-07-05")


def test_holdout_window_defaults_today_to_date_today():
    # today=None -> defaults to date.today(); just check the shape/type.
    result = holdout_window("2026-04-01")
    assert result[0] == "2026-04-01"
    assert result[1] == date.today().isoformat()


# ─── prior_holdout_evals() ──────────────────────────────────────────────────


def test_prior_holdout_evals_counts_only_matching_kind_and_strategy(tmp_path):
    append_event(tmp_path, kind="final_holdout", symbol="eth", strategy_id="eth_s5")
    append_event(tmp_path, kind="final_holdout", symbol="eth", strategy_id="eth_s9")
    append_event(tmp_path, kind="oos_eval", symbol="eth", strategy_id="eth_s5")

    assert prior_holdout_evals(tmp_path, "eth_s5") == 1
    assert prior_holdout_evals(tmp_path, "nope") == 0


def test_prior_holdout_evals_empty_ledger(tmp_path):
    assert prior_holdout_evals(tmp_path, "eth_s5") == 0
