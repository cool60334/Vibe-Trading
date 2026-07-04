"""Derive the OHLCV bar requirement of a strategy's rolling transforms.

The live loop feeds signal engines a finite OHLCV window; engines compute
rolling percentile/zscore transforms sized in *days* (``*_percentile_90d`` =
90*24 bars). When the window is shorter than the rolling ``min_periods`` the
transform is all-NaN, every entry condition is False and the signal silently
sticks at 0 (D2 diagnosis 2026-07-04 — eth_s5/sol_s1 paper traders never able
to trade on the 200-bar default). The trader derives the requirement from the
strategy YAML so the fetch window always covers the largest rolling window.

Pure functions — no network, no pandas.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

#: Warm-up margin on top of the largest rolling window: persistence lookback
#: (persist m/k, k <= 5), sma/ema smoothing (<= 8 bars in current specs), and
#: a little slack for a partial newest bar.
LOOKBACK_BUFFER_BARS = 16

# Matches the signal-compiler DSL: <indicator>_percentile_<n>d / _zscore_<n>d.
# Compiled engines size these windows as n*24 bars regardless of interval
# (they hard-code bars-per-day = 24), so n*24 is the engine-faithful bar count
# even for sub-hour runs — mirror the engine, not the calendar.
_ROLLING_DSL_RE = re.compile(r"_(?:percentile|zscore)_(\d+)d\b")
_ENGINE_BARS_PER_DAY = 24


def required_bars_from_yaml_text(text: str) -> Optional[int]:
    """Bar count the engine's largest rolling window needs, or None.

    None means the spec has no percentile/zscore DSL (nothing to derive) —
    callers fail open and keep their configured lookback.
    """
    days = [int(d) for d in _ROLLING_DSL_RE.findall(text or "")]
    if not days:
        return None
    return max(days) * _ENGINE_BARS_PER_DAY + LOOKBACK_BUFFER_BARS


def required_bars_for_strategy(repo_root: "str | Path", strategy_id: str) -> Optional[int]:
    """Resolve a strategy's YAML via research/strategy_runs.json and derive
    its bar requirement. Any missing file/entry → None (fail open: never block
    a trader start on registry quirks; the min_bars guard simply stays off).
    """
    repo_root = Path(repo_root)
    try:
        runs = json.loads(
            (repo_root / "research" / "strategy_runs.json").read_text(encoding="utf-8")
        )
        entry = runs.get(strategy_id) or {}
        spec_yaml = entry.get("spec_yaml")
        if not spec_yaml:
            return None
        text = (repo_root / spec_yaml).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return required_bars_from_yaml_text(text)
