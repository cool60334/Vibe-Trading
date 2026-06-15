"""
research/pipeline/stage0_discovery.py
───────────────────────────────────────
Stage-0 runner: Factor Discovery.

For each symbol in research_config.yaml, builds a `candidates_<sym>.json`
manifest. The runner has two modes:

  * **Deterministic** (default; ``--no-swarm`` / env
    ``RESEARCH_STAGE0_USE_SWARM=0``) — picks factors from the Stage 0a
    evidence manifest using a pure rule (``select_candidates_from_evidence``).
    No LLM is called; cannot fail on JSON parse errors. This is the path
    that runs in CI and in routine pipeline executions.

  * **Enrichment** (``--use-swarm`` / env ``RESEARCH_STAGE0_USE_SWARM=1``) —
    starts with the deterministic list, then calls the ``crypto_factor_lab``
    swarm to overwrite each candidate's ``economic_logic`` string with a
    human-friendly explanation. Swarm failures are *fail-soft*: the
    deterministic placeholder text is preserved and the pipeline continues
    with exit code 0.

The legacy "swarm-primary + hardcoded fallback" path has been removed; see
``openspec/changes/stage0-deterministic-discovery/`` for the proposal.

Usage
-----
    # From repo root:
    python -m research.pipeline.stage0_discovery

    # From research/ directory (preferred):
    python -m pipeline.stage0_discovery

    # Direct script invocation:
    python research/pipeline/stage0_discovery.py

    # Force re-run (ignore cache):
    python -m pipeline.stage0_discovery --force

    # Enrich economic_logic via swarm (LLM call):
    python -m pipeline.stage0_discovery --use-swarm

Design note
-----------
Same thin-orchestration pattern as stage1_factors.py:
  - pure-logic helpers at module level (testable, no I/O dependencies)
  - subprocess swarm call isolated in run_swarm() (optional)
  - _process_symbol() handles one symbol end-to-end
  - main() drives the loop then verify_outputs() + sys.exit()
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Path bootstrap ─────────────────────────────────────────────────────────────
# This module lives at <repo-root>/research/pipeline/stage0_discovery.py.
# Bootstrap research/ and dashboard/server/ onto sys.path so imports work
# regardless of CWD or how this script is invoked.
_THIS_FILE = Path(__file__).resolve()
_PIPELINE_DIR = _THIS_FILE.parent          # research/pipeline/
_RESEARCH_DIR = _PIPELINE_DIR.parent       # research/
_REPO_ROOT = _RESEARCH_DIR.parent          # repo root
_DASHBOARD_SCHEMAS = _REPO_ROOT / "dashboard" / "server"

for _p in (_RESEARCH_DIR, _DASHBOARD_SCHEMAS):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

# ── Standard library ───────────────────────────────────────────────────────────
import argparse
import dataclasses
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

# ── Third-party ────────────────────────────────────────────────────────────────
import pandas as pd

# ── Internal imports ───────────────────────────────────────────────────────────
from pipeline.config import _REPO_ROOT as _CFG_REPO_ROOT, ResearchConfig, load_config
from pipeline.lib.factor_selector import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_ABS_IC,
    DEFAULT_MIN_ABS_IR,
    select_candidates_from_evidence,
)
from lib.sources import SOURCE_REGISTRY, TRANSFORM_REGISTRY
from schemas import CandidatesManifest, FactorCandidate

# ─── Constants ────────────────────────────────────────────────────────────────

#: Swarm preset name for factor discovery.
SWARM_PRESET = "crypto_factor_lab"

#: Default timeout for a swarm subprocess (seconds). Longer than stage 2
#: because the factor lab may run more parallel workers.
SWARM_TIMEOUT_S = 1200

#: ANSI codes for red warning lines.
_RED = "\033[91m"
_RESET = "\033[0m"

#: Regex that matches a fenced JSON code block produced by an LLM.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)


# ─── Pure-logic helpers (testable, network-free) ──────────────────────────────


@dataclasses.dataclass
class CandidatesCheckResult:
    """Result of checking a single symbol's candidates manifest."""

    symbol: str
    exists: bool
    valid: bool
    n_candidates: int = 0
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exists and self.valid


def parse_candidates_json(stdout: str) -> list[dict]:
    """Extract the first JSON fenced code block that is a JSON array from stdout.

    Scans ALL fenced code blocks (```json or ```) in order and returns the
    content of the first one that parses as a JSON list. This avoids the bug
    where an LLM emits a reasoning JSON object before the actual candidate list.

    Args:
        stdout: Raw string output from the swarm subprocess.

    Returns:
        List of raw dicts (not yet validated against FactorCandidate).

    Raises:
        ValueError: If no fenced block contains a valid JSON array.
    """
    matches = _JSON_FENCE_RE.findall(stdout or "")
    if not matches:
        raise ValueError(
            "No JSON fenced code block found in swarm output. "
            "Expected a ```json ... ``` block containing a list of factor candidates."
        )

    for raw_json in matches:
        text = raw_json.strip()
        try:
            data = json.loads(text, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data

    raise ValueError(
        "No valid JSON array found in any fenced code block in stdout. "
        f"Found {len(matches)} block(s) but none parsed as a JSON list."
    )


def filter_invalid_candidates(
    raw_candidates: list[dict],
    available_sources: list[str],
    available_transforms: list[str],
) -> tuple[list[dict], list[str]]:
    """Filter out candidates with unavailable source or unknown transform.

    Args:
        raw_candidates:       List of raw dicts from parse_candidates_json().
        available_sources:    SOURCE_REGISTRY keys with status="available".
        available_transforms: TRANSFORM_REGISTRY keys.

    Returns:
        (valid_candidates, warnings) where ``warnings`` is a list of human-
        readable warning strings, one per filtered-out candidate.
    """
    valid: list[dict] = []
    warnings: list[str] = []

    source_set = set(available_sources)
    transform_set = set(available_transforms)

    for i, cand in enumerate(raw_candidates):
        name = cand.get("name", f"<candidate[{i}]>")
        src = cand.get("data_source", "")
        tfm = cand.get("transform", "")

        # Evidence-driven candidates identify factors by feature_key, not by the
        # legacy data_source/transform registry pair. When a feature_key is
        # present, defer validation to validate_feature_keys (which checks the
        # key exists in the feature store) and skip the legacy registry gate.
        if cand.get("feature_key"):
            valid.append(cand)
            continue

        if src not in source_set:
            warnings.append(
                f"[stage0] Filtered candidate '{name}': "
                f"data_source='{src}' not in available sources {sorted(source_set)}."
            )
            continue

        if tfm not in transform_set:
            warnings.append(
                f"[stage0] Filtered candidate '{name}': "
                f"transform='{tfm}' not in TRANSFORM_REGISTRY {sorted(transform_set)}."
            )
            continue

        valid.append(cand)

    return valid, warnings


def validate_feature_keys(
    candidates: list[dict],
    available_feature_keys: set[str],
) -> tuple[list[dict], list[str]]:
    """Filter out candidates whose feature_key is None/missing or not in the feature store.

    Args:
        candidates:             List of raw candidate dicts (already passed through
                                filter_invalid_candidates and Pydantic validation or
                                still raw dicts — feature_key is the only field checked).
        available_feature_keys: Set of column names present in the features parquet.

    Returns:
        ``(kept, warnings)`` where ``kept`` is the subset of candidates with a
        valid feature_key and ``warnings`` is a list of human-readable strings,
        one per discarded candidate.
    """
    kept: list[dict] = []
    warnings: list[str] = []

    for i, cand in enumerate(candidates):
        name = cand.get("name", f"<candidate[{i}]>")
        feature_key = cand.get("feature_key")
        if feature_key is None:
            warnings.append(
                f"[stage0] Discarded candidate '{name}': "
                "feature_key is None or missing — must be set to a column in the feature store."
            )
            continue
        if feature_key not in available_feature_keys:
            warnings.append(
                f"[stage0] Discarded candidate '{name}': "
                f"feature_key='{feature_key}' not found in feature store columns."
            )
            continue
        kept.append(cand)

    return kept, warnings


def cache_hit(manifests_dir: Path, sym: str, cache_days: int) -> bool:
    """Return True if a valid cached manifest exists within the TTL window.

    Args:
        manifests_dir: Path to research/manifests/.
        sym:           Short lowercase symbol name (e.g. "eth").
        cache_days:    TTL in days. If 0, cache is disabled and this always
                       returns False.

    Returns:
        True if candidates_<sym>.json exists and its ``generated_at`` timestamp
        is less than ``cache_days`` days old; False otherwise.
    """
    if cache_days == 0:
        return False

    path = manifests_dir / f"candidates_{sym}.json"
    if not path.exists():
        return False

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        generated_at_str = raw.get("generated_at", "")
        generated_at = datetime.fromisoformat(generated_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_days = (now - generated_at).total_seconds() / 86400.0
        return age_days < cache_days
    except Exception:  # noqa: BLE001
        return False


def verify_outputs(
    manifests_dir: Path,
    symbols: list[str],
) -> list[CandidatesCheckResult]:
    """Check all expected candidates manifests after stage 0 runs.

    Args:
        manifests_dir: Path to research/manifests/.
        symbols:       Short lowercase symbol names from config.

    Returns:
        One CandidatesCheckResult per symbol, in input order.
    """
    results: list[CandidatesCheckResult] = []
    for sym in symbols:
        path = manifests_dir / f"candidates_{sym}.json"
        if not path.exists():
            results.append(
                CandidatesCheckResult(
                    symbol=sym,
                    exists=False,
                    valid=False,
                    error="file not found",
                )
            )
            continue

        try:
            raw = path.read_text(encoding="utf-8")
            manifest = CandidatesManifest.model_validate_json(raw)
            results.append(
                CandidatesCheckResult(
                    symbol=sym,
                    exists=True,
                    valid=True,
                    n_candidates=len(manifest.candidates),
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                CandidatesCheckResult(
                    symbol=sym,
                    exists=True,
                    valid=False,
                    error=str(exc),
                )
            )

    return results


def compute_exit_code(results: list[CandidatesCheckResult]) -> int:
    """Return 0 if all ok, 1 if any failed.

    Args:
        results: List from verify_outputs().

    Returns:
        0 on full success; 1 if any result is not ok.
    """
    return 0 if all(r.ok for r in results) else 1


def print_summary(results: list[CandidatesCheckResult]) -> None:
    """Print a human-readable per-symbol summary to stdout.

    Args:
        results: List from verify_outputs().
    """
    print("\n" + "=" * 60)
    print("Stage-0 output verification summary")
    print("=" * 60)
    for r in results:
        status = "OK" if r.ok else "FAIL"
        if r.ok:
            cache_tag = " (from cache)" if r.from_cache else ""
            msg = f"{r.n_candidates} candidates{cache_tag}"
        elif not r.exists:
            msg = f"MISSING — {r.error}"
        else:
            msg = f"INVALID — {r.error}"
        print(f"  [{status}] {r.symbol}: {msg}")

    total = len(results)
    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{total} symbols passed.")
    if passed < total:
        print("Stage 0 FAILED: one or more candidate manifests are missing or invalid.")
    else:
        print("Stage 0 PASSED: all candidate manifests present and valid.")
    print("=" * 60)


# ─── Swarm invocation (thin shell, not unit-tested) ───────────────────────────


def run_swarm(vars_dict: dict, timeout: int = SWARM_TIMEOUT_S) -> str:
    """Invoke ``vibe-trading --swarm-run crypto_factor_lab`` and return stdout.

    The CLI is at ``<repo-root>/agent/cli.py`` and must be run with
    cwd=<repo-root>/agent so its ``src.*`` imports resolve correctly.

    Args:
        vars_dict: User-vars dict to pass as JSON to the CLI.
        timeout:   Subprocess wall-clock timeout in seconds.

    Returns:
        Captured stdout string.

    Raises:
        subprocess.TimeoutExpired: If the swarm does not finish within timeout.
        subprocess.CalledProcessError: If the CLI exits non-zero.
    """
    agent_dir = _CFG_REPO_ROOT / "agent"
    vars_json = json.dumps(vars_dict, ensure_ascii=False)

    # Invoke the CLI as a module (``python -m cli``) with cwd=agent so its
    # ``cli.*`` package imports resolve. The legacy entrypoint handles
    # ``--swarm-run``. (There is no standalone ``agent/cli.py`` file.)
    cmd = [sys.executable, "-m", "cli", "--swarm-run", SWARM_PRESET, vars_json]
    print(
        f"[stage0] invoking swarm: {SWARM_PRESET}  "
        f"(cwd={agent_dir}, timeout={timeout}s)"
    )
    completed = subprocess.run(
        cmd,
        cwd=str(agent_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        stderr_snippet = (completed.stderr or "")[:2000]
        print(
            f"[stage0] swarm subprocess exited with code {completed.returncode}.\n"
            f"stderr (first 2000 chars):\n{stderr_snippet}",
            file=sys.stderr,
        )
        raise subprocess.CalledProcessError(
            returncode=completed.returncode,
            cmd=cmd,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.stdout or ""


# ─── Deterministic + enrichment helpers ──────────────────────────────────────


def _read_stage0_selector_overrides(
    raw_config: dict | None,
) -> tuple[float, float, int]:
    """Return (min_abs_ic, min_abs_ir, max_candidates) using config overrides.

    Looks up the ``stage0_selector`` block in the raw config dict. Any field
    not present falls back to the module defaults (DEFAULT_MIN_ABS_IC etc.).

    Args:
        raw_config: Mapping loaded from research_config.yaml, or None. When
                    None, all defaults are used.

    Returns:
        ``(min_abs_ic, min_abs_ir, max_candidates)`` tuple.
    """
    selector_cfg: dict[str, Any] = {}
    if isinstance(raw_config, dict):
        block = raw_config.get("stage0_selector")
        if isinstance(block, dict):
            selector_cfg = block

    try:
        min_abs_ic = float(selector_cfg.get("min_abs_ic", DEFAULT_MIN_ABS_IC))
    except (TypeError, ValueError):
        min_abs_ic = DEFAULT_MIN_ABS_IC
    try:
        min_abs_ir = float(selector_cfg.get("min_abs_ir", DEFAULT_MIN_ABS_IR))
    except (TypeError, ValueError):
        min_abs_ir = DEFAULT_MIN_ABS_IR
    try:
        max_candidates = int(selector_cfg.get("max_candidates", DEFAULT_MAX_CANDIDATES))
    except (TypeError, ValueError):
        max_candidates = DEFAULT_MAX_CANDIDATES

    return min_abs_ic, min_abs_ir, max_candidates


def run_deterministic_discovery(
    sym_name: str,
    evidence_path: Path,
    features_path: Path,
    *,
    min_abs_ic: float = DEFAULT_MIN_ABS_IC,
    min_abs_ir: float = DEFAULT_MIN_ABS_IR,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[FactorCandidate]:
    """Select FactorCandidates deterministically from an evidence manifest.

    Loads evidence JSON and feature store columns from disk, then delegates
    to ``select_candidates_from_evidence``. Pure on top of those two reads —
    no swarm, no network.

    Args:
        sym_name:       Short symbol name, e.g. "btc".
        evidence_path:  Path to ``evidence_<sym>.json``.
        features_path:  Path to ``features_<sym>.parquet``.
        min_abs_ic:     IC threshold passed to selector.
        min_abs_ir:     IR threshold passed to selector.
        max_candidates: Hard cap on returned list length.

    Returns:
        List of FactorCandidate objects, may be empty.
    """
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    try:
        feature_cols = set(
            pd.read_parquet(features_path, engine="pyarrow").columns.tolist()
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[stage0] {sym_name}: could not read features parquet for column set: {exc}",
            file=sys.stderr,
        )
        feature_cols = set()

    return select_candidates_from_evidence(
        evidence,
        feature_cols,
        min_abs_ic=min_abs_ic,
        min_abs_ir=min_abs_ir,
        max_candidates=max_candidates,
    )


def enrich_candidates_with_swarm(
    candidates: list[FactorCandidate],
    sym_name: str,
    okx_swap: str,
    cfg: ResearchConfig,
    manifests_dir: Path,
    *,
    timeout_s: int = SWARM_TIMEOUT_S,
) -> list[FactorCandidate]:
    """Best-effort overwrite of ``economic_logic`` via swarm; fail-soft.

    Calls ``crypto_factor_lab`` with the deterministic candidate list as
    context and asks for an ``economic_logic`` rewrite per factor. Returns
    the (possibly enriched) candidate list. Any exception or unparseable
    output leaves the deterministic placeholder text in place and the
    pipeline continues normally — this function MUST NOT raise to its
    caller.

    Args:
        candidates:    The deterministic candidate list to enrich in-place
                       (returns a new list; inputs untouched).
        sym_name:      Short symbol name, e.g. "btc".
        okx_swap:      OKX swap ticker, e.g. "BTC-USDT-SWAP".
        cfg:           Loaded ResearchConfig.
        manifests_dir: Path to research/manifests/.
        timeout_s:     Swarm subprocess timeout in seconds.

    Returns:
        List of FactorCandidate. On full success, each candidate's
        ``economic_logic`` is swarm-authored; on any failure, the original
        deterministic candidates are returned unchanged.
    """
    if not candidates:
        return candidates

    try:
        vars_dict, _, _ = _build_swarm_vars(sym_name, okx_swap, cfg, manifests_dir)
        # Inject the deterministic candidate list so the swarm knows what to
        # explain (we ignore any candidates the swarm proposes — its job is
        # text enrichment only).
        vars_dict["preselected_candidates"] = json.dumps(
            [{"feature_key": c.feature_key, "name": c.name} for c in candidates],
            ensure_ascii=False,
        )
        vars_dict["extra_instruction"] = (
            "為以下 preselected_candidates 的每個 feature_key 寫一段經濟邏輯散文。"
            "回傳 JSON 陣列，每元素含 feature_key 與 economic_logic 兩欄。"
        )
        stdout = run_swarm(vars_dict, timeout=timeout_s)
        raw_enriched = parse_candidates_json(stdout)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[stage0] {sym_name}: enrichment swarm failed ({exc}); "
            "keeping deterministic placeholder text.",
            file=sys.stderr,
        )
        return list(candidates)

    # Build feature_key -> economic_logic map from swarm output.
    enrichment: dict[str, str] = {}
    for raw in raw_enriched if isinstance(raw_enriched, list) else []:
        if not isinstance(raw, dict):
            continue
        fk = raw.get("feature_key")
        logic = raw.get("economic_logic")
        if isinstance(fk, str) and isinstance(logic, str) and logic.strip():
            enrichment[fk] = logic.strip()

    if not enrichment:
        print(
            f"[stage0] {sym_name}: enrichment swarm returned no usable economic_logic; "
            "keeping deterministic placeholder text."
        )
        return list(candidates)

    out: list[FactorCandidate] = []
    matched = 0
    for c in candidates:
        new_logic = enrichment.get(c.feature_key or "")
        if new_logic:
            matched += 1
            out.append(c.model_copy(update={"economic_logic": new_logic}))
        else:
            out.append(c)
    print(
        f"[stage0] {sym_name}: enrichment overwrote economic_logic for "
        f"{matched}/{len(candidates)} candidate(s)."
    )
    return out


# ─── Per-symbol processing ───────────────────────────────────────────────────


def _build_swarm_vars(
    sym_name: str, okx_swap: str, cfg: ResearchConfig, manifests_dir: Path
) -> tuple[dict[str, str], list[str], list[str]]:
    """Build the user_vars dict for the crypto_factor_lab swarm.

    Args:
        sym_name:      Short lowercase symbol name, e.g. "eth".
        okx_swap:      OKX perpetual swap ticker, e.g. "ETH-USDT-SWAP".
        cfg:           Loaded ResearchConfig.
        manifests_dir: Path to research/manifests/ (for evidence/features paths).

    Returns:
        Tuple of (vars_dict, available_sources, available_transforms) where
        vars_dict is suitable for JSON serialisation into the CLI VARS_JSON arg,
        and the two lists are the same registries advertised to the swarm so
        filter_invalid_candidates uses identical data.
    """
    available_sources = [
        key
        for key, spec in SOURCE_REGISTRY.items()
        if spec.status == "available"
    ]
    available_transforms = list(TRANSFORM_REGISTRY.keys())

    vars_dict = {
        "target_universe": okx_swap,
        "signal_categories": "funding,basis,oi,momentum,volatility,stablecoin,whale,skew",
        "horizons_h": str(list(cfg.horizons_h)),
        "available_sources": ",".join(available_sources),
        "available_transforms": ",".join(available_transforms),
        "evidence_path": str(manifests_dir / f"evidence_{sym_name}.json"),
        "features_path": str(manifests_dir / f"features_{sym_name}.parquet"),
    }
    return vars_dict, available_sources, available_transforms


def _process_symbol(
    sym_name: str,
    okx_swap: str,
    cfg: ResearchConfig,
    manifests_dir: Path,
    *,
    use_swarm: bool,
    min_abs_ic: float,
    min_abs_ir: float,
    max_candidates: int,
) -> CandidatesCheckResult:
    """Run stage-0 discovery for one symbol (deterministic ± enrichment).

    Steps:
      1. Pre-flight: features/evidence parquet/json must exist.
      2. Cache hit check — return early if cached manifest is fresh.
      3. Deterministic selection via select_candidates_from_evidence.
      4. (Optional) swarm enrichment of economic_logic, fail-soft.
      5. Build CandidatesManifest and write candidates_<sym>.json.

    A 0-candidate result is *not* a failure: an empty manifest is written
    with a stdout warning, and the exit code stays 0. The only failure
    path is missing stage 0a outputs (caller's mistake, not pipeline
    overreach).

    Args:
        sym_name:       Short lowercase symbol name.
        okx_swap:       OKX swap ticker (only used by enrichment swarm).
        cfg:            Loaded ResearchConfig.
        manifests_dir:  Path to research/manifests/.
        use_swarm:      When True, run enrichment after deterministic selection.
        min_abs_ic:     Selector IC threshold.
        min_abs_ir:     Selector IR threshold.
        max_candidates: Hard cap on candidate count.

    Returns:
        CandidatesCheckResult for this symbol.
    """
    # ── 0. Pre-flight: verify stage0a outputs exist ───────────────────────────
    features_path = manifests_dir / f"features_{sym_name}.parquet"
    evidence_path = manifests_dir / f"evidence_{sym_name}.json"
    missing: list[str] = []
    if not features_path.exists():
        missing.append(str(features_path))
    if not evidence_path.exists():
        missing.append(str(evidence_path))
    if missing:
        print(
            f"{_RED}[stage0] {sym_name}: stage0a outputs missing — "
            f"run stage0a_features first. Missing: {missing}{_RESET}",
            file=sys.stderr,
        )
        return CandidatesCheckResult(
            symbol=sym_name,
            exists=False,
            valid=False,
            error="stage0a outputs missing — run stage0a_features first",
        )

    # ── 1. Cache check ────────────────────────────────────────────────────────
    if cache_hit(manifests_dir, sym_name, cfg.discovery_cache_days):
        print(f"[stage0] {sym_name}: cache hit, skipping discovery")
        path = manifests_dir / f"candidates_{sym_name}.json"
        try:
            raw = path.read_text(encoding="utf-8")
            manifest = CandidatesManifest.model_validate_json(raw)
            return CandidatesCheckResult(
                symbol=sym_name,
                exists=True,
                valid=True,
                n_candidates=len(manifest.candidates),
                from_cache=True,
            )
        except Exception as exc:  # noqa: BLE001
            # Cache file exists but is corrupt — fall through to re-run.
            print(
                f"[stage0] {sym_name}: cached file is corrupt ({exc}), "
                "re-running discovery."
            )

    # ── 2. Deterministic selection ────────────────────────────────────────────
    print(
        f"[stage0] {sym_name}: deterministic selection "
        f"(min_abs_ic={min_abs_ic}, min_abs_ir={min_abs_ir}, "
        f"max_candidates={max_candidates})"
    )
    candidates = run_deterministic_discovery(
        sym_name,
        evidence_path,
        features_path,
        min_abs_ic=min_abs_ic,
        min_abs_ir=min_abs_ir,
        max_candidates=max_candidates,
    )

    # ── 3. Optional swarm enrichment ──────────────────────────────────────────
    mode = "deterministic"
    if use_swarm and candidates:
        candidates = enrich_candidates_with_swarm(
            candidates, sym_name, okx_swap, cfg, manifests_dir
        )
        mode = "deterministic+swarm"

    # ── 4. Zero-candidate is a soft warning, not a hard failure ───────────────
    if not candidates:
        print(
            f"{_RED}[stage0] {sym_name}: 0 factors passed IC/IR threshold — "
            f"lower thresholds in research_config.yaml::stage0_selector or "
            f"expand feature pool.{_RESET}"
        )

    # ── 5. Write CandidatesManifest ───────────────────────────────────────────
    manifest = CandidatesManifest(
        symbol=sym_name,
        generated_at=datetime.now(timezone.utc),
        source_swarm_run=None,
        candidates=candidates,
    )

    out_path = manifests_dir / f"candidates_{sym_name}.json"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"[stage0] {sym_name}: wrote {len(candidates)} candidates ({mode}) "
        f"→ {out_path}"
    )

    return CandidatesCheckResult(
        symbol=sym_name,
        exists=True,
        valid=True,
        n_candidates=len(candidates),
    )


# ─── Main entry point ─────────────────────────────────────────────────────────


def _compute_manifests_dir(cfg: ResearchConfig, repo_root: Path) -> Path:
    """Return the manifests directory, derived from cfg.feature_store_path.

    For sub-hour intervals cfg.feature_store_path is already namespaced
    (e.g. 'research/manifests/15m'), so this correctly separates artifacts
    from 1H runs. Never hardcode 'research/manifests'.
    """
    return repo_root / cfg.feature_store_path


def _load_raw_config_yaml() -> dict[str, Any]:
    """Re-read research_config.yaml as a raw dict for stage0_selector overrides.

    ResearchConfig (the dataclass) doesn't carry the ``stage0_selector`` block
    today; rather than thread a new field through it, this helper reads the
    YAML again directly. The cost is cheap (single small file).

    Returns:
        Raw YAML mapping, or an empty dict if the file is missing or unreadable.
    """
    cfg_path = _CFG_REPO_ROOT / "research" / "research_config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # local import to avoid hard dep at module import time

        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(
            f"[stage0] warning: could not re-read research_config.yaml for "
            f"stage0_selector overrides ({exc}); using defaults.",
            file=sys.stderr,
        )
        return {}


def main() -> None:
    """Stage-0 entry point: discover factor candidates, verify, report, exit."""
    parser = argparse.ArgumentParser(description="Stage-0 factor discovery runner.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cache and re-run discovery for all symbols.",
    )
    swarm_group = parser.add_mutually_exclusive_group()
    swarm_group.add_argument(
        "--use-swarm",
        dest="use_swarm",
        action="store_true",
        help=(
            "Enable LLM swarm enrichment of economic_logic after deterministic "
            "selection. Failures are fail-soft (deterministic placeholder kept)."
        ),
    )
    swarm_group.add_argument(
        "--no-swarm",
        dest="use_swarm",
        action="store_false",
        help="Force deterministic-only mode (default).",
    )
    # Default depends on RESEARCH_STAGE0_USE_SWARM env var.
    env_use_swarm = os.environ.get("RESEARCH_STAGE0_USE_SWARM", "").strip()
    parser.set_defaults(use_swarm=env_use_swarm == "1")
    args = parser.parse_args()

    # RESEARCH_FORCE_DISCOVERY env var also forces re-run.
    force = args.force or bool(os.environ.get("RESEARCH_FORCE_DISCOVERY", ""))
    use_swarm: bool = bool(args.use_swarm)

    cfg: ResearchConfig = load_config()
    manifests_dir = _compute_manifests_dir(cfg, _CFG_REPO_ROOT)

    # Override cache TTL when --force / env var is set.
    effective_cache_days = 0 if force else cfg.discovery_cache_days
    effective_cfg = dataclasses.replace(cfg, discovery_cache_days=effective_cache_days)

    # Read selector thresholds from research_config.yaml::stage0_selector.
    raw_config = _load_raw_config_yaml()
    min_abs_ic, min_abs_ir, max_candidates = _read_stage0_selector_overrides(raw_config)

    print("=" * 60)
    print("Stage 0 — Factor Discovery")
    print("=" * 60)
    print(
        f"Config: period={cfg.period}d  horizons={list(cfg.horizons_h)}  "
        f"symbols={cfg.symbol_names()}"
    )
    print(
        f"Cache TTL: {effective_cache_days} days "
        f"({'disabled — force mode' if force else 'enabled'})"
    )
    print(
        f"Mode: {'deterministic+swarm enrichment' if use_swarm else 'deterministic only'}"
    )
    print(
        f"Selector: min_abs_ic={min_abs_ic}  min_abs_ir={min_abs_ir}  "
        f"max_candidates={max_candidates}"
    )
    print(f"Output directory: {manifests_dir}")

    results: list[CandidatesCheckResult] = []
    for sym_cfg in cfg.symbols:
        result = _process_symbol(
            sym_name=sym_cfg.name,
            okx_swap=sym_cfg.okx_swap,
            cfg=effective_cfg,
            manifests_dir=manifests_dir,
            use_swarm=use_swarm,
            min_abs_ic=min_abs_ic,
            min_abs_ir=min_abs_ir,
            max_candidates=max_candidates,
        )
        results.append(result)

    # Final verification pass (reads manifests from disk to double-check).
    verified = verify_outputs(manifests_dir, cfg.symbol_names())

    # Propagate from_cache flag from processing results to verify results.
    cache_flags = {r.symbol: r.from_cache for r in results}
    for vr in verified:
        vr.from_cache = cache_flags.get(vr.symbol, False)

    print_summary(verified)

    exit_code = compute_exit_code(verified)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
