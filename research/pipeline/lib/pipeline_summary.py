"""Aggregate per-strategy manifests into one pipeline_summary.json.

Answers "what did this pipeline run conclude?" in one machine-readable,
all-UTC file — the direct input for any automation deciding what to do
next, instead of crawling manifests/selection/diagnosis separately.
Read-only over existing artifacts; never mutates them.

Schema note (verified against research/emit_manifest.py and
dashboard/server/schemas.py as of 2026-07-05): the per-strategy
``manifest.json`` (StrategyManifest schema) has NO top-level ``selected``
field — emit_manifest.py never writes one. ``selected`` only exists in the
separate ``selection.json`` (SelectionManifest.ranking, one SelectionEntry
per strategy_id), written by stage5_select.py. This module therefore reads
both files and joins them by strategy_id. Similarly, OOS trade count lives
at ``backtest.oos.trades`` (BacktestMetrics.trades), not ``trade_count``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _load_selected_map(manifests_dir: Path) -> dict:
    """Read selection.json and return {strategy_id: selected bool}.

    Returns an empty dict if selection.json is missing, corrupt, or
    malformed — callers should treat a missing key as "unknown" (None),
    not crash.
    """
    selection_path = manifests_dir / "selection.json"
    if not selection_path.exists():
        return {}
    try:
        data = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ranking = data.get("ranking") or []
    out = {}
    for entry in ranking:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("strategy_id")
        if sid is not None:
            out[sid] = entry.get("selected")
    return out


def _summarise_manifest(data: dict, selected: "bool | None") -> dict:
    gate = data.get("gate") or {}
    oos = (data.get("backtest") or {}).get("oos") or {}
    diagnosis = data.get("diagnosis") or {}
    return {
        "selected": selected,
        "recommended_action": diagnosis.get("recommended_action")
                              or data.get("recommended_action"),
        "gate_overall_pass": gate.get("overall_pass"),
        "gate_fatal_fail": gate.get("fatal_fail"),
        "oos_sharpe": oos.get("sharpe"),
        "oos_max_drawdown": oos.get("max_drawdown"),
        "oos_trade_count": oos.get("trades"),
    }


def build_pipeline_summary(manifests_dir: "str | Path") -> dict:
    manifests_dir = Path(manifests_dir)
    selected_map = _load_selected_map(manifests_dir)
    strategies: dict = {}
    for mf in sorted(manifests_dir.glob("*/manifest.json")):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # a corrupt manifest must not sink the summary
        sid = mf.parent.name
        strategies[sid] = _summarise_manifest(data, selected_map.get(sid))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "strategies": strategies,
    }


def write_pipeline_summary(manifests_dir: "str | Path") -> Path:
    manifests_dir = Path(manifests_dir)
    out = manifests_dir / "pipeline_summary.json"
    out.write_text(
        json.dumps(build_pipeline_summary(manifests_dir), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out
