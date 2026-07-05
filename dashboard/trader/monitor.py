"""Expected-vs-actual trade telemetry — pure functions, no network.

A strategy that goes silent for a month is either legitimately low-frequency
or broken (all-NaN transforms, stale regime mask, drifted factor
distribution). The kill switch only watches losses; these helpers give the
loop a "should have traded by now" dimension so silence becomes visible.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from trader.freshness import _symbol_short, _to_utc

_ESTIMATE_RE = re.compile(r"trades_per_year_estimate:\s*(\d+(?:\.\d+)?)")

#: Minimum expected fills/30d before zero fills is deemed suspicious.
#: 6 fills = ~3 trades -> P(0 trades | lambda=3) ~= 5%.
SILENCE_MIN_EXPECTED_FILLS = 6.0


def expected_fills_per_30d(repo_root: "str | Path", strategy_id: str) -> Optional[float]:
    """Fills (order executions) per 30 days implied by the strategy YAML's
    ``trades_per_year_estimate``; one round-trip trade = 2 fills. None when
    the registry/YAML/estimate is missing (fail open -- no alert)."""
    repo_root = Path(repo_root)
    try:
        runs = json.loads(
            (repo_root / "research" / "strategy_runs.json").read_text(encoding="utf-8")
        )
        spec_yaml = (runs.get(strategy_id) or {}).get("spec_yaml")
        if not spec_yaml:
            return None
        text = (repo_root / spec_yaml).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    m = _ESTIMATE_RE.search(text)
    if not m:
        return None
    trades_per_year = float(m.group(1))
    return trades_per_year / 365.25 * 30 * 2


def actual_fills_last_30d(out_dir: "str | Path", now: datetime) -> int:
    """Rows of trades.csv (one row = one fill) within the last 30 days."""
    path = Path(out_dir) / "trades.csv"
    if not path.exists():
        return 0
    cutoff = _to_utc(now).timestamp() - 30 * 86400
    n = 0
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ts = _to_utc(datetime.fromisoformat(row["timestamp"]))
                except (KeyError, ValueError):
                    continue
                if ts.timestamp() >= cutoff:
                    n += 1
    except OSError:
        return 0
    return n


def silence_alert_needed(expected_fills_30d: Optional[float], actual_fills_30d: int) -> bool:
    """True when the strategy should statistically have traded but hasn't."""
    if expected_fills_30d is None:
        return False
    return expected_fills_30d >= SILENCE_MIN_EXPECTED_FILLS and actual_fills_30d == 0


def regime_age_days(manifests_dir: "str | Path", symbol: str, now: datetime) -> Optional[float]:
    """Days since the last date in regime_<short>.json breakdown; None if
    the file is missing/unreadable/empty. The regime overlay IS the alpha for
    regime-filtered strategies -- a frozen regime silently masks all entries."""
    short = _symbol_short(symbol)
    path = Path(manifests_dir) / f"regime_{short}.json"
    try:
        breakdown = json.loads(path.read_text(encoding="utf-8")).get("breakdown") or []
    except (OSError, json.JSONDecodeError):
        return None
    if not breakdown:
        return None
    last = breakdown[-1].get("date")
    try:
        last_dt = datetime.strptime(str(last), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (_to_utc(now) - last_dt).total_seconds() / 86400
