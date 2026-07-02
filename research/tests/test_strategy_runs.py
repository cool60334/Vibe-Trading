"""
Tests for research/pipeline/strategy_runs.py

TDD: these tests were written BEFORE the implementation.

Runs against:
  - The real research/strategy_runs.json  (integration smoke test)
  - In-memory fixture dicts               (unit tests; no disk I/O beyond tmp)

Run from the research/ directory:
    cd research && python -m pytest tests/test_strategy_runs.py -v
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from pipeline.strategy_runs import (
    StrategyRunsEntry,
    StrategyRunsMap,
    load_strategy_runs,
    register_strategy,
    update_stress_runs,
    update_sweep_run,
    update_intrabar_audit_runs,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def write_json(tmp_path: Path, data: object, name: str = "strategy_runs.json") -> Path:
    """Write JSON fixture to a temp file and return its path."""
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ─── Minimal valid fixture ────────────────────────────────────────────────────

MINIMAL_VALID_ENTRY = {
    "symbol": "BTC-USDT-SWAP",
    "spec_yaml": "research/strategies/strategy_S1.yaml",
    "base_run": "btc_s1_base",
    "regime_runs": {"bull": "btc_s1_bull", "bear": "btc_s1_bear", "neutral": "btc_s1_neutral"},
    "stress_runs": {"3x_fees": "btc_s1_base_stress"},
    "sweep_run": "btc_s1_sweep",
}

MINIMAL_VALID_MAP = {"btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY}


# ─── Integration: real strategy_runs.json ────────────────────────────────────

class TestRealStrategyRuns:
    """Load the actual research/strategy_runs.json that ships with the repo."""

    def test_loads_without_error(self) -> None:
        result = load_strategy_runs()
        assert isinstance(result, StrategyRunsMap)

    def test_has_strategies(self) -> None:
        # The live strategy_runs.json evolves as strategies are added/retired, so
        # assert structure rather than an exact count (the old ==4 assertion was
        # brittle and coupled to a specific snapshot).
        result = load_strategy_runs()
        assert len(result.entries) >= 1, "strategy_runs.json should have at least one strategy"
        for strategy_id, entry in result.entries.items():
            assert isinstance(strategy_id, str) and strategy_id
            assert isinstance(entry, StrategyRunsEntry)
            assert entry.symbol

    def test_all_strategy_ids_have_coin_prefix(self) -> None:
        result = load_strategy_runs()
        for sid in result.entries:
            coin = sid.split("_", 1)[0]
            assert coin.isalpha() and coin.islower() and "_" in sid, (
                f"strategy_id '{sid}' must start with a lowercase coin prefix "
                f"like 'btc_'/'eth_'/'sol_'"
            )

    def test_all_entries_are_strategy_runs_entry(self) -> None:
        result = load_strategy_runs()
        for sid, entry in result.entries.items():
            assert isinstance(entry, StrategyRunsEntry), (
                f"Entry for '{sid}' must be a StrategyRunsEntry"
            )

    def test_all_entries_have_usdt_swap_symbol(self) -> None:
        result = load_strategy_runs()
        for sid, entry in result.entries.items():
            assert entry.symbol.endswith("-USDT-SWAP") and entry.symbol.isupper(), (
                f"Entry '{sid}' symbol must be a <COIN>-USDT-SWAP ticker, "
                f"got '{entry.symbol}'"
            )

    def test_spec_yaml_paths_point_to_existing_files(self) -> None:
        result = load_strategy_runs()
        # Resolve relative to repo root (two parents above pipeline/)
        repo_root = Path(__file__).resolve().parents[2]
        for sid, entry in result.entries.items():
            spec_path = repo_root / entry.spec_yaml
            assert spec_path.exists(), (
                f"Entry '{sid}': spec_yaml '{entry.spec_yaml}' does not exist at {spec_path}"
            )

    def test_regime_runs_has_expected_keys(self) -> None:
        result = load_strategy_runs()
        for sid, entry in result.entries.items():
            assert isinstance(entry.regime_runs, types.MappingProxyType), (
                f"Entry '{sid}' regime_runs must be a MappingProxyType"
            )

    def test_stress_runs_has_entries(self) -> None:
        result = load_strategy_runs()
        for sid, entry in result.entries.items():
            assert isinstance(entry.stress_runs, types.MappingProxyType), (
                f"Entry '{sid}' stress_runs must be a MappingProxyType"
            )

    def test_sweep_run_is_str_or_none(self) -> None:
        result = load_strategy_runs()
        for sid, entry in result.entries.items():
            assert entry.sweep_run is None or isinstance(entry.sweep_run, str), (
                f"Entry '{sid}' sweep_run must be str or null"
            )

    def test_run_names_carry_coin_prefix(self) -> None:
        """All non-null run names must start with their entry's coin prefix."""
        result = load_strategy_runs()
        for sid, entry in result.entries.items():
            prefix = sid.split("_", 1)[0] + "_"  # e.g. "btc_", "eth_", "sol_"
            for run_name in [entry.base_run, entry.sweep_run]:
                if run_name is not None:
                    assert run_name.startswith(prefix), (
                        f"Entry '{sid}': run name '{run_name}' must start with '{prefix}'"
                    )
            for label, run_name in entry.regime_runs.items():
                assert run_name.startswith(prefix), (
                    f"Entry '{sid}': regime_runs['{label}'] = '{run_name}' must start with '{prefix}'"
                )
            for label, run_name in entry.stress_runs.items():
                assert run_name.startswith(prefix), (
                    f"Entry '{sid}': stress_runs['{label}'] = '{run_name}' must start with '{prefix}'"
                )


# ─── Unit: valid fixture load ─────────────────────────────────────────────────

class TestValidFixture:
    def test_loads_minimal_valid_map(self, tmp_path: Path) -> None:
        p = write_json(tmp_path, MINIMAL_VALID_MAP)
        result = load_strategy_runs(p)
        assert isinstance(result, StrategyRunsMap)

    def test_entry_fields_parsed_correctly(self, tmp_path: Path) -> None:
        p = write_json(tmp_path, MINIMAL_VALID_MAP)
        result = load_strategy_runs(p)
        entry = result.entries["btc_s1_multifactor_contrarian"]
        assert entry.symbol == "BTC-USDT-SWAP"
        assert entry.spec_yaml == "research/strategies/strategy_S1.yaml"
        assert entry.base_run == "btc_s1_base"
        assert entry.regime_runs == {"bull": "btc_s1_bull", "bear": "btc_s1_bear", "neutral": "btc_s1_neutral"}
        assert entry.stress_runs == {"3x_fees": "btc_s1_base_stress"}
        assert entry.sweep_run == "btc_s1_sweep"

    def test_sweep_run_null_is_allowed(self, tmp_path: Path) -> None:
        data = {
            "btc_s1_test": {
                **MINIMAL_VALID_ENTRY,
                "sweep_run": None,
            }
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert result.entries["btc_s1_test"].sweep_run is None

    def test_empty_stress_runs_allowed(self, tmp_path: Path) -> None:
        data = {
            "btc_s1_test": {
                **MINIMAL_VALID_ENTRY,
                "stress_runs": {},
            }
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert result.entries["btc_s1_test"].stress_runs == {}

    def test_oos_runs_field_loaded_from_json(self, tmp_path: Path) -> None:
        """oos_runs is a real field (emit_manifest reads it, taking precedence over
        walk_forward_runs): an empty list loads to an empty tuple, a populated list
        loads into the field. (Re-added in 3957a6b after the B4 removal in 7ee12c0.)"""
        data = {
            "btc_s1_empty": {**MINIMAL_VALID_ENTRY, "oos_runs": []},
            "btc_s1_filled": {**MINIMAL_VALID_ENTRY, "oos_runs": ["btc_s1_oos_2024"]},
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert result.entries["btc_s1_empty"].oos_runs == ()
        assert result.entries["btc_s1_filled"].oos_runs == ("btc_s1_oos_2024",)

    def test_multiple_entries_parsed(self, tmp_path: Path) -> None:
        entry2 = {**MINIMAL_VALID_ENTRY, "spec_yaml": "research/strategies/strategy_S2.yaml"}
        data = {
            "btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY,
            "btc_s2_funding_mean_reversion": entry2,
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert len(result.entries) == 2
        assert "btc_s1_multifactor_contrarian" in result.entries
        assert "btc_s2_funding_mean_reversion" in result.entries

    def test_returns_immutable_entries(self, tmp_path: Path) -> None:
        """All mutable containers must be genuinely immutable after loading."""
        p = write_json(tmp_path, MINIMAL_VALID_MAP)
        result = load_strategy_runs(p)
        entry = result.entries["btc_s1_multifactor_contrarian"]

        # Frozen dataclass field — cannot reassign
        with pytest.raises((AttributeError, TypeError)):
            entry.symbol = "MODIFIED"  # type: ignore[misc]

        # regime_runs is a MappingProxyType — cannot set items
        with pytest.raises(TypeError):
            entry.regime_runs["x"] = "y"  # type: ignore[index]

        # stress_runs is a MappingProxyType — cannot set items
        with pytest.raises(TypeError):
            entry.stress_runs["x"] = "y"  # type: ignore[index]

        # entries top-level mapping is a MappingProxyType — cannot set items
        with pytest.raises(TypeError):
            result.entries["fake"] = entry  # type: ignore[index]


# ─── Unit: missing required key → clear error naming strategy_id + field ─────

class TestMissingRequiredKey:
    @pytest.mark.parametrize("missing_key", [
        "symbol",
        "spec_yaml",
        "base_run",
        "regime_runs",
        "stress_runs",
        "sweep_run",
    ])
    def test_raises_key_error_naming_strategy_id_and_field(
        self, tmp_path: Path, missing_key: str
    ) -> None:
        entry = {**MINIMAL_VALID_ENTRY}
        del entry[missing_key]
        data = {"btc_s1_test": entry}
        p = write_json(tmp_path, data)

        with pytest.raises(KeyError) as exc_info:
            load_strategy_runs(p)

        msg = str(exc_info.value)
        assert "btc_s1_test" in msg, (
            f"Error message must name the offending strategy_id 'btc_s1_test', got: {msg}"
        )
        assert missing_key in msg, (
            f"Error message must name the missing field '{missing_key}', got: {msg}"
        )


# ─── Unit: wrong type → clear error naming strategy_id + field ───────────────

class TestWrongType:
    def test_symbol_not_string_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": {**MINIMAL_VALID_ENTRY, "symbol": 123}}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "symbol" in msg

    def test_spec_yaml_not_string_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": {**MINIMAL_VALID_ENTRY, "spec_yaml": 42}}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "spec_yaml" in msg

    def test_base_run_not_string_or_null_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": {**MINIMAL_VALID_ENTRY, "base_run": 99}}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "base_run" in msg

    def test_regime_runs_not_dict_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": {**MINIMAL_VALID_ENTRY, "regime_runs": ["bull"]}}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "regime_runs" in msg

    def test_stress_runs_not_dict_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": {**MINIMAL_VALID_ENTRY, "stress_runs": "3x"}}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "stress_runs" in msg

    def test_sweep_run_not_string_or_null_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": {**MINIMAL_VALID_ENTRY, "sweep_run": 7}}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "sweep_run" in msg

    def test_root_not_dict_raises_type_error(self, tmp_path: Path) -> None:
        p = write_json(tmp_path, ["btc_s1_test"])
        with pytest.raises(TypeError, match="mapping"):
            load_strategy_runs(p)

    def test_entry_not_dict_raises_type_error(self, tmp_path: Path) -> None:
        data = {"btc_s1_test": "bad_entry"}
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg

    def test_regime_runs_value_not_string_raises_type_error(self, tmp_path: Path) -> None:
        """regime_runs dict values must be strings."""
        data = {
            "btc_s1_test": {
                **MINIMAL_VALID_ENTRY,
                "regime_runs": {"bull": 123, "bear": "btc_s1_bear", "neutral": "btc_s1_neutral"},
            }
        }
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "regime_runs" in msg

    def test_stress_runs_value_not_string_raises_type_error(self, tmp_path: Path) -> None:
        """stress_runs dict values must be strings — mirrors regime_runs coverage."""
        data = {
            "btc_s1_test": {
                **MINIMAL_VALID_ENTRY,
                "stress_runs": {"3x_fees": 42},
            }
        }
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "btc_s1_test" in msg
        assert "stress_runs" in msg


# ─── Unit: file not found ─────────────────────────────────────────────────────

class TestFileNotFound:
    def test_raises_file_not_found_for_missing_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError, match="strategy_runs.json"):
            load_strategy_runs(missing)


# ─── Unit: multi-symbol prefix parsing ───────────────────────────────────────

class TestMultiSymbolPrefixes:
    def test_btc_and_eth_entries_coexist(self, tmp_path: Path) -> None:
        """Multi-symbol: btc_ and eth_ prefixed strategy_ids must both load cleanly."""
        eth_entry = {
            **MINIMAL_VALID_ENTRY,
            "symbol": "ETH-USDT-SWAP",
            "spec_yaml": "research/strategies/strategy_S1.yaml",
            "base_run": "eth_s1_base",
            "regime_runs": {"bull": "eth_s1_bull", "bear": "eth_s1_bear", "neutral": "eth_s1_neutral"},
            "stress_runs": {"3x_fees": "eth_s1_base_stress"},
            "sweep_run": "eth_s1_sweep",
        }
        data = {
            "btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY,
            "eth_s1_multifactor_contrarian": eth_entry,
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert len(result.entries) == 2
        assert result.entries["btc_s1_multifactor_contrarian"].symbol == "BTC-USDT-SWAP"
        assert result.entries["eth_s1_multifactor_contrarian"].symbol == "ETH-USDT-SWAP"

    def test_strategy_ids_are_dict_keys_verbatim(self, tmp_path: Path) -> None:
        """strategy_id is just the JSON key — loader does not transform it."""
        data = {"eth_s2_funding_mean_reversion": MINIMAL_VALID_ENTRY}
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert "eth_s2_funding_mean_reversion" in result.entries


# ─── Unit: _comment skip is exact-match only ─────────────────────────────────

class TestCommentSkip:
    def test_comment_key_is_skipped(self, tmp_path: Path) -> None:
        """The exact key '_comment' is silently skipped."""
        data = {
            "_comment": "This file maps strategy IDs to run directories.",
            "btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY,
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert "_comment" not in result.entries
        assert len(result.entries) == 1

    def test_underscore_prefixed_strategy_id_is_not_dropped(self, tmp_path: Path) -> None:
        """A strategy_id like '_archived' must NOT be silently discarded.
        It should be loaded as a normal entry (since its structure is valid)."""
        data = {
            "_archived_strategy": MINIMAL_VALID_ENTRY,
            "btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY,
        }
        p = write_json(tmp_path, data)
        result = load_strategy_runs(p)
        assert "_archived_strategy" in result.entries, (
            "strategy_id '_archived_strategy' must be loaded, not silently dropped"
        )
        assert len(result.entries) == 2

    def test_underscore_prefixed_with_invalid_structure_raises(self, tmp_path: Path) -> None:
        """A strategy_id like '_archived' with bad structure must raise, not silently vanish."""
        data = {
            "_archived_strategy": "not_a_dict",
        }
        p = write_json(tmp_path, data)
        with pytest.raises(TypeError) as exc_info:
            load_strategy_runs(p)
        msg = str(exc_info.value)
        assert "_archived_strategy" in msg


# ─── update_sweep_run writer tests ───────────────────────────────────────────


class TestUpdateSweepRun:
    """Writer that stage 4 calls after a successful grid sweep."""

    def _bootstrap(self, tmp_path: Path) -> Path:
        """Write a minimal valid strategy_runs.json fixture and return its path."""
        return write_json(tmp_path, dict(MINIMAL_VALID_MAP))

    def test_updates_sweep_run_to_new_name(self, tmp_path: Path) -> None:
        p = self._bootstrap(tmp_path)
        update_sweep_run("btc_s1_multifactor_contrarian", "new_sweep_033", path=p)
        loaded = load_strategy_runs(p)
        assert loaded.entries["btc_s1_multifactor_contrarian"].sweep_run == "new_sweep_033"

    def test_clears_sweep_run_when_none(self, tmp_path: Path) -> None:
        p = self._bootstrap(tmp_path)
        update_sweep_run("btc_s1_multifactor_contrarian", None, path=p)
        loaded = load_strategy_runs(p)
        assert loaded.entries["btc_s1_multifactor_contrarian"].sweep_run is None

    def test_unknown_strategy_id_raises_keyerror(self, tmp_path: Path) -> None:
        p = self._bootstrap(tmp_path)
        with pytest.raises(KeyError):
            update_sweep_run("nonexistent_strategy", "x", path=p)

    def test_comment_key_treated_as_unknown(self, tmp_path: Path) -> None:
        p = self._bootstrap(tmp_path)
        with pytest.raises(KeyError):
            update_sweep_run("_comment", "x", path=p)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.json"
        with pytest.raises(FileNotFoundError):
            update_sweep_run("any", "x", path=missing)

    def test_non_string_sweep_run_raises_typeerror(self, tmp_path: Path) -> None:
        p = self._bootstrap(tmp_path)
        with pytest.raises(TypeError):
            update_sweep_run("btc_s1_multifactor_contrarian", 123, path=p)  # type: ignore[arg-type]

    def test_preserves_other_fields(self, tmp_path: Path) -> None:
        """Writer must not corrupt unrelated fields on the same entry."""
        p = self._bootstrap(tmp_path)
        update_sweep_run("btc_s1_multifactor_contrarian", "new_name", path=p)
        loaded = load_strategy_runs(p)
        entry = loaded.entries["btc_s1_multifactor_contrarian"]
        assert entry.symbol == MINIMAL_VALID_ENTRY["symbol"]
        assert entry.base_run == MINIMAL_VALID_ENTRY["base_run"]
        assert dict(entry.regime_runs) == MINIMAL_VALID_ENTRY["regime_runs"]

    def test_preserves_other_strategies(self, tmp_path: Path) -> None:
        """Writing one strategy's sweep_run must leave siblings untouched."""
        data = {
            "btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY,
            "btc_s2_other": {**MINIMAL_VALID_ENTRY, "sweep_run": "keep_me"},
        }
        p = write_json(tmp_path, data)
        update_sweep_run("btc_s1_multifactor_contrarian", "fresh", path=p)
        loaded = load_strategy_runs(p)
        assert loaded.entries["btc_s1_multifactor_contrarian"].sweep_run == "fresh"
        assert loaded.entries["btc_s2_other"].sweep_run == "keep_me"

    def test_preserves_comment_key(self, tmp_path: Path) -> None:
        data = {
            "_comment": "this is metadata, do not delete",
            "btc_s1_multifactor_contrarian": MINIMAL_VALID_ENTRY,
        }
        p = write_json(tmp_path, data)
        update_sweep_run("btc_s1_multifactor_contrarian", "renamed", path=p)
        # Re-parse raw to confirm _comment survived (load_strategy_runs strips it).
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw.get("_comment") == "this is metadata, do not delete"


# ─── register_strategy() tests ───────────────────────────────────────────────


class TestRegisterStrategy:
    """register_strategy() adds new entries to strategy_runs.json, idempotently."""

    def _empty_file(self, tmp_path: Path) -> Path:
        """Write an empty JSON object and return its path."""
        p = tmp_path / "strategy_runs.json"
        p.write_text("{}", encoding="utf-8")
        return p

    def _file_with_entry(self, tmp_path: Path) -> Path:
        """Write a file with one existing entry and return its path."""
        return write_json(tmp_path, dict(MINIMAL_VALID_MAP))

    # 1. Register new entry adds the key with all required fields ──────────────

    def test_register_new_entry_creates_key(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "btc_s10_single_factor" in raw

    def test_register_new_entry_has_all_required_keys(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        entry = raw["btc_s10_single_factor"]
        required_keys = {"symbol", "spec_yaml", "base_run", "regime_runs",
                         "stress_runs", "sweep_run", "walk_forward_runs"}
        missing = required_keys - entry.keys()
        assert not missing, f"Entry is missing required keys: {missing}"

    def test_register_new_entry_field_defaults(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        entry = raw["btc_s10_single_factor"]
        assert entry["symbol"] == "BTC-USDT-SWAP"
        assert entry["spec_yaml"] == "research/strategies/strategy_btc_s10_single_factor.yaml"
        assert entry["base_run"] == "btc_s10_single_factor_base"
        assert entry["regime_runs"] == {}
        assert entry["stress_runs"] == {}
        assert entry["sweep_run"] is None
        assert entry["walk_forward_runs"] == []

    # 2. Idempotent — second call with same id is a no-op ─────────────────────

    def test_idempotent_second_call_no_duplicate(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        for _ in range(2):
            register_strategy(
                "btc_s10_single_factor",
                "BTC-USDT-SWAP",
                "research/strategies/strategy_btc_s10_single_factor.yaml",
                path=p,
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        # Key appears exactly once (JSON object keys are unique)
        assert list(raw.keys()).count("btc_s10_single_factor") == 1

    def test_idempotent_second_call_does_not_change_entry(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        first_raw = json.loads(p.read_text(encoding="utf-8"))

        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        second_raw = json.loads(p.read_text(encoding="utf-8"))
        assert first_raw == second_raw

    # 3. Additive — does not touch existing entries ───────────────────────────

    def test_additive_existing_entry_not_modified(self, tmp_path: Path) -> None:
        p = self._file_with_entry(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        # Original entry should be completely unchanged
        assert raw["btc_s1_multifactor_contrarian"] == MINIMAL_VALID_ENTRY

    def test_additive_new_entry_added_alongside_existing(self, tmp_path: Path) -> None:
        p = self._file_with_entry(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "btc_s1_multifactor_contrarian" in raw
        assert "btc_s10_single_factor" in raw

    # 4. Preserves existing data — does not overwrite set fields ──────────────

    def test_preserves_existing_base_run_when_already_set(self, tmp_path: Path) -> None:
        """If an entry already exists with base_run set, re-calling must NOT reset it."""
        p = self._empty_file(tmp_path)
        # First call creates the entry with default base_run
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        # Manually set base_run to a known value (simulating a hand-written edit)
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["btc_s10_single_factor"]["base_run"] = "btc_s10_base"
        p.write_text(json.dumps(raw, indent=2), encoding="utf-8")

        # Second call should NOT overwrite base_run
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw2 = json.loads(p.read_text(encoding="utf-8"))
        assert raw2["btc_s10_single_factor"]["base_run"] == "btc_s10_base"

    # 5. Multiple strategies — 3 distinct entries ─────────────────────────────

    def test_multiple_strategies_registered_independently(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        ids = [
            "btc_s10_single_factor",
            "btc_s11_trend_with_gate",
            "btc_s12_consensus_all",
        ]
        for sid in ids:
            register_strategy(
                sid,
                "BTC-USDT-SWAP",
                f"research/strategies/strategy_{sid}.yaml",
                path=p,
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert len(raw) == 3
        for sid in ids:
            assert sid in raw

    # 6. Generated run names follow convention ────────────────────────────────

    def test_base_run_is_strategy_id_plus_base_suffix(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["btc_s10_single_factor"]["base_run"] == "btc_s10_single_factor_base"

    def test_sweep_run_is_null_by_default(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["btc_s10_single_factor"]["sweep_run"] is None

    def test_regime_runs_empty_dict_by_default(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["btc_s10_single_factor"]["regime_runs"] == {}

    def test_walk_forward_runs_empty_list_by_default(self, tmp_path: Path) -> None:
        p = self._empty_file(tmp_path)
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["btc_s10_single_factor"]["walk_forward_runs"] == []

    # 7. Works when file does not exist yet (creates it) ──────────────────────

    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "strategy_runs_new.json"
        assert not p.exists()
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        assert p.exists()
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "btc_s10_single_factor" in raw

    def test_created_file_is_loadable_by_load_strategy_runs(self, tmp_path: Path) -> None:
        """File created by register_strategy() must pass load_strategy_runs() validation."""
        p = tmp_path / "strategy_runs_new.json"
        register_strategy(
            "btc_s10_single_factor",
            "BTC-USDT-SWAP",
            "research/strategies/strategy_btc_s10_single_factor.yaml",
            path=p,
        )
        result = load_strategy_runs(p)
        assert "btc_s10_single_factor" in result.entries
        entry = result.entries["btc_s10_single_factor"]
        assert entry.base_run == "btc_s10_single_factor_base"
        assert entry.sweep_run is None
        assert entry.regime_runs == {}
        assert entry.walk_forward_runs == ()


# ─── Unit: RESEARCH_ONLY_SYMBOL env-var filtering ────────────────────────────

def test_load_strategy_runs_filters_to_env_symbol(tmp_path, monkeypatch):
    payload = {
        "btc_s1_x": {"symbol": "BTC-USDT-SWAP", "spec_yaml": "research/strategies/strategy_S1.yaml",
                      "base_run": "btc_s1_x_base", "regime_runs": {}, "stress_runs": {},
                      "sweep_run": None, "walk_forward_runs": []},
        "eth_s1_y": {"symbol": "ETH-USDT-SWAP", "spec_yaml": "research/strategies/strategy_S1.yaml",
                      "base_run": "eth_s1_y_base", "regime_runs": {}, "stress_runs": {},
                      "sweep_run": None, "walk_forward_runs": []},
    }
    p = tmp_path / "strategy_runs.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("RESEARCH_ONLY_SYMBOL", "btc")
    result = load_strategy_runs(p)
    assert list(result.entries.keys()) == ["btc_s1_x"]

    monkeypatch.delenv("RESEARCH_ONLY_SYMBOL", raising=False)
    result_all = load_strategy_runs(p)
    assert set(result_all.entries.keys()) == {"btc_s1_x", "eth_s1_y"}


# ─── Unit: update_stress_runs writer tests ──────────────────────────────────


def test_update_stress_runs_writes_mapping(tmp_path):
    payload = {
        "btc_s9": {"symbol": "BTC-USDT-SWAP", "spec_yaml": "research/strategies/strategy_S1.yaml",
                    "base_run": "btc_s9_base", "regime_runs": {}, "stress_runs": {},
                    "sweep_run": None, "walk_forward_runs": []},
    }
    p = tmp_path / "strategy_runs.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    update_stress_runs("btc_s9", {"2x_fees_train": "btc_s9_stress_train_2x",
                                   "3x_fees_oos": "btc_s9_stress_oos_3x"}, path=p)

    entry = json.loads(p.read_text())["btc_s9"]
    assert entry["stress_runs"] == {
        "2x_fees_train": "btc_s9_stress_train_2x",
        "3x_fees_oos": "btc_s9_stress_oos_3x",
    }


def test_update_stress_runs_unknown_strategy_raises(tmp_path):
    p = tmp_path / "strategy_runs.json"
    p.write_text("{}", encoding="utf-8")
    import pytest
    with pytest.raises(KeyError):
        update_stress_runs("missing", {"2x_fees_train": "x"}, path=p)


# ─── Unit: update_intrabar_audit_runs writer tests ─────────────────────────────


def test_update_intrabar_audit_runs_writes_mapping(tmp_path):
    from pipeline.strategy_runs import update_intrabar_audit_runs

    payload = {
        "eth_s5": {
            "symbol": "ETH-USDT-SWAP",
            "spec_yaml": "research/strategies/strategy_S1.yaml",
            "base_run": "eth_s5_base",
            "regime_runs": {},
            "stress_runs": {},
            "sweep_run": None,
            "walk_forward_runs": [],
        }
    }
    p = tmp_path / "strategy_runs.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    update_intrabar_audit_runs("eth_s5", {"train": "eth_s5_base", "oos": "eth_s5_oos"}, path=p)

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["eth_s5"]["intrabar_audit_runs"] == {"train": "eth_s5_base", "oos": "eth_s5_oos"}
    # and it parses back onto the dataclass
    m = load_strategy_runs(path=p)
    assert m.entries["eth_s5"].intrabar_audit_runs["oos"] == "eth_s5_oos"
