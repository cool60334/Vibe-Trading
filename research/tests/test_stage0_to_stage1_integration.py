"""
Integration test: stage0 → stage1 handoff contract.

Tests the full handoff from stage-0's _process_symbol() writing a
candidates_<sym>.json to stage-1's _load_candidates() reading it back.

Stage 0 is now *deterministic*: candidates are selected from the stage-0a
evidence manifest by IC/IR threshold (select_candidates_from_evidence), NOT
parsed out of swarm stdout. The swarm is optional and only enriches each
candidate's economic_logic string; its failures are fail-soft (the
deterministic placeholder is kept). There is no longer any
``candidates_<sym>.failed.json`` marker — a zero-candidate result writes a
valid empty manifest and exits 0.

Pytest is run from research/ as:
    cd research && python -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Bootstrap: research/ and dashboard/server/ must be on sys.path.
_RESEARCH_DIR = Path(__file__).resolve().parents[1]  # research/
_REPO_ROOT = _RESEARCH_DIR.parent
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"

for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from factor_extended import _load_candidates
from pipeline.stage0_discovery import _process_symbol
from pipeline.lib.factor_selector import DEFAULT_MIN_ABS_IC, DEFAULT_MIN_ABS_IR
from schemas import CandidatesManifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_research_config():
    """Return a minimal ResearchConfig with all required fields."""
    from pipeline.config import FeesConfig, ResearchConfig, SymbolConfig
    return ResearchConfig(
        symbols=(SymbolConfig(name="eth", okx_swap="ETH-USDT-SWAP", ccxt_bybit="ETHUSDT"),),
        period=30,
        interval="1H",
        data_source="okx",
        engine="daily",
        fees=FeesConfig(maker_rate=0.0002, taker_rate=0.0005, slippage=0.0001),
        horizons_h=(8, 24),
        discovery_cache_days=0,  # disable cache so _process_symbol always runs
    )


def _process_eth(tmp_path: Path, *, use_swarm: bool = False):
    """Run _process_symbol for eth with the standard deterministic kwargs."""
    return _process_symbol(
        sym_name="eth",
        okx_swap="ETH-USDT-SWAP",
        cfg=_make_research_config(),
        manifests_dir=tmp_path,
        use_swarm=use_swarm,
        min_abs_ic=DEFAULT_MIN_ABS_IC,
        min_abs_ir=DEFAULT_MIN_ABS_IR,
        max_candidates=6,
    )


def _write_stage0a_outputs(
    manifests_dir: Path,
    sym: str = "eth",
    *,
    evidence_entries: list[dict] | None = None,
    feature_cols: list[str] | None = None,
) -> None:
    """Write minimal features parquet + evidence JSON to satisfy stage-0 preflight.

    By default writes two PASSING factors (funding_z_30d +IC, stablecoin_supply_z
    -IC) plus two that the selector must DROP (weak IC, and a feature_key that is
    absent from the feature store).
    """
    import numpy as np
    import pandas as pd
    from lib.factor_io import dump_features, dump_evidence

    if evidence_entries is None:
        evidence_entries = [
            # PASS: |IC| 0.08 >= 0.05, |IR| 0.15 >= 0.10
            {"feature_key": "funding_z_30d", "category": "funding",
             "ic_by_horizon": {"8": 0.08, "24": 0.04}, "ir": 0.15, "sample_size": 5000},
            # PASS: |IC| 0.07, |IR| 0.20, negative sign
            {"feature_key": "stablecoin_supply_z", "category": "stablecoin",
             "ic_by_horizon": {"8": -0.07, "24": -0.02}, "ir": 0.20, "sample_size": 5000},
            # DROP: |IC| 0.02 < 0.05
            {"feature_key": "weak_oi", "category": "oi",
             "ic_by_horizon": {"8": 0.02}, "ir": 0.30, "sample_size": 5000},
            # DROP: strong IC but feature_key not in feature store columns
            {"feature_key": "ghost_factor", "category": "momentum",
             "ic_by_horizon": {"8": 0.20}, "ir": 0.50, "sample_size": 5000},
        ]

    if feature_cols is None:
        # ghost_factor deliberately omitted so the selector drops it.
        feature_cols = ["funding_z_30d", "stablecoin_supply_z", "weak_oi"]

    n = 100
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    features = {col: pd.Series(np.random.randn(n), index=idx) for col in feature_cols}
    dump_features(sym, features, manifests_dir)

    evidence_payload = {
        "caveat": "test only",
        "symbol": sym,
        "generated_at": "2024-01-01T00:00:00+00:00",
        "evidence": evidence_entries,
    }
    dump_evidence(sym, evidence_payload, manifests_dir)


# ---------------------------------------------------------------------------
# Deterministic handoff
# ---------------------------------------------------------------------------


class TestStage0ToStage1Handoff:
    """Stage-0 deterministic selection writes a manifest stage-1 can load."""

    def test_process_symbol_writes_valid_manifest(self, tmp_path: Path):
        _write_stage0a_outputs(tmp_path)
        result = _process_eth(tmp_path)

        assert result.ok, f"Expected ok=True but got: {result}"
        assert result.symbol == "eth"
        assert result.n_candidates == 2  # two factors pass IC/IR thresholds

        out_path = tmp_path / "candidates_eth.json"
        assert out_path.exists(), "candidates_eth.json should have been written"

    def test_written_manifest_is_valid_candidates_manifest(self, tmp_path: Path):
        _write_stage0a_outputs(tmp_path)
        _process_eth(tmp_path)

        out_path = tmp_path / "candidates_eth.json"
        manifest = CandidatesManifest.model_validate_json(
            out_path.read_text(encoding="utf-8")
        )
        assert isinstance(manifest, CandidatesManifest)
        assert manifest.symbol == "eth"
        assert len(manifest.candidates) == 2

    def test_only_threshold_passing_factors_are_kept(self, tmp_path: Path):
        """weak_oi (low IC) and ghost_factor (not in feature store) are dropped."""
        _write_stage0a_outputs(tmp_path)
        _process_eth(tmp_path)

        manifest = CandidatesManifest.model_validate_json(
            (tmp_path / "candidates_eth.json").read_text(encoding="utf-8")
        )
        names = {c.name for c in manifest.candidates}
        assert names == {"funding_z_30d", "stablecoin_supply_z"}
        assert "weak_oi" not in names
        assert "ghost_factor" not in names

    def test_expected_ic_sign_follows_measured_ic(self, tmp_path: Path):
        _write_stage0a_outputs(tmp_path)
        _process_eth(tmp_path)

        manifest = CandidatesManifest.model_validate_json(
            (tmp_path / "candidates_eth.json").read_text(encoding="utf-8")
        )
        sign = {c.name: c.expected_ic_sign for c in manifest.candidates}
        assert sign["funding_z_30d"] == "+"
        assert sign["stablecoin_supply_z"] == "-"

    def test_candidates_are_ranked_by_abs_ic_desc(self, tmp_path: Path):
        """Top |IC| factor (funding_z_30d, 0.08) ranks before stablecoin (0.07)."""
        _write_stage0a_outputs(tmp_path)
        _process_eth(tmp_path)

        manifest = CandidatesManifest.model_validate_json(
            (tmp_path / "candidates_eth.json").read_text(encoding="utf-8")
        )
        assert [c.name for c in manifest.candidates] == [
            "funding_z_30d", "stablecoin_supply_z"
        ]

    def test_stage1_load_candidates_reads_stage0_output(self, tmp_path: Path):
        """_load_candidates (stage-1) reads back the manifest written by stage-0."""
        _write_stage0a_outputs(tmp_path)
        _process_eth(tmp_path)

        loaded = _load_candidates(tmp_path, "eth")
        assert loaded is not None
        assert isinstance(loaded, CandidatesManifest)
        assert len(loaded.candidates) == 2

    def test_stage1_gets_correct_candidate_fields(self, tmp_path: Path):
        _write_stage0a_outputs(tmp_path)
        _process_eth(tmp_path)

        loaded = _load_candidates(tmp_path, "eth")
        assert loaded is not None
        cand_map = {c.name: c for c in loaded.candidates}

        funding = cand_map["funding_z_30d"]
        assert funding.feature_key == "funding_z_30d"
        assert funding.category == "funding"
        # Deterministic path identifies factors by feature_key, not the legacy
        # data_source/transform registry pair.
        assert funding.data_source is None
        assert funding.transform is None


# ---------------------------------------------------------------------------
# Zero-candidate behaviour — empty manifest, no .failed.json
# ---------------------------------------------------------------------------


class TestZeroCandidates:
    """When no factor clears the threshold, stage-0 writes a valid EMPTY
    manifest and stays ok — there is no .failed.json marker anymore."""

    def test_empty_manifest_written_and_ok(self, tmp_path: Path):
        # All entries below the IC threshold → 0 candidates.
        _write_stage0a_outputs(
            tmp_path,
            evidence_entries=[
                {"feature_key": "weak_a", "category": "funding",
                 "ic_by_horizon": {"8": 0.01}, "ir": 0.5, "sample_size": 5000},
                {"feature_key": "weak_b", "category": "oi",
                 "ic_by_horizon": {"8": 0.02}, "ir": 0.5, "sample_size": 5000},
            ],
            feature_cols=["weak_a", "weak_b"],
        )
        result = _process_eth(tmp_path)

        assert result.ok  # zero candidates is NOT a failure
        assert result.n_candidates == 0

        out_path = tmp_path / "candidates_eth.json"
        assert out_path.exists()
        manifest = CandidatesManifest.model_validate_json(
            out_path.read_text(encoding="utf-8")
        )
        assert manifest.candidates == []

    def test_no_failed_json_marker_is_written(self, tmp_path: Path):
        _write_stage0a_outputs(
            tmp_path,
            evidence_entries=[
                {"feature_key": "weak_a", "category": "funding",
                 "ic_by_horizon": {"8": 0.01}, "ir": 0.5, "sample_size": 5000},
            ],
            feature_cols=["weak_a"],
        )
        _process_eth(tmp_path)
        assert not (tmp_path / "candidates_eth.failed.json").exists()


# ---------------------------------------------------------------------------
# Preflight + swarm behaviour
# ---------------------------------------------------------------------------


class TestPreflightAndSwarm:
    def test_missing_stage0a_outputs_fails(self, tmp_path: Path):
        """No features/evidence on disk → preflight failure (caller's mistake)."""
        result = _process_eth(tmp_path)
        assert not result.ok
        assert "stage0a" in (result.error or "")
        assert not (tmp_path / "candidates_eth.json").exists()

    def test_deterministic_mode_never_calls_swarm(self, tmp_path: Path):
        """use_swarm=False must not shell out to the swarm subprocess."""
        _write_stage0a_outputs(tmp_path)
        with patch("pipeline.stage0_discovery.subprocess.run") as mock_run:
            result = _process_eth(tmp_path, use_swarm=False)
        assert result.ok
        mock_run.assert_not_called()

    def test_swarm_enrichment_overwrites_economic_logic(self, tmp_path: Path):
        """use_swarm=True enriches economic_logic; selection stays deterministic."""
        _write_stage0a_outputs(tmp_path)

        enrichment_json = (
            "```json\n"
            '[{"feature_key": "funding_z_30d", '
            '"economic_logic": "Crowded longs mean-revert after funding spikes."}]\n'
            "```"
        )
        with patch(
            "pipeline.stage0_discovery.run_swarm", return_value=enrichment_json
        ):
            result = _process_eth(tmp_path, use_swarm=True)

        assert result.ok
        assert result.n_candidates == 2  # selection unaffected by enrichment

        manifest = CandidatesManifest.model_validate_json(
            (tmp_path / "candidates_eth.json").read_text(encoding="utf-8")
        )
        logic = {c.name: c.economic_logic for c in manifest.candidates}
        assert logic["funding_z_30d"] == "Crowded longs mean-revert after funding spikes."
        # The un-enriched factor keeps its deterministic placeholder text.
        assert "deterministic selection" in logic["stablecoin_supply_z"]

    def test_swarm_failure_is_fail_soft(self, tmp_path: Path):
        """A swarm crash must NOT fail the stage — placeholders are kept."""
        _write_stage0a_outputs(tmp_path)
        with patch(
            "pipeline.stage0_discovery.run_swarm",
            side_effect=RuntimeError("swarm exploded"),
        ):
            result = _process_eth(tmp_path, use_swarm=True)

        assert result.ok
        assert result.n_candidates == 2
        manifest = CandidatesManifest.model_validate_json(
            (tmp_path / "candidates_eth.json").read_text(encoding="utf-8")
        )
        for c in manifest.candidates:
            assert "deterministic selection" in c.economic_logic
