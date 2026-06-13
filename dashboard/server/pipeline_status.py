"""Pure builder: scan research/manifests/ -> PipelineStatus (A1 status page).

No I/O beyond reading files. Defensive: any unreadable/invalid artifact is
treated as missing rather than raising, so the endpoint never 500s.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schemas import (
    PipelineStatus,
    StageStatus,
    StrategyPipeline,
    SymbolPipeline,
)


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _artifact_time(path: Path, raw: Optional[dict]) -> Optional[str]:
    """ISO time for an artifact: JSON ``generated_at`` if present, else mtime."""
    if raw and isinstance(raw.get("generated_at"), str):
        return raw["generated_at"]
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class _Raw:
    stage_id: str
    label: str
    generated_at: Optional[str]
    metric_label: Optional[str]
    metric_value: Optional[str]
    present: bool


def _apply_staleness(raws: list["_Raw"], seed_time: Optional[str]) -> list[StageStatus]:
    """Turn ordered raw stages into StageStatus, computing done/stale/missing.

    A present stage is ``stale`` if its time is older than the max time of any
    upstream present stage (or the ``seed_time`` from an earlier chain, e.g. a
    strategy's symbol-level stage 2). Otherwise ``done``. Absent -> ``missing``.
    """
    out: list[StageStatus] = []
    max_up = _parse_iso(seed_time)
    for r in raws:
        if not r.present:
            state = "missing"
        else:
            t = _parse_iso(r.generated_at)
            if max_up is not None and t is not None and t < max_up:
                state = "stale"
            else:
                state = "done"
            if t is not None and (max_up is None or t > max_up):
                max_up = t
        out.append(StageStatus(
            stage_id=r.stage_id, label=r.label, state=state,
            generated_at=r.generated_at,
            metric_label=r.metric_label, metric_value=r.metric_value,
        ))
    return out


def _raw_0a(md: Path, sym: str) -> _Raw:
    meta_p = md / f"features_{sym}.meta.json"
    meta = _load_json(meta_p)
    if meta is None:
        return _Raw("0a", "Features/Evidence", None, None, None, False)
    gen = _artifact_time(meta_p, meta)
    ev = _load_json(md / f"evidence_{sym}.json")
    if ev and isinstance(ev.get("evidence"), list) and ev["evidence"]:
        top = 0.0
        for e in ev["evidence"]:
            for v in (e.get("ic_by_horizon") or {}).values():
                if isinstance(v, (int, float)):
                    top = max(top, abs(v))
        return _Raw("0a", "Features/Evidence", gen, "top|IC|", f"{top:.3f}", True)
    n = len(meta.get("feature_names") or [])
    return _Raw("0a", "Features/Evidence", gen, "features", str(n), True)


def _raw_0(md: Path, sym: str) -> _Raw:
    p = md / f"candidates_{sym}.json"
    raw = _load_json(p)
    if raw is None:
        return _Raw("0", "Discovery", None, None, None, False)
    n = len(raw.get("candidates") or [])
    return _Raw("0", "Discovery", _artifact_time(p, raw), "candidates", str(n), True)


def _raw_1(md: Path, sym: str) -> _Raw:
    p = md / f"factor_{sym}.json"
    raw = _load_json(p)
    if raw is None:
        return _Raw("1", "Factors", None, None, None, False)
    counts = {"single_use": 0, "ensemble_only": 0, "reject": 0}
    for f in raw.get("factors") or []:
        v = f.get("verdict")
        if v in counts:
            counts[v] += 1
    val = f"S{counts['single_use']} E{counts['ensemble_only']} R{counts['reject']}"
    return _Raw("1", "Factors", _artifact_time(p, raw), "verdicts", val, True)


def _raw_2(md: Path, sym: str) -> _Raw:
    """Count strategies emitted for this symbol (dirs with generation.json,
    id prefixed ``<sym>_``). Time = newest generation.json among them."""
    ids = _strategy_ids_for_symbol(md, sym)
    times: list[str] = []
    for sid in ids:
        gp = md / sid / "generation.json"
        raw = _load_json(gp)
        if raw is not None:
            t = _artifact_time(gp, raw)
            if t:
                times.append(t)
    if not times:
        return _Raw("2", "Strategies", None, None, None, False)
    newest = max(times, key=lambda s: _parse_iso(s) or datetime.min.replace(tzinfo=timezone.utc))
    return _Raw("2", "Strategies", newest, "emitted", str(len(times)), True)


def _raw_25(md: Path, sym: str) -> _Raw:
    p = md / f"regime_{sym}.json"
    raw = _load_json(p)
    if raw is None:
        return _Raw("2.5", "Regime", None, None, None, False)
    breakdown = raw.get("breakdown") or []
    dominant = None
    if breakdown:
        tally: dict[str, int] = {}
        for row in breakdown:
            lbl = row.get("regime")
            if lbl:
                tally[lbl] = tally.get(lbl, 0) + 1
        if tally:
            dominant = max(tally, key=lambda k: tally[k])
    return _Raw("2.5", "Regime", _artifact_time(p, raw),
                "regime" if dominant else None, dominant, True)


def _strategy_ids_for_symbol(md: Path, sym: str) -> list[str]:
    """Strategy manifest dirs whose id is prefixed ``<sym>_`` (e.g. btc_s1_...)."""
    out: list[str] = []
    if not md.is_dir():
        return out
    for child in md.iterdir():
        if child.is_dir() and child.name.startswith(f"{sym}_"):
            out.append(child.name)
    return sorted(out)


def build_symbol_stages(md: Path, sym: str) -> list[StageStatus]:
    raws = [_raw_0a(md, sym), _raw_0(md, sym), _raw_1(md, sym),
            _raw_2(md, sym), _raw_25(md, sym)]
    return _apply_staleness(raws, seed_time=None)


def _raw_3(md: Path, sid: str) -> _Raw:
    p = md / sid / "diagnosis.json"
    raw = _load_json(p)
    if raw is None:
        return _Raw("3", "Backtest+Diag", None, None, None, False)
    action = raw.get("recommended_action")
    return _Raw("3", "Backtest+Diag", _artifact_time(p, raw),
                "action" if action else None, action, True)


def _raw_4(md: Path, sid: str) -> _Raw:
    p = md / sid / "optimization.json"
    raw = _load_json(p)
    if raw is None:
        return _Raw("4", "Optimize", None, None, None, False)
    metrics = raw.get("best_metrics") or {}
    sharpe = metrics.get("sharpe")
    val = f"{sharpe:.2f}" if isinstance(sharpe, (int, float)) else None
    return _Raw("4", "Optimize", _artifact_time(p, raw),
                "best sharpe" if val else None, val, True)


def _raw_5(md: Path, sid: str, selection: Optional[dict],
           selection_time: Optional[str]) -> _Raw:
    if selection is None:
        return _Raw("5", "Select", None, None, None, False)
    entry = None
    for row in selection.get("ranking") or []:
        if row.get("strategy_id") == sid:
            entry = row
            break
    if entry is None:
        return _Raw("5", "Select", None, None, None, False)
    score = entry.get("score")
    mark = "✓" if entry.get("selected") else "✗"
    val = f"{mark} {score:.2f}" if isinstance(score, (int, float)) else mark
    return _Raw("5", "Select", selection_time, "selected", val, True)


def build_strategy_pipeline(md: Path, sid: str, seed_time: Optional[str],
                            selection: Optional[dict],
                            selection_time: Optional[str]) -> StrategyPipeline:
    raws = [_raw_3(md, sid), _raw_4(md, sid),
            _raw_5(md, sid, selection, selection_time)]
    stages = _apply_staleness(raws, seed_time=seed_time)
    return StrategyPipeline(strategy_id=sid, stages=stages)


def build_pipeline_status(repo_root: Path, config_symbols: list[str]) -> PipelineStatus:
    md = repo_root / "research" / "manifests"
    now = datetime.now(tz=timezone.utc).isoformat()
    if not md.is_dir():
        return PipelineStatus(generated_at=now, symbols=[])

    sel_p = md / "selection.json"
    selection = _load_json(sel_p)
    selection_time = _artifact_time(sel_p, selection) if selection is not None else None

    config_set = set(config_symbols)
    symbols: list[SymbolPipeline] = []
    for sym in discover_symbols(md, config_symbols):
        sym_stages = build_symbol_stages(md, sym)
        # Seed the strategy chain with this symbol's stage-2 time.
        stage2 = next((s for s in sym_stages if s.stage_id == "2"), None)
        seed_time = stage2.generated_at if stage2 else None
        strategies = [
            build_strategy_pipeline(md, sid, seed_time, selection, selection_time)
            for sid in _strategy_ids_for_symbol(md, sym)
        ]
        # Only config symbols are runnable; disk-only symbols (stale artifacts of
        # a retired symbol) would 400 at /api/pipeline/run, so flag them so the UI
        # disables their Run control instead of offering a button that always fails.
        symbols.append(SymbolPipeline(
            symbol=sym, stages=sym_stages, strategies=strategies,
            runnable=sym in config_set,
        ))

    return PipelineStatus(generated_at=now, symbols=symbols)


def discover_symbols(manifests_dir: Path, config_symbols: list[str]) -> list[str]:
    """Union of config symbols (first, in order) and symbols found on disk."""
    found: set[str] = set()
    for pattern in ("factor_*.json", "evidence_*.json", "candidates_*.json"):
        for p in manifests_dir.glob(pattern):
            name = p.stem.split(".")[0]  # candidates_eth.failed -> candidates_eth
            for prefix in ("factor_", "evidence_", "candidates_"):
                if name.startswith(prefix):
                    sym = name[len(prefix):]
                    if sym and not sym.startswith("values_"):
                        found.add(sym)
                    break
    extra = sorted(found - set(config_symbols))
    return list(config_symbols) + extra
