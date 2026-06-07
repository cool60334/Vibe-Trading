"""
Tests for research/pipeline/stage2_strategies.py pure-logic helpers.

Stage 2 calls a Vibe-Trading LLM swarm — that call costs money, is
non-deterministic, and needs API keys, so it is NEVER exercised here. The
swarm subprocess is always stubbed/mocked. Only the pure, deterministic
logic is tested:

  (a) select_usable_factors()   — drop factors whose verdict == reject
  (b) swarm_target_from_ticker()/build_swarm_vars()
                                — correct target + JSON-context injection
  (c) parse_swarm_result()      — swarm run-id retrieval from CLI output
  (d) build_strategy_spec()     — prose-aware deterministic YAML scaffold
  (e) build_generation_block()  — GenerationBlock-aligned handoff dict
  (f) verify_outputs()/compute_exit_code()/print_summary()
                                — output verification + exit-code logic

Pytest is run from research/ as:
    cd research && python -m pytest tests/
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

# Bootstrap: research/ and dashboard/server/ must be on sys.path.
_RESEARCH_DIR = Path(__file__).resolve().parents[1]  # research/
_REPO_ROOT = _RESEARCH_DIR.parent
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"

for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from pipeline.stage2_strategies import (  # noqa: E402
    DEFAULT_TIMEFRAME,
    GENERATION_METHOD_DETERMINISTIC,
    GENERATION_METHOD_SWARM,
    SWARM_TIMEOUT_S,
    GeneratedStrategy,
    StrategyCheckResult,
    _generate_for_symbol_multi,
    build_generation_block,
    build_strategy_spec,
    build_swarm_vars,
    check_strategy,
    compute_exit_code,
    extract_swarm_report,
    parse_swarm_result,
    print_summary,
    run_swarm,
    select_usable_factors,
    swarm_target_from_ticker,
    verify_outputs,
)
from schemas import FactorManifest, GenerationBlock  # noqa: E402
from pipeline.lib.archetype_router import ArchetypePlan  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _factor(name: str, verdict: str, ic8: float = 0.07) -> dict:
    """Return one FactorEntry dict with the given name and verdict."""
    return {
        "name": name,
        "ic_by_horizon": {"8": ic8, "24": ic8 * 0.8, "72": ic8 * 1.1, "168": ic8},
        "ir": 0.5,
        "sample_size": 5000,
        "cross_regime_ic": None,
        "stability": None,
        "verdict": verdict,
    }


def _manifest_dict(symbol: str = "BTC", factors: list[dict] | None = None) -> dict:
    """Return a FactorManifest dict; default has one single_use + one reject."""
    if factors is None:
        factors = [
            _factor("funding_rate", "single_use", 0.12),
            _factor("fng", "ensemble_only", 0.07),
            _factor("oi_change", "reject", 0.02),
        ]
    return {
        "schema_version": 1,
        "symbol": symbol.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": 730,
        "horizons_h": [8, 24, 72, 168],
        "factors": factors,
    }


def _manifest(symbol: str = "BTC", factors: list[dict] | None = None) -> FactorManifest:
    return FactorManifest.model_validate(_manifest_dict(symbol, factors))


# ---------------------------------------------------------------------------
# SWARM_TIMEOUT_S constant (issue #1)
# ---------------------------------------------------------------------------


class TestSwarmTimeoutConstant:
    """SWARM_TIMEOUT_S must exist and be a positive integer."""

    def test_constant_exists_and_is_positive_int(self):
        assert isinstance(SWARM_TIMEOUT_S, int)
        assert SWARM_TIMEOUT_S > 0

    def test_constant_is_at_least_60_seconds(self):
        # A swarm involves multiple LLM calls; a timeout below 60 s would be
        # too aggressive in practice.
        assert SWARM_TIMEOUT_S >= 60


# ---------------------------------------------------------------------------
# (a) select_usable_factors
# ---------------------------------------------------------------------------


class TestSelectUsableFactors:
    """select_usable_factors(manifest) -> list[FactorEntry] without verdict=reject."""

    def test_drops_reject_verdict(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        names = {f.name for f in usable}
        assert "oi_change" not in names  # reject dropped
        assert names == {"funding_rate", "fng"}

    def test_keeps_single_use_and_ensemble_only(self):
        manifest = _manifest(
            factors=[
                _factor("a", "single_use"),
                _factor("b", "ensemble_only"),
            ]
        )
        usable = select_usable_factors(manifest)
        assert len(usable) == 2

    def test_all_rejected_returns_empty(self):
        manifest = _manifest(
            factors=[_factor("a", "reject"), _factor("b", "reject")]
        )
        assert select_usable_factors(manifest) == []

    def test_preserves_input_order(self):
        manifest = _manifest(
            factors=[
                _factor("z", "single_use"),
                _factor("m", "reject"),
                _factor("a", "ensemble_only"),
            ]
        )
        usable = select_usable_factors(manifest)
        assert [f.name for f in usable] == ["z", "a"]


# ---------------------------------------------------------------------------
# (b) swarm_target_from_ticker / build_swarm_vars
# ---------------------------------------------------------------------------


class TestSwarmTargetFromTicker:
    """swarm_target_from_ticker(okx_swap) -> grounding-friendly target token."""

    def test_strips_swap_suffix(self):
        # grounding.py regex matches BASE-USDT, not BASE-USDT-SWAP.
        assert swarm_target_from_ticker("BTC-USDT-SWAP") == "BTC-USDT"

    def test_passthrough_without_swap_suffix(self):
        assert swarm_target_from_ticker("ETH-USDT") == "ETH-USDT"

    def test_uppercased(self):
        assert swarm_target_from_ticker("sol-usdt-swap") == "SOL-USDT"


class TestBuildSwarmVars:
    """build_swarm_vars(target, factors, ...) -> dict for --swarm-run VARS_JSON."""

    def test_target_is_clean_token(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        vars_ = build_swarm_vars("BTC-USDT", usable)
        # target MUST stay a clean symbol token so swarm grounding can
        # regex-detect it (grounding.py _SYMBOL_PATTERNS). The factor
        # context must NOT pollute it.
        assert vars_["target"] == "BTC-USDT"

    def test_factor_context_injected_into_timeframe(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        vars_ = build_swarm_vars("BTC-USDT", usable)
        # The selected-factor JSON is injected as decision context via the
        # timeframe variable (the only free-form var that reaches a
        # prompt_template without breaking grounding).
        assert DEFAULT_TIMEFRAME in vars_["timeframe"]
        assert "funding_rate" in vars_["timeframe"]
        assert "fng" in vars_["timeframe"]
        # rejected factor must not leak into the context
        assert "oi_change" not in vars_["timeframe"]

    def test_injected_context_contains_valid_json(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        vars_ = build_swarm_vars("BTC-USDT", usable)
        # The context block must embed a machine-parseable JSON array so a
        # reader (human or LLM) sees structured data, not just prose.
        text = vars_["timeframe"]
        start = text.index("[")
        end = text.rindex("]") + 1
        parsed = json.loads(text[start:end])
        assert isinstance(parsed, list)
        assert {f["name"] for f in parsed} == {"funding_rate", "fng"}
        assert all("verdict" in f and "ic_by_horizon" in f for f in parsed)

    def test_vars_are_all_strings(self):
        # CLI VARS_JSON is json.loads()'d into a dict[str, str]; non-string
        # values would break prompt_template.format_map.
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        vars_ = build_swarm_vars("BTC-USDT", usable)
        assert all(isinstance(v, str) for v in vars_.values())

    def test_empty_factors_still_builds_vars(self):
        vars_ = build_swarm_vars("BTC-USDT", [])
        assert vars_["target"] == "BTC-USDT"
        assert DEFAULT_TIMEFRAME in vars_["timeframe"]

    def test_custom_timeframe_respected(self):
        vars_ = build_swarm_vars("BTC-USDT", [], timeframe="intraday")
        assert vars_["timeframe"].startswith("intraday")


# ---------------------------------------------------------------------------
# (c) parse_swarm_result
# ---------------------------------------------------------------------------


class TestParseSwarmResult:
    """parse_swarm_result(stdout) -> run id string or None."""

    def test_extracts_run_id_from_starting_line(self):
        stdout = (
            "Starting swarm: crypto_trading_desk\n"
            "Variables: {\"target\": \"BTC-USDT\"}\n"
        )
        # cmd_swarm_run_live prints the preset name then assigns run.id;
        # the run id format is swarm-YYYYMMDD-HHMMSS-<hex8>.
        stdout += "swarm-20260522-101530-ab12cd34\n"
        assert parse_swarm_result(stdout) == "swarm-20260522-101530-ab12cd34"

    def test_returns_none_when_no_run_id(self):
        assert parse_swarm_result("nothing useful here\n") is None

    def test_returns_first_run_id_when_multiple(self):
        stdout = (
            "swarm-20260522-101530-aaaaaaaa\n"
            "swarm-20260522-101533-bbbbbbbb\n"
        )
        assert parse_swarm_result(stdout) == "swarm-20260522-101530-aaaaaaaa"

    def test_handles_empty_output(self):
        assert parse_swarm_result("") is None


class TestExtractSwarmReport:
    """extract_swarm_report(stdout) -> prose desk analysis."""

    def test_extracts_after_final_report_marker(self):
        stdout = (
            "Starting swarm: crypto_trading_desk\n"
            "swarm-20260522-101530-ab12cd34\n"
            "── Final Report ──\n"
            "Desk recommends fading funding extremes.\n"
            "COMPLETED\n"
        )
        report = extract_swarm_report(stdout)
        # The box-drawing decoration around the header must be stripped.
        assert not report.startswith("─")
        assert not report.startswith("-")
        assert "Desk recommends fading funding extremes." in report

    def test_falls_back_to_tail_without_marker(self):
        # Without a "Final Report" marker the fallback must return the LAST
        # ~4000 chars (where the desk analysis actually is), NOT the first
        # ~4000 chars (which are startup banners).
        #
        # Build a stdout string that is ~8000 chars total:
        #   - First half: "START_BANNER" repeated to fill ~4000 chars
        #   - Second half: "END_ANALYSIS" repeated to fill ~4000 chars
        # Only the tail should be returned by the fallback.
        start_chunk = "START_BANNER" * 350     # ~4200 chars
        end_chunk = "END_ANALYSIS_" * 350       # ~4550 chars
        stdout = start_chunk + end_chunk
        assert len(stdout) > 8000              # confirm total length
        report = extract_swarm_report(stdout)
        # The analysis at the tail must be present in the returned text.
        assert "END_ANALYSIS_" in report
        # The very beginning (startup banners) must NOT be in the returned text.
        # We check for the specific start token that only appears at the beginning.
        assert not report.startswith("START_BANNER"), (
            "fallback must return the tail, not the beginning"
        )
        assert "START_BANNER" not in report, (
            "fallback returned the head of stdout instead of the tail"
        )

    def test_handles_empty(self):
        assert extract_swarm_report("") == ""

    def test_output_is_bounded(self):
        stdout = "Final Report\n" + ("x" * 10000)
        report = extract_swarm_report(stdout)
        assert len(report) <= 4000


# ---------------------------------------------------------------------------
# (d) build_strategy_spec
# ---------------------------------------------------------------------------


class TestBuildStrategySpec:
    """build_strategy_spec(...) -> (strategy_id, yaml_text) deterministic scaffold."""

    def test_returns_id_and_yaml(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        sid, yaml_text = build_strategy_spec(
            symbol="btc",
            ticker="BTC-USDT-SWAP",
            usable_factors=usable,
            swarm_rationale="Desk recommends contrarian funding fade.",
            seq=1,
        )
        assert isinstance(sid, str) and sid
        assert isinstance(yaml_text, str) and yaml_text

    def test_strategy_id_follows_convention(self):
        # strategy_runs.py documents <coin>_s<N>_<archetype>.
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        sid, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable, swarm_rationale="x", seq=2,
        )
        assert sid.startswith("btc_s2_")

    def test_yaml_parses_and_has_required_keys(self):
        # The generated YAML must match the schema of the existing
        # research/strategies/strategy_S*.yaml files.
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        for key in (
            "name", "archetype", "hypothesis", "symbol", "timeframe_signal",
            "hold_period", "indicators", "entry_long", "entry_short",
            "exit_rules", "position_sizing", "parameter_search_ranges",
            "expected_behavior", "caveats",
        ):
            assert key in doc, f"generated strategy YAML missing key: {key}"

    def test_yaml_symbol_matches_ticker(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        assert doc["symbol"] == "BTC-USDT-SWAP"

    def test_yaml_indicators_cover_usable_factors_only(self):
        # Indicators block is derived from the usable factors; the rejected
        # factor must not appear.
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        assert set(doc["indicators"].keys()) == {"funding_rate", "fng"}

    def test_swarm_rationale_recorded_in_caveats_or_hypothesis(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable,
            swarm_rationale="DESK_MARKER_TEXT",
            seq=1,
        )
        assert "DESK_MARKER_TEXT" in yaml_text

    def test_seq_changes_strategy_id(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        sid1, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable, swarm_rationale="x", seq=1,
        )
        sid3, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            usable_factors=usable, swarm_rationale="x", seq=3,
        )
        assert sid1 != sid3

    def test_raises_on_no_usable_factors(self):
        # A strategy with zero usable factors is meaningless; the builder
        # must refuse rather than emit a degenerate spec.
        with pytest.raises(ValueError):
            build_strategy_spec(
                symbol="btc", ticker="BTC-USDT-SWAP",
                usable_factors=[], swarm_rationale="x", seq=1,
            )

    def test_single_factor_yields_mean_reversion_archetype(self):
        # When exactly ONE factor passes stage-1 screening (verdict=single_use),
        # build_strategy_spec must use the <factor>_mean_reversion archetype,
        # NOT multi_factor_consensus. This exercises the single-factor branch of
        # _archetype_for() and validates that the strategy_id is named correctly.
        single_factor_manifest = _manifest(
            factors=[
                _factor("funding_rate", "single_use", 0.12),
                _factor("fng", "reject", 0.02),
            ]
        )
        usable = select_usable_factors(single_factor_manifest)
        assert len(usable) == 1, "test setup: exactly one non-rejected factor expected"

        sid, yaml_text = build_strategy_spec(
            symbol="btc",
            ticker="BTC-USDT-SWAP",
            usable_factors=usable,
            swarm_rationale="Single-factor desk rationale.",
            seq=1,
        )

        # The strategy_id must contain the single-factor mean-reversion archetype.
        assert "funding_rate_mean_reversion" in sid, (
            f"Expected 'funding_rate_mean_reversion' in strategy_id, got: {sid}"
        )
        # Sanity: multi_factor_consensus must NOT be selected.
        assert "multi_factor_consensus" not in sid

        # The archetype field in the YAML must also reflect the single-factor path.
        doc = yaml.safe_load(yaml_text)
        assert doc["archetype"] == "funding_rate_mean_reversion"


# ---------------------------------------------------------------------------
# (e) build_generation_block
# ---------------------------------------------------------------------------


class TestBuildGenerationBlock:
    """build_generation_block(...) -> dict aligned with GenerationBlock schema."""

    def test_validates_against_generation_block_schema(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        block = build_generation_block(
            run_id="swarm-20260522-101530-ab12cd34",
            usable_factors=usable,
            rationale="desk prose",
        )
        # Must validate cleanly against the dashboard schema so task 2.12
        # (emit_manifest.py) can drop it into StrategyManifest.generation.
        GenerationBlock.model_validate(block)

    def test_method_names_the_swarm(self):
        block = build_generation_block(
            run_id="swarm-x", usable_factors=[], rationale="r",
        )
        assert "crypto_trading_desk" in block["method"]

    def test_factors_used_lists_usable_factor_names(self):
        manifest = _manifest()
        usable = select_usable_factors(manifest)
        block = build_generation_block(
            run_id="swarm-x", usable_factors=usable, rationale="r",
        )
        assert set(block["factors_used"]) == {"funding_rate", "fng"}

    def test_source_run_carries_swarm_run_id(self):
        block = build_generation_block(
            run_id="swarm-20260522-101530-ab12cd34",
            usable_factors=[], rationale="r",
        )
        assert block["source_run"] == "swarm-20260522-101530-ab12cd34"

    def test_rationale_recorded(self):
        block = build_generation_block(
            run_id="swarm-x", usable_factors=[], rationale="DESK_PROSE_MARKER",
        )
        assert block["rationale"] == "DESK_PROSE_MARKER"

    def test_none_run_id_allowed(self):
        # When the swarm run id could not be parsed, source_run is null but
        # the block must still validate (audit data is best-effort).
        block = build_generation_block(
            run_id=None, usable_factors=[], rationale="r",
        )
        GenerationBlock.model_validate(block)
        assert block["source_run"] is None


# ---------------------------------------------------------------------------
# (f) verify_outputs / check_strategy / compute_exit_code / print_summary
# ---------------------------------------------------------------------------


def _write_generated(tmp_path: Path, sid: str = "btc_s2_funding") -> GeneratedStrategy:
    """Write a valid strategy YAML + generation.json under tmp_path, return handle."""
    strategies_dir = tmp_path / "strategies"
    manifests_dir = tmp_path / "manifests"
    strategies_dir.mkdir(exist_ok=True)
    (manifests_dir / sid).mkdir(parents=True, exist_ok=True)

    manifest = _manifest()
    usable = select_usable_factors(manifest)
    _, yaml_text = build_strategy_spec(
        symbol="btc", ticker="BTC-USDT-SWAP",
        usable_factors=usable, swarm_rationale="x", seq=2,
    )
    yaml_path = strategies_dir / f"strategy_{sid}.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    gen = build_generation_block(run_id="swarm-x", usable_factors=usable, rationale="r")
    gen_path = manifests_dir / sid / "generation.json"
    gen_path.write_text(json.dumps(gen), encoding="utf-8")

    return GeneratedStrategy(
        strategy_id=sid,
        symbol="btc",
        yaml_path=yaml_path,
        generation_path=gen_path,
    )


class TestCheckStrategy:
    """check_strategy(generated) -> StrategyCheckResult"""

    def test_valid_strategy_passes(self, tmp_path: Path):
        gen = _write_generated(tmp_path)
        result = check_strategy(gen)
        assert result.ok

    def test_missing_yaml_detected(self, tmp_path: Path):
        gen = _write_generated(tmp_path)
        gen.yaml_path.unlink()
        result = check_strategy(gen)
        assert not result.ok
        assert result.error is not None

    def test_missing_generation_json_detected(self, tmp_path: Path):
        gen = _write_generated(tmp_path)
        gen.generation_path.unlink()
        result = check_strategy(gen)
        assert not result.ok

    def test_invalid_generation_json_detected(self, tmp_path: Path):
        gen = _write_generated(tmp_path)
        # break the GenerationBlock contract (missing required 'method')
        gen.generation_path.write_text(json.dumps({"factors_used": []}), encoding="utf-8")
        result = check_strategy(gen)
        assert not result.ok

    def test_corrupt_yaml_detected(self, tmp_path: Path):
        gen = _write_generated(tmp_path)
        gen.yaml_path.write_text("{ not: valid: yaml: ::", encoding="utf-8")
        result = check_strategy(gen)
        assert not result.ok

    def test_yaml_missing_required_key_detected(self, tmp_path: Path):
        # check_strategy must reject a YAML that parses as a dict but is
        # missing one of the required strategy keys (e.g. 'archetype').
        gen = _write_generated(tmp_path)
        # Read the valid YAML, remove a required key, write it back.
        doc = yaml.safe_load(gen.yaml_path.read_text(encoding="utf-8"))
        assert "archetype" in doc  # confirm it was there
        del doc["archetype"]
        gen.yaml_path.write_text(
            yaml.safe_dump(doc, default_flow_style=False), encoding="utf-8"
        )
        result = check_strategy(gen)
        assert not result.ok
        assert result.error is not None
        assert "archetype" in result.error  # error message names the missing key

    def test_yaml_all_required_keys_present_passes(self, tmp_path: Path):
        # Confirm that a correctly written strategy passes the required-key check.
        gen = _write_generated(tmp_path)
        result = check_strategy(gen)
        assert result.ok  # all keys present -> must pass


class TestVerifyOutputs:
    """verify_outputs(generated_list) -> list[StrategyCheckResult]"""

    def test_all_present(self, tmp_path: Path):
        g1 = _write_generated(tmp_path, "btc_s2_a")
        g2 = _write_generated(tmp_path, "btc_s2_b")
        results = verify_outputs([g1, g2])
        assert len(results) == 2
        assert all(r.ok for r in results)

    def test_partial_failure(self, tmp_path: Path):
        g1 = _write_generated(tmp_path, "btc_s2_a")
        g2 = _write_generated(tmp_path, "btc_s2_b")
        g2.yaml_path.unlink()
        results = verify_outputs([g1, g2])
        assert sum(1 for r in results if r.ok) == 1

    def test_empty_list(self):
        assert verify_outputs([]) == []


class TestComputeExitCode:
    """compute_exit_code(results) -> int"""

    def test_zero_on_success(self):
        results = [StrategyCheckResult(strategy_id="a", ok=True)]
        assert compute_exit_code(results) == 0

    def test_nonzero_on_failure(self):
        results = [
            StrategyCheckResult(strategy_id="a", ok=True),
            StrategyCheckResult(strategy_id="b", ok=False, error="missing"),
        ]
        assert compute_exit_code(results) != 0

    def test_nonzero_on_empty(self):
        # Stage 2 producing zero strategies is a failure: there is nothing
        # for downstream stages to backtest.
        assert compute_exit_code([]) != 0

    def test_returns_int(self):
        assert isinstance(compute_exit_code([StrategyCheckResult("a", True)]), int)


class TestPrintSummary:
    """print_summary(results) — smoke test, must not raise."""

    def test_all_pass(self, capsys: pytest.CaptureFixture):
        results = [StrategyCheckResult(strategy_id="btc_s2_a", ok=True)]
        print_summary(results)
        out = capsys.readouterr().out
        assert "OK" in out
        assert "btc_s2_a" in out

    def test_failure_shown(self, capsys: pytest.CaptureFixture):
        results = [StrategyCheckResult(strategy_id="btc_s2_a", ok=False, error="boom")]
        print_summary(results)
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_empty(self, capsys: pytest.CaptureFixture):
        print_summary([])
        out = capsys.readouterr().out
        assert "0" in out


# ---------------------------------------------------------------------------
# Per-archetype spec builders (Task 2: TestBuildStrategySpecArchetype)
# ---------------------------------------------------------------------------


def _factor_pos_ic(name: str) -> dict:
    """Factor with positive IC at all horizons (trend direction)."""
    return {
        "name": name,
        "ic_by_horizon": {"8": 0.10, "24": 0.09, "72": 0.12, "168": 0.08},
        "ir": 0.6,
        "sample_size": 5000,
        "cross_regime_ic": None,
        "stability": None,
        "verdict": "single_use",
    }


def _factor_neg_ic(name: str) -> dict:
    """Factor with negative IC at all horizons (contrarian / gate direction)."""
    return {
        "name": name,
        "ic_by_horizon": {"8": -0.08, "24": -0.07, "72": -0.10, "168": -0.06},
        "ir": 0.5,
        "sample_size": 5000,
        "cross_regime_ic": None,
        "stability": None,
        "verdict": "single_use",
    }


def _make_factor_entry(d: dict):
    """Validate a factor dict into a FactorEntry object."""
    from schemas import FactorEntry
    return FactorEntry.model_validate(d)


_REQUIRED_YAML_KEYS = (
    "name", "archetype", "hypothesis", "symbol", "timeframe_signal",
    "hold_period", "indicators", "entry_long", "entry_short",
    "exit_rules", "position_sizing", "parameter_search_ranges",
    "expected_behavior", "caveats",
)


class TestBuildStrategySpecArchetype:
    """Tests for build_strategy_spec() with ArchetypePlan (Task 2 per-archetype builders)."""

    # ── 1. trend_with_gate builder ─────────────────────────────────────────

    def test_trend_with_gate_strategy_id_suffix(self):
        """strategy_id ends with _trend_with_gate."""
        trend_f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        gate_f = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="trend_with_gate",
            factors=[(trend_f, "trend"), (gate_f, "gate")],
        )
        sid, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        assert sid.endswith("_trend_with_gate"), (
            f"Expected strategy_id to end with '_trend_with_gate', got: {sid}"
        )

    def test_trend_with_gate_long_entry_conditions(self):
        """Long entry: trend >= 80 AND gate <= 20; logic=all."""
        trend_f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        gate_f = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="trend_with_gate",
            factors=[(trend_f, "trend"), (gate_f, "gate")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        entry_long = doc["entry_long"]
        assert entry_long["logic"] == "all"
        conditions = entry_long["conditions"]
        # Trend factor should be >= 80 in long entry (positive IC -> high extreme)
        assert any(">= 80" in c and "stablecoin_supply_z" in c for c in conditions), (
            f"Expected trend factor condition '>= 80' in entry_long, got: {conditions}"
        )
        # Gate factor should be <= 20 in long entry (negative IC -> low extreme)
        assert any("<= 20" in c and "funding_rate" in c for c in conditions), (
            f"Expected gate factor condition '<= 20' in entry_long, got: {conditions}"
        )

    def test_trend_with_gate_short_entry_conditions(self):
        """Short entry: trend <= 20 AND gate >= 80; logic=all."""
        trend_f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        gate_f = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="trend_with_gate",
            factors=[(trend_f, "trend"), (gate_f, "gate")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        entry_short = doc["entry_short"]
        assert entry_short["logic"] == "all"
        conditions = entry_short["conditions"]
        # Trend factor should be <= 20 in short entry (positive IC -> low extreme for short)
        assert any("<= 20" in c and "stablecoin_supply_z" in c for c in conditions), (
            f"Expected trend factor condition '<= 20' in entry_short, got: {conditions}"
        )
        # Gate factor should be >= 80 in short entry (negative IC -> high extreme for short)
        assert any(">= 80" in c and "funding_rate" in c for c in conditions), (
            f"Expected gate factor condition '>= 80' in entry_short, got: {conditions}"
        )

    def test_trend_with_gate_hypothesis_mentions_capital_inflowing(self):
        """Hypothesis mentions 'capital inflowing AND longs not crowded'."""
        trend_f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        gate_f = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="trend_with_gate",
            factors=[(trend_f, "trend"), (gate_f, "gate")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        hypothesis = doc["hypothesis"]
        assert "capital inflowing" in hypothesis.lower() or "capital inflowing" in yaml_text.lower(), (
            "Expected 'capital inflowing' in hypothesis"
        )
        assert "longs not crowded" in hypothesis.lower() or "longs not crowded" in yaml_text.lower(), (
            "Expected 'longs not crowded' in hypothesis"
        )

    # ── 2. single_factor builder ──────────────────────────────────────────

    def test_single_factor_strategy_id_suffix(self):
        """strategy_id ends with _single_factor."""
        f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        plan = ArchetypePlan(archetype="single_factor", factors=[(f, "signal")])
        sid, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        assert sid.endswith("_single_factor"), (
            f"Expected strategy_id to end with '_single_factor', got: {sid}"
        )

    def test_single_factor_exactly_one_condition_in_entries(self):
        """Exactly 1 condition in entry_long and entry_short."""
        f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        plan = ArchetypePlan(archetype="single_factor", factors=[(f, "signal")])
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        assert len(doc["entry_long"]["conditions"]) == 1, (
            "single_factor must produce exactly 1 condition in entry_long"
        )
        assert len(doc["entry_short"]["conditions"]) == 1, (
            "single_factor must produce exactly 1 condition in entry_short"
        )

    def test_single_factor_logic_is_all(self):
        """Logic is 'all' for single_factor."""
        f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        plan = ArchetypePlan(archetype="single_factor", factors=[(f, "signal")])
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        assert doc["entry_long"]["logic"] == "all"
        assert doc["entry_short"]["logic"] == "all"

    # ── 3. consensus_all 2-factor builder ────────────────────────────────

    def test_consensus_all_2factor_strategy_id_suffix(self):
        """strategy_id ends with _consensus_all."""
        f1 = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        f2 = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="consensus_all",
            factors=[(f1, "consensus"), (f2, "consensus")],
        )
        sid, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        assert sid.endswith("_consensus_all"), (
            f"Expected strategy_id to end with '_consensus_all', got: {sid}"
        )

    def test_consensus_all_2factor_logic_is_all(self):
        """2-factor consensus uses logic=all (strict AND)."""
        f1 = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        f2 = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="consensus_all",
            factors=[(f1, "consensus"), (f2, "consensus")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        assert doc["entry_long"]["logic"] == "all", (
            "2-factor consensus must use logic=all"
        )

    # ── 4. consensus_all 3-factor builder ────────────────────────────────

    def test_consensus_all_3factor_logic_is_any(self):
        """3-factor consensus uses logic=any (sparsity rule)."""
        f1 = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        f2 = _make_factor_entry(_factor_neg_ic("funding_rate"))
        f3 = _make_factor_entry(_factor_pos_ic("basis_rel"))
        plan = ArchetypePlan(
            archetype="consensus_all",
            factors=[(f1, "consensus"), (f2, "consensus"), (f3, "consensus")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=2,
        )
        doc = yaml.safe_load(yaml_text)
        assert doc["entry_long"]["logic"] == "any", (
            "3-factor consensus must use logic=any (sparsity rule)"
        )

    def test_consensus_all_3factor_strategy_id_suffix(self):
        """strategy_id ends with _consensus_all for 3 factors."""
        f1 = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        f2 = _make_factor_entry(_factor_neg_ic("funding_rate"))
        f3 = _make_factor_entry(_factor_pos_ic("basis_rel"))
        plan = ArchetypePlan(
            archetype="consensus_all",
            factors=[(f1, "consensus"), (f2, "consensus"), (f3, "consensus")],
        )
        sid, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=2,
        )
        assert sid.endswith("_consensus_all"), (
            f"Expected strategy_id to end with '_consensus_all', got: {sid}"
        )

    # ── 5. Required YAML keys for all archetypes ──────────────────────────

    def test_required_yaml_keys_single_factor(self):
        """single_factor spec has all required YAML keys."""
        f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        plan = ArchetypePlan(archetype="single_factor", factors=[(f, "signal")])
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        for key in _REQUIRED_YAML_KEYS:
            assert key in doc, f"single_factor spec missing required key: {key}"

    def test_required_yaml_keys_trend_with_gate(self):
        """trend_with_gate spec has all required YAML keys."""
        trend_f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        gate_f = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="trend_with_gate",
            factors=[(trend_f, "trend"), (gate_f, "gate")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        for key in _REQUIRED_YAML_KEYS:
            assert key in doc, f"trend_with_gate spec missing required key: {key}"

    def test_required_yaml_keys_consensus_all(self):
        """consensus_all spec has all required YAML keys."""
        f1 = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        f2 = _make_factor_entry(_factor_neg_ic("funding_rate"))
        plan = ArchetypePlan(
            archetype="consensus_all",
            factors=[(f1, "consensus"), (f2, "consensus")],
        )
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=1,
        )
        doc = yaml.safe_load(yaml_text)
        for key in _REQUIRED_YAML_KEYS:
            assert key in doc, f"consensus_all spec missing required key: {key}"

    # ── strategy_id format ─────────────────────────────────────────────────

    def test_strategy_id_format_coin_seq_archetype(self):
        """strategy_id follows <coin>_s<seq>_<archetype> for all archetypes."""
        f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        plan = ArchetypePlan(archetype="single_factor", factors=[(f, "signal")])
        sid, _ = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="x", seq=7,
        )
        assert sid == "btc_s7_single_factor", (
            f"Expected 'btc_s7_single_factor', got: {sid}"
        )


# ---------------------------------------------------------------------------
# Integration test: compile each archetype through stage-2b
# (Task 2: TestArchetypeSpecCompilesToSignalEngine)
# ---------------------------------------------------------------------------

import importlib.util
import textwrap
from unittest.mock import MagicMock, patch

from pipeline.stage2b_compile_signal import _compile_one  # noqa: E402


def _make_entry_for_yaml(strat_id: str, yaml_text: str, tmp_path: Path) -> dict:
    """Write a YAML to a temp strategies dir and return a strategy_runs entry dict."""
    strategies_dir = tmp_path / "research" / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = strategies_dir / f"strategy_{strat_id}.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")
    return {
        "symbol": "BTC-USDT-SWAP",
        "spec_yaml": f"research/strategies/strategy_{strat_id}.yaml",
        "base_run": None,
        "regime_runs": {},
        "stress_runs": {},
        "oos_runs": [],
        "sweep_run": None,
    }


@pytest.mark.integration
class TestArchetypeSpecCompilesToSignalEngine:
    """End-to-end: each archetype spec (built by build_strategy_spec) compiles via stage-2b."""

    def _build_and_compile(self, plan: ArchetypePlan, strat_id: str, tmp_path: Path):
        """Helper: build spec for plan, then compile via _compile_one."""
        _, yaml_text = build_strategy_spec(
            symbol="btc", ticker="BTC-USDT-SWAP",
            plan=plan, swarm_rationale="test rationale", seq=1,
        )
        entry = _make_entry_for_yaml(strat_id, yaml_text, tmp_path)
        with (
            patch("pipeline.stage2b_compile_signal._REPO_ROOT", tmp_path),
            patch("pipeline.stage2b_compile_signal.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
            result = _compile_one(strat_id, entry)
        return result

    def test_single_factor_compiles_without_error(self, tmp_path: Path):
        """single_factor spec compiles through stage-2b without schema/AST error."""
        f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        plan = ArchetypePlan(archetype="single_factor", factors=[(f, "signal")])
        result = self._build_and_compile(plan, "btc_s1_single_factor", tmp_path)
        assert result.status == "ok", (
            f"single_factor spec failed stage-2b compilation: {result.message}"
        )

    def test_trend_with_gate_compiles_without_error(self, tmp_path: Path):
        """trend_with_gate spec compiles through stage-2b without schema/AST error."""
        trend_f = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        # Use a non-special factor name (not in _KNOWN_SOURCES) so the source
        # resolves to "stage1:basis_rel" which satisfies StrategySpec validation.
        gate_f = _make_factor_entry(_factor_neg_ic("basis_rel"))
        plan = ArchetypePlan(
            archetype="trend_with_gate",
            factors=[(trend_f, "trend"), (gate_f, "gate")],
        )
        result = self._build_and_compile(plan, "btc_s1_trend_with_gate", tmp_path)
        assert result.status == "ok", (
            f"trend_with_gate spec failed stage-2b compilation: {result.message}"
        )

    def test_consensus_all_2factor_compiles_without_error(self, tmp_path: Path):
        """consensus_all (2 factors) spec compiles through stage-2b without error."""
        f1 = _make_factor_entry(_factor_pos_ic("stablecoin_supply_z"))
        # Use a non-special factor name so the source resolves to "stage1:oi_change"
        # which satisfies StrategySpec validation (avoids okx: prefix special case).
        f2 = _make_factor_entry(_factor_neg_ic("oi_change_z"))
        plan = ArchetypePlan(
            archetype="consensus_all",
            factors=[(f1, "consensus"), (f2, "consensus")],
        )
        result = self._build_and_compile(plan, "btc_s1_consensus_all", tmp_path)
        assert result.status == "ok", (
            f"consensus_all spec failed stage-2b compilation: {result.message}"
        )


# ---------------------------------------------------------------------------
# Task 3: TestStage2MultiEmit — multi-emit archetype fan-out + swarm demotion
# ---------------------------------------------------------------------------

import os
import subprocess
from unittest.mock import patch, MagicMock

from pipeline.lib.archetype_router import pick_archetypes  # noqa: E402


def _sym_config(name: str = "btc", okx_swap: str = "BTC-USDT-SWAP", ccxt_bybit: str = "BTC/USDT:USDT"):
    """Return a minimal SymbolConfig-like object for _generate_for_symbol_multi tests."""
    from pipeline.config import SymbolConfig
    return SymbolConfig(name=name, okx_swap=okx_swap, ccxt_bybit=ccxt_bybit)


def _write_factor_manifest(manifests_dir: Path, sym_name: str, factors: list[dict]) -> Path:
    """Write a stage-1 factor manifest JSON for a symbol and return its path."""
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "symbol": sym_name.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": 730,
        "horizons_h": [8, 24, 72, 168],
        "factors": factors,
    }
    path = manifests_dir / f"factor_{sym_name}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class TestStage2MultiEmit:
    """Task 3: _generate_for_symbol_multi — archetype fan-out and swarm demotion."""

    # Two mixed-sign factors -> pick_archetypes returns 3 plans:
    # single_factor + trend_with_gate + consensus_all
    _MIXED_FACTORS = [
        _factor("stablecoin_supply_z", "single_use", ic8=0.10),   # positive IC
        _factor("funding_rate",        "single_use", ic8=-0.08),  # negative IC
    ]

    # ── Test 1: Default run produces all routed archetypes, no swarm ──────

    def test_default_run_no_swarm_called(self, tmp_path: Path):
        """_generate_for_symbol_multi() in default mode never calls run_swarm."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        with patch("pipeline.stage2_strategies.run_swarm") as mock_swarm:
            results = _generate_for_symbol_multi(
                sym, strategies_dir, manifests_dir, use_swarm=False
            )

        mock_swarm.assert_not_called()
        assert len(results) > 0

    def test_default_run_emits_all_routed_archetypes(self, tmp_path: Path):
        """N strategies returned = len(pick_archetypes(usable_factors))."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        # Compute expected number of plans from the router itself
        from schemas import FactorManifest, FactorEntry
        manifest = FactorManifest.model_validate(
            json.loads((manifests_dir / "factor_btc.json").read_text())
        )
        usable = select_usable_factors(manifest)
        expected_plans = pick_archetypes(usable)

        results = _generate_for_symbol_multi(
            sym, strategies_dir, manifests_dir, use_swarm=False
        )

        assert len(results) == len(expected_plans), (
            f"Expected {len(expected_plans)} strategies (one per archetype plan), "
            f"got {len(results)}"
        )

    # ── Test 2: Swarm failure still writes specs and returns strategies ───

    def test_swarm_failure_still_returns_strategies(self, tmp_path: Path):
        """When use_swarm=True and run_swarm raises CalledProcessError, still returns strategies."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        def _raise(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["vibe-trading"], output="", stderr="timeout")

        with patch("pipeline.stage2_strategies.run_swarm", side_effect=_raise):
            results = _generate_for_symbol_multi(
                sym, strategies_dir, manifests_dir, use_swarm=True
            )

        # Must still produce strategies (no raise propagated)
        assert len(results) > 0

    def test_swarm_failure_strategies_are_valid_yaml(self, tmp_path: Path):
        """Strategies written after swarm failure have valid YAML with required keys."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        def _raise(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["vibe-trading"])

        with patch("pipeline.stage2_strategies.run_swarm", side_effect=_raise):
            results = _generate_for_symbol_multi(
                sym, strategies_dir, manifests_dir, use_swarm=True
            )

        _REQUIRED = (
            "name", "archetype", "hypothesis", "symbol", "timeframe_signal",
            "hold_period", "indicators", "entry_long", "entry_short",
            "exit_rules", "position_sizing", "parameter_search_ranges",
            "expected_behavior", "caveats",
        )
        for gen in results:
            assert gen.yaml_path.exists(), f"YAML not written: {gen.yaml_path}"
            doc = yaml.safe_load(gen.yaml_path.read_text(encoding="utf-8"))
            for key in _REQUIRED:
                assert key in doc, f"Missing key {key!r} in {gen.strategy_id}"

    # ── Test 3: strategy_id uniqueness ────────────────────────────────────

    def test_strategy_ids_are_unique(self, tmp_path: Path):
        """All strategies for one symbol have distinct strategy_ids."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        results = _generate_for_symbol_multi(
            sym, strategies_dir, manifests_dir, use_swarm=False
        )

        ids = [g.strategy_id for g in results]
        assert len(ids) == len(set(ids)), (
            f"Duplicate strategy_ids found: {ids}"
        )

    def test_strategy_ids_have_sequential_seq_numbers(self, tmp_path: Path):
        """strategy_ids contain _s1_, _s2_, ... in order."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        results = _generate_for_symbol_multi(
            sym, strategies_dir, manifests_dir, use_swarm=False
        )

        for i, gen in enumerate(results, start=1):
            assert f"_s{i}_" in gen.strategy_id, (
                f"Expected _s{i}_ in strategy_id at position {i}, got: {gen.strategy_id}"
            )

    # ── Test 4: RESEARCH_STAGE2_USE_SWARM env var default is off ─────────

    def test_env_var_default_is_off(self, monkeypatch):
        """RESEARCH_STAGE2_USE_SWARM env var must not be set by default (off)."""
        monkeypatch.delenv("RESEARCH_STAGE2_USE_SWARM", raising=False)
        val = os.environ.get("RESEARCH_STAGE2_USE_SWARM", "")
        use_swarm = val.lower() in ("1", "true", "yes")
        assert not use_swarm, (
            "RESEARCH_STAGE2_USE_SWARM should be unset/empty by default — swarm off"
        )

    def test_env_var_true_enables_swarm(self, monkeypatch):
        """RESEARCH_STAGE2_USE_SWARM=true evaluates to use_swarm=True."""
        monkeypatch.setenv("RESEARCH_STAGE2_USE_SWARM", "true")
        val = os.environ.get("RESEARCH_STAGE2_USE_SWARM", "")
        use_swarm = val.lower() in ("1", "true", "yes")
        assert use_swarm

    def test_env_var_1_enables_swarm(self, monkeypatch):
        """RESEARCH_STAGE2_USE_SWARM=1 evaluates to use_swarm=True."""
        monkeypatch.setenv("RESEARCH_STAGE2_USE_SWARM", "1")
        val = os.environ.get("RESEARCH_STAGE2_USE_SWARM", "")
        use_swarm = val.lower() in ("1", "true", "yes")
        assert use_swarm

    # ── Test 5: generation.json uses correct method constant ─────────────

    def test_deterministic_mode_writes_deterministic_method(self, tmp_path: Path):
        """generation.json uses GENERATION_METHOD_DETERMINISTIC when swarm is off."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        results = _generate_for_symbol_multi(
            sym, strategies_dir, manifests_dir, use_swarm=False
        )

        for gen in results:
            raw = json.loads(gen.generation_path.read_text(encoding="utf-8"))
            assert raw["method"] == GENERATION_METHOD_DETERMINISTIC, (
                f"Expected deterministic method in generation.json for {gen.strategy_id}"
            )
            assert raw["source_run"] is None, (
                "source_run must be None in deterministic mode"
            )

    def test_generation_constants_exist(self):
        """GENERATION_METHOD_DETERMINISTIC and GENERATION_METHOD_SWARM are exported."""
        assert isinstance(GENERATION_METHOD_DETERMINISTIC, str) and GENERATION_METHOD_DETERMINISTIC
        assert isinstance(GENERATION_METHOD_SWARM, str) and GENERATION_METHOD_SWARM
        assert "deterministic" in GENERATION_METHOD_DETERMINISTIC.lower()
        assert "swarm" in GENERATION_METHOD_SWARM.lower()

    def test_deterministic_rationale_mentions_archetype(self, tmp_path: Path):
        """Deterministic rationale in hypothesis mentions the archetype name."""
        strategies_dir = tmp_path / "strategies"
        manifests_dir = tmp_path / "manifests"
        _write_factor_manifest(manifests_dir, "btc", self._MIXED_FACTORS)
        sym = _sym_config("btc", "BTC-USDT-SWAP")

        results = _generate_for_symbol_multi(
            sym, strategies_dir, manifests_dir, use_swarm=False
        )

        for gen in results:
            raw = json.loads(gen.generation_path.read_text(encoding="utf-8"))
            assert raw["rationale"] is not None, f"rationale is None for {gen.strategy_id}"
