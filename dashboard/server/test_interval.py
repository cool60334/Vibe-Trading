"""Interval-aware artifacts + API — run from dashboard/server/ (pytest test_interval.py)."""
import json
from pathlib import Path

import artifacts


def _write_manifest(base: Path, strategy_id: str) -> None:
    d = base / strategy_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "strategy_id": strategy_id, "symbol": "ETH-USDT-SWAP",
        "generated_at": "2026-06-15T00:00:00+00:00", "pipeline_stage": 5,
        "spec": {"source_run": None, "strategy_id": strategy_id, "symbol": "ETH-USDT-SWAP",
                 "spec_yaml": "x.yaml", "description": None},
        "generation": None, "reproducibility": None, "backtest": None,
        "optimization": None, "diagnosis": None, "gate": None,
    }), encoding="utf-8")


def test_manifests_base_1H_is_root():
    root = Path("/tmp/repo")
    assert artifacts._manifests_base(root, "1H") == root / "research" / "manifests"


def test_manifests_base_subhour_is_namespaced():
    root = Path("/tmp/repo")
    assert artifacts._manifests_base(root, "30m") == root / "research" / "manifests" / "30m"


def test_list_strategy_manifests_reads_namespaced(tmp_path):
    _write_manifest(tmp_path / "research" / "manifests", "eth_1h")
    _write_manifest(tmp_path / "research" / "manifests" / "30m", "eth_30m")
    root_list = artifacts.list_strategy_manifests(tmp_path, "1H")
    sub_list = artifacts.list_strategy_manifests(tmp_path, "30m")
    assert [m.strategy_id for m in root_list] == ["eth_1h"]
    assert [m.strategy_id for m in sub_list] == ["eth_30m"]


def test_list_strategy_manifests_missing_interval_returns_empty(tmp_path):
    (tmp_path / "research" / "manifests").mkdir(parents=True)
    assert artifacts.list_strategy_manifests(tmp_path, "15m") == []


def test_discover_intervals_root_only(tmp_path):
    _write_manifest(tmp_path / "research" / "manifests", "eth_1h")
    assert artifacts.discover_intervals(tmp_path) == ["1H"]


def test_discover_intervals_includes_subhour(tmp_path):
    _write_manifest(tmp_path / "research" / "manifests", "eth_1h")
    _write_manifest(tmp_path / "research" / "manifests" / "30m", "eth_30m")
    _write_manifest(tmp_path / "research" / "manifests" / "15m", "eth_15m")
    assert artifacts.discover_intervals(tmp_path) == ["1H", "15m", "30m"]


def test_discover_intervals_empty_defaults_1H(tmp_path):
    assert artifacts.discover_intervals(tmp_path) == ["1H"]


from fastapi.testclient import TestClient

import main


def test_api_strategies_passes_interval(monkeypatch):
    seen = {}

    def fake_list(repo_root, interval="1H"):
        seen["interval"] = interval
        return []

    monkeypatch.setattr(main.artifacts, "list_strategy_manifests", fake_list)
    client = TestClient(main.app)
    client.get("/api/strategies?interval=15m")
    assert seen["interval"] == "15m"


def test_api_strategies_all_merges_and_tags(monkeypatch):
    monkeypatch.setattr(main.artifacts, "discover_intervals", lambda r: ["1H", "30m"])

    class M:
        def __init__(self, sid):
            self.strategy_id = sid
            self.symbol = "ETH-USDT-SWAP"
            self.pipeline_stage = 5
            from datetime import datetime, timezone
            self.generated_at = datetime(2026, 6, 15, tzinfo=timezone.utc)
            self.gate = None
            self.backtest = None
            self.diagnosis = None

    monkeypatch.setattr(main.artifacts, "list_strategy_manifests",
                        lambda r, interval="1H": [M(f"s_{interval}")])
    client = TestClient(main.app)
    rows = client.get("/api/strategies?interval=all").json()
    by_iv = {r["interval"]: r["strategy_id"] for r in rows}
    assert by_iv == {"1H": "s_1H", "30m": "s_30m"}
    # not running (no testnet status) → running_mode tagged null on every row
    assert all(r["running_mode"] is None for r in rows)


def test_api_intervals_endpoint(monkeypatch):
    monkeypatch.setattr(main.artifacts, "discover_intervals", lambda r: ["1H", "15m"])
    client = TestClient(main.app)
    assert client.get("/api/intervals").json() == ["1H", "15m"]
