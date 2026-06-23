"""Integration tests for FastAPI endpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


STRATEGY_PAYLOAD = {
    "schema_version": 1,
    "strategy_id": "strat_btc_001",
    "symbol": "BTC",
    "generated_at": "2024-01-01T00:00:00Z",
    "pipeline_stage": 2,
    "spec": {
        "strategy_id": "strat_btc_001",
        "symbol": "BTC",
        "spec_yaml": "runs/strat_btc_001/config.yaml",
    },
}


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    manifests = tmp_path / "research" / "manifests" / "strat_btc_001"
    manifests.mkdir(parents=True)
    (manifests / "manifest.json").write_text(
        json.dumps(STRATEGY_PAYLOAD), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def client(repo_root: Path):
    os.environ["REPO_ROOT"] = str(repo_root)
    import importlib
    import main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /api/strategies
# ---------------------------------------------------------------------------

def test_list_strategies(client):
    r = client.get("/api/strategies")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["strategy_id"] == "strat_btc_001"
    assert data[0]["symbol"] == "BTC"
    assert data[0]["pipeline_stage"] == 2
    assert data[0]["gate_pass"] is None  # no gate block yet


def test_list_strategies_empty(tmp_path):
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        r = c.get("/api/strategies")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# GET /api/strategies/{id}/promote — promotion status
# ---------------------------------------------------------------------------

def test_promote_status_roundtrip(repo_root, tmp_path):
    """GET reports promotion state; reflects promote then demote."""
    os.environ["REPO_ROOT"] = str(repo_root)
    os.environ["DASHBOARD_DIR"] = str(tmp_path / "dash")
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    try:
        with TestClient(app) as c:
            # initially not promoted
            r = c.get("/api/strategies/strat_btc_001/promote")
            assert r.status_code == 200
            body = r.json()
            assert body["strategy_id"] == "strat_btc_001"
            assert body["promoted"] is False
            assert body["running"] is False

            # promote → status flips true
            assert c.post("/api/strategies/strat_btc_001/promote", json={}).status_code == 201
            assert c.get("/api/strategies/strat_btc_001/promote").json()["promoted"] is True

            # demote → status flips back false
            c.request("DELETE", "/api/strategies/strat_btc_001/promote")
            assert c.get("/api/strategies/strat_btc_001/promote").json()["promoted"] is False
    finally:
        os.environ.pop("DASHBOARD_DIR", None)


def _write_running_testnet(repo_root: Path, strategy_id: str = "strat_btc_001",
                           testnet_id: str = "strat_btc_001_paper",
                           status: str = "running", mode: str = "paper") -> None:
    """Emit a trader-style testnet_status.json so the strategy looks deployed."""
    d = repo_root / "runs" / "testnet" / testnet_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "testnet_status.json").write_text(json.dumps({
        "schema_version": 1,
        "testnet_id": testnet_id,
        "strategy_id": strategy_id,
        "symbol": "BTC",
        "mode": mode,
        "live": {
            "started_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "status": status,
        },
        "killswitch": {},
    }), encoding="utf-8")


def test_promote_status_reports_running(repo_root, tmp_path):
    """GET promote status surfaces an out-of-band live trader as running=True."""
    _write_running_testnet(repo_root)
    os.environ["REPO_ROOT"] = str(repo_root)
    os.environ["DASHBOARD_DIR"] = str(tmp_path / "dash")
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    try:
        with TestClient(app) as c:
            body = c.get("/api/strategies/strat_btc_001/promote").json()
            assert body["running"] is True
            assert body["running_testnet_id"] == "strat_btc_001_paper"
            assert body["running_mode"] == "paper"
            assert body["promoted"] is False  # deployed out-of-band, never promoted
    finally:
        os.environ.pop("DASHBOARD_DIR", None)


def test_promote_blocked_when_running(repo_root, tmp_path):
    """POST promote is rejected with 409 while a trader is live on the strategy."""
    _write_running_testnet(repo_root)
    os.environ["REPO_ROOT"] = str(repo_root)
    os.environ["DASHBOARD_DIR"] = str(tmp_path / "dash")
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    try:
        with TestClient(app) as c:
            r = c.post("/api/strategies/strat_btc_001/promote", json={})
            assert r.status_code == 409
            assert "already running" in r.json()["detail"]
    finally:
        os.environ.pop("DASHBOARD_DIR", None)


def test_promote_allowed_when_testnet_stopped(repo_root, tmp_path):
    """A stopped testnet status must not block promote (only running/paused do)."""
    _write_running_testnet(repo_root, status="stopped")
    os.environ["REPO_ROOT"] = str(repo_root)
    os.environ["DASHBOARD_DIR"] = str(tmp_path / "dash")
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    try:
        with TestClient(app) as c:
            assert c.get("/api/strategies/strat_btc_001/promote").json()["running"] is False
            assert c.post("/api/strategies/strat_btc_001/promote", json={}).status_code == 201
    finally:
        os.environ.pop("DASHBOARD_DIR", None)


# ---------------------------------------------------------------------------
# GET /api/strategies/{id}
# ---------------------------------------------------------------------------

def test_get_strategy(client):
    r = client.get("/api/strategies/strat_btc_001")
    assert r.status_code == 200
    data = r.json()
    assert data["strategy_id"] == "strat_btc_001"
    assert data["spec"]["symbol"] == "BTC"


def test_get_strategy_404(client):
    r = client.get("/api/strategies/nonexistent")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /api/strategies/{id}/equity  &  /trades
# ---------------------------------------------------------------------------

EQUITY_CSV = "timestamp,equity\n2024-01-01T00:00:00,10000.5\n2024-01-02T00:00:00,10250.0\n"
TRADES_CSV = "entry_time,side,pnl\n2024-01-01T08:00:00,long,250.5\n"


@pytest.fixture()
def repo_root_with_run(tmp_path: Path) -> Path:
    manifests = tmp_path / "research" / "manifests" / "strat_btc_001"
    manifests.mkdir(parents=True)
    (manifests / "manifest.json").write_text(json.dumps(STRATEGY_PAYLOAD), encoding="utf-8")
    run_dir = tmp_path / "runs" / "strat_btc_001_base"
    run_dir.mkdir(parents=True)
    (run_dir / "equity.csv").write_text(EQUITY_CSV, encoding="utf-8")
    (run_dir / "trades.csv").write_text(TRADES_CSV, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client_with_run(repo_root_with_run: Path):
    os.environ["REPO_ROOT"] = str(repo_root_with_run)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


def test_get_equity(client_with_run):
    r = client_with_run.get("/api/strategies/strat_btc_001/equity?run=runs/strat_btc_001_base")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]["equity"] == 10000.5


def test_get_trades(client_with_run):
    r = client_with_run.get("/api/strategies/strat_btc_001/trades?run=runs/strat_btc_001_base")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["side"] == "long"


def test_get_equity_strategy_404(client_with_run):
    r = client_with_run.get("/api/strategies/ghost/equity?run=runs/strat_btc_001_base")
    assert r.status_code == 404


def test_get_equity_csv_missing(client_with_run):
    r = client_with_run.get("/api/strategies/strat_btc_001/equity?run=runs/nonexistent_run")
    assert r.status_code == 404


def test_get_equity_no_run_no_manifest_default(client):
    # manifest has no backtest block → no default run → 404
    r = client.get("/api/strategies/strat_btc_001/equity")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/factor-analysis, /api/regime, /api/selection
# ---------------------------------------------------------------------------

FACTOR_PAYLOAD = {
    "schema_version": 1,
    "symbol": "BTC",
    "generated_at": "2024-01-01T00:00:00Z",
    "period_days": 365,
    "horizons_h": [4, 8, 24],
    "factors": [
        {
            "name": "funding_rate",
            "ic_by_horizon": {"4": 0.12, "8": 0.09, "24": 0.06},
            "ir": 0.8,
            "sample_size": 500,
            "verdict": "single_use",
        }
    ],
}

SELECTION_PAYLOAD = {
    "schema_version": 1,
    "generated_at": "2024-01-01T00:00:00Z",
    "method": "weighted_score",
    "ranking": [{"strategy_id": "strat_btc_001", "symbol": "BTC", "rank": 1, "score": 0.92, "selected": True}],
}

REGIME_PAYLOAD = {"symbol": "BTC", "current_regime": "bull", "distribution": {"bull": 0.6, "bear": 0.3, "neutral": 0.1}}


@pytest.fixture()
def repo_root_full(tmp_path: Path) -> Path:
    m = tmp_path / "research" / "manifests"
    m.mkdir(parents=True)
    (m / "factor_BTC.json").write_text(json.dumps(FACTOR_PAYLOAD), encoding="utf-8")
    (m / "selection.json").write_text(json.dumps(SELECTION_PAYLOAD), encoding="utf-8")
    (m / "regime_BTC.json").write_text(json.dumps(REGIME_PAYLOAD), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client_full(repo_root_full: Path):
    os.environ["REPO_ROOT"] = str(repo_root_full)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


def test_get_factor_analysis(client_full):
    r = client_full.get("/api/factor-analysis")
    assert r.status_code == 200
    payload = r.json()
    assert isinstance(payload, list)
    btc = next((m for m in payload if m["symbol"] == "BTC"), None)
    assert btc is not None
    assert len(btc["factors"]) == 1


def test_get_factor_analysis_excludes_absent_symbol(client_full):
    # Endpoint returns all factor manifests; fixture only has BTC, so ETH
    # must not appear (no per-symbol 404 path on the list endpoint).
    r = client_full.get("/api/factor-analysis")
    assert r.status_code == 200
    assert "ETH" not in {m["symbol"] for m in r.json()}


def test_get_regime(client_full):
    r = client_full.get("/api/regime?symbol=BTC")
    assert r.status_code == 200
    assert r.json()["current_regime"] == "bull"


def test_get_regime_404(client_full):
    r = client_full.get("/api/regime?symbol=ETH")
    assert r.status_code == 404


def test_get_selection(client_full):
    r = client_full.get("/api/selection")
    assert r.status_code == 200
    assert len(r.json()["ranking"]) == 1


def test_get_selection_404(tmp_path):
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        r = c.get("/api/selection")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/reports — allowlist
# ---------------------------------------------------------------------------

@pytest.fixture()
def client_reports(tmp_path: Path):
    research = tmp_path / "research"
    research.mkdir()
    (research / "report.md").write_text("# Analysis\nContent here", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "creds.txt").write_text("password=abc", encoding="utf-8")
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


def test_get_report_allowed(client_reports):
    r = client_reports.get("/api/reports?path=research/report.md")
    assert r.status_code == 200
    assert "Analysis" in r.json()["content"]


def test_get_report_outside_allowlist(client_reports):
    r = client_reports.get("/api/reports?path=secret/creds.txt")
    assert r.status_code == 403


def test_get_report_traversal(client_reports):
    r = client_reports.get("/api/reports?path=research/../secret/creds.txt")
    assert r.status_code == 403


def test_get_report_missing_file(client_reports):
    r = client_reports.get("/api/reports?path=research/nonexistent.md")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/testnet  &  /api/testnet/{id}
# ---------------------------------------------------------------------------

TESTNET_PAYLOAD = {
    "schema_version": 1,
    "testnet_id": "tn_001",
    "strategy_id": "strat_btc_001",
    "symbol": "BTC",
    "live": {
        "started_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T01:00:00Z",
        "status": "running",
    },
    "killswitch": {"triggered": False},
}


@pytest.fixture()
def client_testnet(tmp_path: Path):
    tn = tmp_path / "runs" / "testnet" / "tn_001"
    tn.mkdir(parents=True)
    (tn / "testnet_status.json").write_text(json.dumps(TESTNET_PAYLOAD), encoding="utf-8")
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


def test_list_testnet(client_testnet):
    r = client_testnet.get("/api/testnet")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["testnet_id"] == "tn_001"


def test_get_testnet(client_testnet):
    r = client_testnet.get("/api/testnet/tn_001")
    assert r.status_code == 200
    assert r.json()["strategy_id"] == "strat_btc_001"


def test_get_testnet_404(client_testnet):
    r = client_testnet.get("/api/testnet/nonexistent")
    assert r.status_code == 404


def test_list_testnet_empty(tmp_path):
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        r = c.get("/api/testnet")
    assert r.status_code == 200
    assert r.json() == []


def test_get_testnet_mode_defaults_null_when_absent(client_testnet):
    # TESTNET_PAYLOAD has no "mode" → schema default None → JSON null.
    r = client_testnet.get("/api/testnet/tn_001")
    assert r.status_code == 200
    assert r.json()["mode"] is None


@pytest.fixture()
def client_testnet_csv(tmp_path: Path):
    tn = tmp_path / "runs" / "testnet" / "tn_001"
    tn.mkdir(parents=True)
    payload = {**TESTNET_PAYLOAD, "mode": "paper"}
    (tn / "testnet_status.json").write_text(json.dumps(payload), encoding="utf-8")
    (tn / "equity.csv").write_text(
        "timestamp,equity\n"
        "2024-01-01T00:00:00Z,10000\n"
        "2024-01-01T01:00:00Z,10050\n",
        encoding="utf-8",
    )
    (tn / "trades.csv").write_text(
        "timestamp,symbol,side,qty,price\n"
        "2024-01-01T00:30:00Z,BTC/USDT:USDT,buy,0.01,60000\n",
        encoding="utf-8",
    )
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        yield c


def test_get_testnet_status_reports_mode(client_testnet_csv):
    r = client_testnet_csv.get("/api/testnet/tn_001")
    assert r.status_code == 200
    assert r.json()["mode"] == "paper"


def test_get_testnet_equity_returns_live_curve(client_testnet_csv):
    r = client_testnet_csv.get("/api/testnet/tn_001/equity")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]["equity"] == 10000
    assert data[-1]["equity"] == 10050


def test_get_testnet_trades_returns_live_fills(client_testnet_csv):
    r = client_testnet_csv.get("/api/testnet/tn_001/trades")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["side"] == "buy"
    assert data[0]["price"] == 60000


def test_get_testnet_equity_404_when_no_csv(client_testnet):
    # Basic fixture writes status JSON only, no equity.csv.
    r = client_testnet.get("/api/testnet/tn_001/equity")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/pipeline
# ---------------------------------------------------------------------------

def test_get_pipeline(client):
    r = client.get("/api/pipeline")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["strategy_id"] == "strat_btc_001"
    assert data[0]["pipeline_stage"] == 2


def test_get_pipeline_empty(tmp_path):
    os.environ["REPO_ROOT"] = str(tmp_path)
    import importlib, main as main_module
    importlib.reload(main_module)
    from main import app
    with TestClient(app) as c:
        r = c.get("/api/pipeline")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# POST /api/pipeline/run — stress flag
# ---------------------------------------------------------------------------

def test_run_stage3_stress_accepted(client, monkeypatch):
    monkeypatch.setattr("main._config_symbols", lambda: ["ETH"])
    r = client.post(
        "/api/pipeline/run",
        json={"kind": "stage", "stage": "3", "symbol": "ETH", "stress": True},
    )
    assert r.status_code == 201
    assert r.json()["stress"] is True


def test_run_stress_rejected_on_non_stage3(client):
    r = client.post(
        "/api/pipeline/run",
        json={"kind": "stage", "stage": "1", "stress": True},
    )
    assert r.status_code == 400


def test_run_stress_rejected_on_pipeline(client):
    r = client.post(
        "/api/pipeline/run",
        json={"kind": "pipeline", "stress": True},
    )
    assert r.status_code == 400


def test_run_stage3_without_stress_still_works(client, monkeypatch):
    monkeypatch.setattr("main._config_symbols", lambda: ["ETH"])
    r = client.post(
        "/api/pipeline/run",
        json={"kind": "stage", "stage": "3", "symbol": "ETH"},
    )
    assert r.status_code == 201
    assert r.json()["stress"] is False


def test_run_unknown_symbol_rejected_with_detail(client, monkeypatch):
    """A symbol not in research_config (e.g. a retired disk-only symbol) is
    rejected 400 with a legible detail, not a silent failure."""
    monkeypatch.setattr("main._config_symbols", lambda: ["btc", "eth"])
    r = client.post(
        "/api/pipeline/run",
        json={"kind": "pipeline", "symbol": "sol"},
    )
    assert r.status_code == 400
    assert "sol" in r.json()["detail"]
