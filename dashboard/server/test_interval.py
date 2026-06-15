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
