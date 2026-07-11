"""Docker-gated full-chain e2e: entry -> reconcile -> run_foundry -> forge ->
sandbox -> gatekeeper -> evidence card, against a REAL container.

Every prior hermes test mocked the sandbox boundary (FakePopen) or exercised a
single unit. This is the one test that proves the production path actually
wires together: enqueue_foundry_job (write-file), reconcile_foundry_jobs
(pick-up), run_foundry -> forge -> DockerSandbox.run (real `docker run`) ->
gatekeeper.evaluate -> EvidenceCard.

Skipped entirely (3 SKIP, not 3 FAIL) unless docker is up, the test image is
built, and the ohlcv fixture is committed -- see `_needs_docker` below. Never
touches the network: OHLCV comes from the committed fixture, features come
from the real (already-computed, on-disk) features_eth.parquet.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from research.hermes.foundry_runner import reconcile_foundry_jobs, resolve_image_id
from research.hermes.orchestrator import Budget, enqueue_foundry_job
from research.hermes.sandbox import DockerSandbox, is_docker_available
from research.lib.factor_io import load_features
from research.tests.hermes_support import (
    HUNT_DEADLINE_S, ScriptedLLM, SandboxContainerGuard, call_with_deadline,
)

SANDBOX_TEST_IMAGE = os.environ.get("TALOS_SANDBOX_TEST_IMAGE")
FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv_eth_1h.parquet"
# The real zoo dir, so build_queue finds hypotheses (the scripted LLM ignores
# the hypothesis body, but the queue must be non-empty for reconcile to do
# anything). Resolved relative to repo root -- tests run from there (CLAUDE.md).
_ZOO = Path("agent/src/factors/zoo")
# Real, on-disk, already-computed features -- NOT fetched. load_features()'s
# default manifests_dir resolves via research.lib.timeframe.active_manifests_dir(),
# which does a lazy `from lib.timeframe import ...` that only resolves when the
# caller has bootstrapped research/ onto sys.path (as the pipeline scripts do).
# This test doesn't, so pass manifests_dir explicitly -- the same pattern every
# other research test already uses (see test_hermes_orchestrator.py).
_REAL_MANIFESTS_DIR = Path(__file__).resolve().parents[1] / "manifests"

_needs_docker = pytest.mark.skipif(
    not is_docker_available() or not SANDBOX_TEST_IMAGE or not FIXTURE.exists(),
    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE + the ohlcv fixture")


def _stage(tmp_path):
    """Copy a pre-OOS features_eth slice + the ohlcv fixture into a fresh
    manifests dir, aligned so _align_ohlcv's 95% coverage check passes."""
    ohlcv = pd.read_parquet(FIXTURE)
    feats = load_features("eth", manifests_dir=_REAL_MANIFESTS_DIR).loc[
        ohlcv.index.min():ohlcv.index.max()]
    man = tmp_path / "manifests"; (man / "candidate_features").mkdir(parents=True)
    feats.to_parquet(man / "features_eth.parquet")
    op = tmp_path / "ohlcv_eth.parquet"; ohlcv.reindex(feats.index).to_parquet(op)
    return man, op


def _sandbox(timeout_s=90):
    return DockerSandbox(image=resolve_image_id(SANDBOX_TEST_IMAGE),
                         memory="512m", timeout_s=timeout_s, allow_unpinned=False)


def _enqueue(runs_dir, ohlcv_path):
    return enqueue_foundry_job("eth", runs_dir, {
        "oos_start": "2025-01-01", "interval": "1H", "horizon_h": 24,
        "ohlcv_path": str(ohlcv_path)})


@_needs_docker
def test_healthy_factor_runs_full_pipeline_and_marks_job_done(tmp_path):
    man, op = _stage(tmp_path)
    job_path = _enqueue(tmp_path, op)
    # every hypothesis gets the same legal factor; description is ignored by the
    # fake but MUST still reach it (asserted below).
    llm = ScriptedLLM(["def compute(df):\n    return df['close'].pct_change(20)"])
    summaries = reconcile_foundry_jobs(tmp_path, man, llm, _sandbox(), zoo_dir=_ZOO)
    assert summaries, "no job ran"
    s = summaries[0]
    # engineering facts only -- we do NOT assert a factor passed the gate
    assert s["candidate"] + s["rejected"] + s["forge_failed"] >= 1
    assert "queue_composition" in s and "features_range" in s
    job = json.loads(Path(job_path).read_text())
    assert job["status"] == "done"
    assert llm.prompts and "compute" in llm.prompts[0]      # the real prompt was built


@_needs_docker
def test_bad_code_is_repaired_from_container_stderr(tmp_path):
    man, op = _stage(tmp_path)
    _enqueue(tmp_path, op)
    llm = ScriptedLLM([
        "def compute(df):\n    return df['nonexistent_col']",     # KeyError in-container
        "def compute(df):\n    return df['close'].pct_change(5)",  # repaired
    ])
    reconcile_foundry_jobs(tmp_path, man, llm, _sandbox(), zoo_dir=_ZOO)
    assert len(llm.prompts) >= 2
    assert "KeyError" in llm.prompts[1] or "nonexistent" in llm.prompts[1]  # stderr fed back


@_needs_docker
def test_infinite_loop_times_out_then_short_circuits(tmp_path):
    man, op = _stage(tmp_path)
    _enqueue(tmp_path, op)
    llm = ScriptedLLM(["def compute(df):\n    while True:\n        pass"])   # same code every retry
    # Measured empirically: one real container timeout (timeout_s=5) costs
    # ~5.8s wall clock (5s wait + docker kill + reap verification). forge()'s
    # seen_shas dedup means a SECOND attempt with identical code never spins up
    # a second container -- so one hypothesis costs exactly one real timeout.
    # Cap the sweep at that one hypothesis (max_factors=1): the default
    # Budget(early_stop_after=8) would otherwise burn 8 real timeouts
    # (~46s) against every hypothesis in the real zoo queue giving the SAME
    # infinite-loop code, comfortably exceeding HUNT_DEADLINE_S even though
    # nothing is actually hung -- that would make this a test of wall-clock
    # budgeting, not of the timeout/short-circuit/reap behaviour it's meant
    # to prove.
    budget = Budget(max_factors=1)
    with SandboxContainerGuard() as guard:
        thread, box = call_with_deadline(
            lambda: reconcile_foundry_jobs(tmp_path, man, llm, _sandbox(timeout_s=5),
                                           zoo_dir=_ZOO, budget=budget))
        assert not thread.is_alive(), f"reconcile hung past {HUNT_DEADLINE_S}s"
        assert box.get("exc") is None, f"reconcile raised unexpectedly: {box!r}"
        assert guard.new() == [], "container survived the timeout"
