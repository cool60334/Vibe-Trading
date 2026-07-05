"""Append-only research ledger (JSONL) — the funnel's flight recorder.

DSR only penalises trials INSIDE one stage-4 sweep. Everything upstream —
how many factors stage 0a screened, how many sweeps ran, how many times the
same OOS window has been evaluated for a symbol — went uncounted, which is
exactly how a holdout degrades into a second train set. This module makes
those counts durable so gates and humans can SEE the multiple-testing debt.

Transparency, not punishment: counts are surfaced on manifests/gates as
red flags; they never hard-block by themselves. Writes are fail-soft — a
ledger hiccup must never fail a pipeline stage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LEDGER_NAME = "research_ledger.jsonl"


def _ledger_path(manifests_dir: "str | Path") -> Path:
    return Path(manifests_dir) / LEDGER_NAME


def append_event(
    manifests_dir: "str | Path",
    *,
    kind: str,
    symbol: str,
    strategy_id: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    """Append one event. Fail-soft: any OSError is printed, never raised."""
    event = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "kind": kind,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "detail": detail or {},
    }
    try:
        with _ledger_path(manifests_dir).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:  # ledger must never sink a stage
        print(f"[ledger] WARN: append failed: {exc}")


def read_events(manifests_dir: "str | Path") -> list:
    """All parseable events, oldest first. Corrupt lines are skipped."""
    path = _ledger_path(manifests_dir)
    if not path.exists():
        return []
    out: list = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def count_events(
    manifests_dir: "str | Path",
    kind: Optional[str] = None,
    symbol: Optional[str] = None,
) -> int:
    return sum(
        1
        for e in read_events(manifests_dir)
        if (kind is None or e.get("kind") == kind)
        and (symbol is None or e.get("symbol") == symbol)
    )


def research_accounting_block(manifests_dir: "str | Path", symbol: str) -> dict:
    """Per-symbol funnel counters for embedding into a strategy manifest."""
    return {
        "factor_screens_symbol": count_events(manifests_dir, "factor_screen", symbol),
        "sweeps_symbol": count_events(manifests_dir, "sweep", symbol),
        "oos_evals_symbol": count_events(manifests_dir, "oos_eval", symbol),
        "final_holdout_evals_symbol": count_events(manifests_dir, "final_holdout", symbol),
    }
