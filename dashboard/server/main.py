from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import artifacts
import parsers
import pipeline_jobs
import pipeline_status
import state as state_module
import supervisor as supervisor_module
from ops_health import build_ops_health
from schemas import FATAL_GATE_CHECKS, StrategyManifest

# Repo root — override with REPO_ROOT env var for Docker / Linux deployment.
REPO_ROOT = Path(os.environ.get("REPO_ROOT", Path(__file__).parent.parent.parent))
# Dashboard data dir — one level up from server/ (override with DASHBOARD_DIR
# env var; used by tests to isolate promote state).
DASHBOARD_DIR = Path(os.environ.get("DASHBOARD_DIR", Path(__file__).parent.parent))

app = FastAPI(title="Quant Strategy Dashboard API", version="0.1.0")

# Initialise supervisor singleton at startup
supervisor_module.init_supervisor(REPO_ROOT, DASHBOARD_DIR)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "repo_root": str(REPO_ROOT)}


@app.get("/api/ops/health")
def api_ops_health():
    return build_ops_health(REPO_ROOT)


# ---------------------------------------------------------------------------
# 3.4 Strategy list & detail
# ---------------------------------------------------------------------------

def _strategy_row(m, interval: str, running_modes: dict[str, str] | None = None) -> dict:
    oos = m.backtest.oos if (m.backtest and m.backtest.oos) else None
    return {
        "strategy_id": m.strategy_id,
        # Live deployment mode ("paper"/"testnet"/"live") if a trader is running
        # this strategy, else None. Lets the list surface running strategies
        # regardless of their manifest gate state (a CLI-deployed paper strategy
        # whose manifest lacks a gate would otherwise show as N/A).
        "running_mode": (running_modes or {}).get(m.strategy_id),
        "symbol": m.symbol,
        "pipeline_stage": m.pipeline_stage,
        "generated_at": m.generated_at.isoformat(),
        "gate_pass": m.gate.overall_pass if m.gate else None,
        "gate_fatal": m.gate.fatal_fail if m.gate else None,
        "sharpe": m.backtest.in_sample.sharpe if m.backtest else None,
        "max_drawdown": m.backtest.in_sample.max_drawdown if m.backtest else None,
        "red_flags": [f.value for f in m.gate.red_flags] if m.gate else [],
        # Stage-3 diagnosis verdict (proceed / back_to_stage_2 / back_to_stage_4);
        # null until a diagnosis run exists. Drives the list's next-step chip.
        "recommended_action": m.diagnosis.recommended_action.value if m.diagnosis else None,
        "interval": interval,
        # OOS (walk-forward held-out) — authoritative ranking metrics; null until an OOS run exists.
        "sharpe_oos": oos.sharpe if oos else None,
        "dd_oos": oos.max_drawdown if oos else None,
        "trades_oos": oos.trades if oos else None,
        "pf_oos": oos.profit_factor if oos else None,
    }


@app.get("/api/intervals")
def list_intervals() -> list[str]:
    return artifacts.discover_intervals(REPO_ROOT)


@app.get("/api/strategies")
def list_strategies(interval: str = Query("1H")) -> list[dict]:
    running_modes = artifacts.running_modes_by_strategy(REPO_ROOT)
    if interval == "all":
        rows: list[dict] = []
        for iv in artifacts.discover_intervals(REPO_ROOT):
            rows.extend(
                _strategy_row(m, iv, running_modes)
                for m in artifacts.list_strategy_manifests(REPO_ROOT, iv)
            )
        return rows
    return [
        _strategy_row(m, interval, running_modes)
        for m in artifacts.list_strategy_manifests(REPO_ROOT, interval)
    ]


@app.get("/api/strategies/{strategy_id}")
def get_strategy(strategy_id: str, interval: str = Query("1H")) -> StrategyManifest:
    manifest = artifacts.get_strategy_manifest(REPO_ROOT, strategy_id, interval)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
    return manifest


# ---------------------------------------------------------------------------
# 3.5 Equity curve & trades
# ---------------------------------------------------------------------------

def _resolve_run_csv(strategy_id: str, run: Optional[str], filename: str) -> Path:
    """Return path to <run>/<filename>; 404 if not found.

    Manifests store ``source_run`` under two historical conventions:
      - full path relative to repo root, e.g. ``runs/btc_s1_base/artifacts``
        (legacy manifests), and
      - bare run name, e.g. ``btc_s9_base`` (current emit_manifest output),
        whose artifacts live at ``runs/<run>/artifacts/``.
    Try both so every strategy resolves regardless of which convention its
    manifest used.
    """
    manifest = artifacts.get_strategy_manifest(REPO_ROOT, strategy_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    run_dir = run
    if run_dir is None and manifest.backtest and manifest.backtest.in_sample:
        run_dir = manifest.backtest.in_sample.source_run
    if run_dir is None:
        raise HTTPException(status_code=404, detail="No run specified and no default in manifest")

    repo_resolved = REPO_ROOT.resolve()
    candidates = [
        REPO_ROOT / run_dir / filename,                      # full path (legacy)
        REPO_ROOT / "runs" / run_dir / "artifacts" / filename,  # bare run name (current)
        REPO_ROOT / "runs" / run_dir / filename,                # bare run name, no artifacts/
    ]
    for cand in candidates:
        path = cand.resolve()
        # Safety: must stay within repo_root
        if not path.is_relative_to(repo_resolved):
            continue
        if path.exists():
            return path

    raise HTTPException(status_code=404, detail=f"{filename} not found in run '{run_dir}'")


def _normalize_equity_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure each equity row has a ``time`` key (the X-axis the chart reads).

    equity.csv uses ``timestamp`` (live runs) or ``date`` (sample data) for the
    time column; the dashboard's EquityPoint expects ``time``. Alias it without
    dropping the other columns.
    """
    for row in rows:
        if "time" not in row or row.get("time") is None:
            ts = row.get("timestamp")
            if ts is None:
                ts = row.get("date")
            if ts is not None:
                row["time"] = ts
    return rows


@app.get("/api/strategies/{strategy_id}/equity")
def get_equity(
    strategy_id: str,
    run: Optional[str] = Query(default=None, description="Run directory relative to repo root"),
) -> list[dict[str, Any]]:
    path = _resolve_run_csv(strategy_id, run, "equity.csv")
    return _normalize_equity_rows(parsers.csv_to_records(path))


@app.get("/api/strategies/{strategy_id}/trades")
def get_trades(
    strategy_id: str,
    run: Optional[str] = Query(default=None, description="Run directory relative to repo root"),
) -> list[dict[str, Any]]:
    path = _resolve_run_csv(strategy_id, run, "trades.csv")
    return parsers.csv_to_records(path)


# ---------------------------------------------------------------------------
# 3.6 Factor analysis, regime, selection
# ---------------------------------------------------------------------------

from schemas import FactorManifest, SelectionManifest


@app.get("/api/factor-analysis")
def get_factor_analysis(interval: str = Query("1H")) -> list[FactorManifest]:
    iv = "1H" if interval == "all" else interval
    return artifacts.list_factor_manifests(REPO_ROOT, iv)


@app.get("/api/regime")
def get_regime(
    symbol: str = Query(..., description="Trading symbol, e.g. 'BTC'"),
    interval: str = Query("1H"),
) -> dict[str, Any]:
    data = artifacts.get_regime_manifest(REPO_ROOT, symbol, "1H" if interval == "all" else interval)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Regime manifest for '{symbol}' not found")
    return data


@app.get("/api/selection")
def get_selection(interval: str = Query("1H")) -> SelectionManifest:
    manifest = artifacts.get_selection_manifest(REPO_ROOT, "1H" if interval == "all" else interval)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Selection manifest not found")
    return manifest


# ---------------------------------------------------------------------------
# 3.7 Markdown reports — allowlist: research/ only
# ---------------------------------------------------------------------------

_REPORT_ALLOWED_DIRS = ["research"]


# ---------------------------------------------------------------------------
# 3.8 Pipeline stage overview
# ---------------------------------------------------------------------------

@app.get("/api/pipeline")
def get_pipeline() -> list[dict]:
    manifests = artifacts.list_strategy_manifests(REPO_ROOT)
    return [
        {
            "strategy_id": m.strategy_id,
            "symbol": m.symbol,
            "pipeline_stage": m.pipeline_stage,
            "generated_at": m.generated_at.isoformat(),
        }
        for m in manifests
    ]


def _config_symbols() -> list[str]:
    """Best-effort read of research_config.yaml symbol names; [] on any failure."""
    try:
        cfg = parsers.load_yaml(REPO_ROOT / "research" / "research_config.yaml")
        out: list[str] = []
        for s in cfg.get("symbols") or []:
            name = s.get("name") if isinstance(s, dict) else None
            if name:
                out.append(str(name))
        return out
    except Exception:
        logger.warning("_config_symbols: failed to read research_config.yaml", exc_info=True)
        return []


@app.get("/api/pipeline/status")
def get_pipeline_status():
    return pipeline_status.build_pipeline_status(REPO_ROOT, _config_symbols())


class PipelineRunRequest(BaseModel):
    kind: str
    stage: Optional[str] = None
    symbol: Optional[str] = None
    stress: bool = False
    interval: str = "1H"


@app.post("/api/pipeline/run", status_code=201)
def run_pipeline(body: PipelineRunRequest) -> dict:
    if body.stress and not (body.kind == "stage" and body.stage == "3"):
        raise HTTPException(status_code=400, detail="stress is only valid for a stage 3 run")
    if body.symbol is not None and body.symbol not in _config_symbols():
        raise HTTPException(status_code=400, detail=f"unknown symbol {body.symbol!r}")
    try:
        return pipeline_jobs.create_job(
            REPO_ROOT, body.kind, body.stage, body.symbol, body.stress, body.interval
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/pipeline/jobs")
def list_pipeline_jobs(limit: int = 50) -> list[dict]:
    return pipeline_jobs.list_jobs(REPO_ROOT, limit)


@app.get("/api/pipeline/jobs/{job_id}")
def get_pipeline_job(job_id: str) -> dict:
    job = pipeline_jobs.read_job(REPO_ROOT, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    job["log_tail"] = pipeline_jobs.tail_log(REPO_ROOT, job_id)
    return job


@app.post("/api/pipeline/jobs/{job_id}/cancel")
def cancel_pipeline_job(job_id: str) -> dict:
    job = pipeline_jobs.request_cancel(REPO_ROOT, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ---------------------------------------------------------------------------
# 3.7 Markdown reports — allowlist: research/ only
# ---------------------------------------------------------------------------

@app.get("/api/reports")
def get_report(
    path: str = Query(..., description="Path to markdown file relative to repo root"),
) -> dict[str, str]:
    target = (REPO_ROOT / path)
    if not parsers.is_path_allowed(target, REPO_ROOT, _REPORT_ALLOWED_DIRS):
        raise HTTPException(status_code=403, detail="Path not in allowed directories")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Report '{path}' not found")
    return {"path": path, "content": parsers.read_text(target)}


# ---------------------------------------------------------------------------
# 3.9 Promote / demote
# ---------------------------------------------------------------------------

class PromoteRequest(BaseModel):
    override_reason: Optional[str] = None


@app.post("/api/strategies/{strategy_id}/promote", status_code=201)
def promote_strategy(strategy_id: str, body: PromoteRequest = PromoteRequest()) -> dict:
    manifest = artifacts.get_strategy_manifest(REPO_ROOT, strategy_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")

    # Already-running guard — block re-promote of a strategy a trader is live on
    # (deployed via control.json out-of-band, so state.json may not know).
    running = artifacts.find_running_testnet(REPO_ROOT, strategy_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Strategy '{strategy_id}' is already running as testnet "
                f"'{running.testnet_id}' (mode={running.mode}, "
                f"status={running.live.status}). Stop it before re-promoting."
            ),
        )

    # Fatal gate check — hard block, no override allowed
    if manifest.gate and manifest.gate.fatal_fail:
        fatal_names = [
            t.name for t in manifest.gate.thresholds
            if t.fatal and not t.passed
        ]
        raise HTTPException(
            status_code=422,
            detail=f"Fatal gate failures cannot be overridden: {fatal_names}",
        )

    # Required validations not run yet — hard block (distinct from FATAL, not overridable)
    if manifest.gate and manifest.gate.not_tested:
        missing = manifest.gate.not_tested
        raise HTTPException(
            status_code=422,
            detail=(
                f"Not testable yet — missing required validation(s): {missing}. "
                "Run cost-stress" + (" and CPCV" if "cpcv" in missing else "")
                + " before promoting."
            ),
        )

    # Non-fatal gate failures require an override reason
    if manifest.gate and not manifest.gate.overall_pass and not body.override_reason:
        raise HTTPException(
            status_code=422,
            detail="Gate not fully passed — provide override_reason to override",
        )

    record = state_module.promote(
        DASHBOARD_DIR,
        REPO_ROOT,
        strategy_id,
        manifest.spec.spec_yaml,
        override_reason=body.override_reason,
    )
    return {"strategy_id": strategy_id, **record}


@app.get("/api/strategies/{strategy_id}/promote")
def get_promote_status(strategy_id: str) -> dict:
    """Promote + live-deploy state for *strategy_id* (drives the UI button).

    ``running`` reflects whether a trader is actively live on this strategy
    (testnet_status.json running/paused) — true even for out-of-band control.json
    deployments the dashboard promote flow never recorded.
    """
    running = artifacts.find_running_testnet(REPO_ROOT, strategy_id)
    return {
        "strategy_id": strategy_id,
        "promoted": state_module.is_promoted(DASHBOARD_DIR, strategy_id),
        "running": running is not None,
        "running_testnet_id": running.testnet_id if running else None,
        "running_mode": running.mode if running else None,
    }


@app.delete("/api/strategies/{strategy_id}/promote", status_code=200)
def demote_strategy(strategy_id: str) -> dict:
    removed = state_module.demote(DASHBOARD_DIR, strategy_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' is not promoted")
    return {"strategy_id": strategy_id, "demoted": True}


# ---------------------------------------------------------------------------
# 3.10 Testnet status
# ---------------------------------------------------------------------------

from schemas import TestnetStatus


@app.get("/api/testnet")
def list_testnet() -> list[TestnetStatus]:
    return artifacts.list_testnet_statuses(REPO_ROOT)


@app.get("/api/testnet/{testnet_id}")
def get_testnet(testnet_id: str) -> TestnetStatus:
    status = artifacts.get_testnet_status(REPO_ROOT, testnet_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Testnet '{testnet_id}' not found")
    return status


def _resolve_testnet_csv(testnet_id: str, filename: str) -> Path:
    """Path to the live trader's runs/testnet/<id>/<filename>; 404 if missing."""
    path = (REPO_ROOT / "runs" / "testnet" / testnet_id / filename).resolve()
    if not path.is_relative_to((REPO_ROOT / "runs" / "testnet").resolve()):
        raise HTTPException(status_code=403, detail="Path outside testnet dir")
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found for '{testnet_id}'")
    return path


@app.get("/api/testnet/{testnet_id}/equity")
def get_testnet_equity(testnet_id: str) -> list[dict[str, Any]]:
    """Live virtual-equity curve for a running trader (paper/testnet/live)."""
    return parsers.csv_to_records(_resolve_testnet_csv(testnet_id, "equity.csv"))


@app.get("/api/testnet/{testnet_id}/trades")
def get_testnet_trades(testnet_id: str) -> list[dict[str, Any]]:
    """Live fills recorded by a running trader (paper/testnet/live)."""
    return parsers.csv_to_records(_resolve_testnet_csv(testnet_id, "trades.csv"))


# ---------------------------------------------------------------------------
# 6.5 Trader start / stop (v1.5)
# ---------------------------------------------------------------------------

class TraderStartRequest(BaseModel):
    strategy_id: str
    run_dir: str = ""      # repo-relative path to backtest run dir (has code/signal_engine.py)
    symbol: str = ""       # ccxt symbol e.g. "BTC/USDT:USDT"
    interval: str = "1H"
    qty: float = 0.001
    mode: Optional[str] = None  # paper (default) | testnet | live; None → $TRADING_MODE


@app.post("/api/testnet/{testnet_id}/start", status_code=201)
def start_trader(testnet_id: str, body: TraderStartRequest) -> dict:
    """Launch trader subprocess for a promoted strategy."""
    if not state_module.is_promoted(DASHBOARD_DIR, body.strategy_id):
        raise HTTPException(
            status_code=422,
            detail=f"Strategy '{body.strategy_id}' is not promoted — promote first",
        )
    sup = supervisor_module.get_supervisor()
    try:
        sup.start(
            strategy_id=body.strategy_id,
            testnet_id=testnet_id,
            run_dir=str(REPO_ROOT / body.run_dir) if body.run_dir else "",
            symbol=body.symbol,
            interval=body.interval,
            qty=body.qty,
            mode=body.mode,
        )
    except EnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"testnet_id": testnet_id, "strategy_id": body.strategy_id, "started": True}


@app.post("/api/testnet/{testnet_id}/stop", status_code=200)
def stop_trader(testnet_id: str, strategy_id: str = Query(...)) -> dict:
    """Request the trader for *testnet_id* to stop (flips its control file)."""
    sup = supervisor_module.get_supervisor()
    stopped = sup.stop(testnet_id)
    return {"testnet_id": testnet_id, "strategy_id": strategy_id, "stopped": stopped}


@app.get("/api/testnet/{testnet_id}/process")
def trader_process_status(testnet_id: str, strategy_id: str = Query(...)) -> dict:
    """Return the control-file state (running/stopped) for *testnet_id*."""
    sup = supervisor_module.get_supervisor()
    return sup.status(testnet_id)
