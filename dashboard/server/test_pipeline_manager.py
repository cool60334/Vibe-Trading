"""Tests for dashboard/server/pipeline_manager.py (global-serial executor)."""
from __future__ import annotations

import io

import pipeline_jobs as pj
import pipeline_manager as pm
from pipeline_manager import Manager


def _runner(codes):
    """Injectable runner: records (stage, symbol, stress), returns codes.get(stage, 0).

    Also records per-stage interval in ``run.intervals`` so interval-threading
    tests can assert without changing the ``calls`` tuple shape other tests use.
    """
    calls = []
    intervals = []

    def run(repo_root, stage_id, symbol, fp, stress=False, interval="1H", live_refresh=False,
            config=None):
        calls.append((stage_id, symbol, stress))
        intervals.append((stage_id, interval))
        fp.write(f"ran {stage_id} symbol={symbol} stress={stress} interval={interval}\n")
        return codes.get(stage_id, 0)

    run.calls = calls
    run.intervals = intervals
    return run


def _stages(calls):
    return [c[0] for c in calls]


def test_stage_job_runs_one_step_succeeds(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "succeeded"
    assert out["steps"][0]["status"] == "succeeded"
    assert _stages(r.calls) == ["1"]
    assert "ran 1" in pj.tail_log(tmp_path, job["job_id"])


def test_pipeline_job_runs_steps_in_order(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "succeeded"
    assert _stages(r.calls) == pj.pipeline_sequence()


def test_failing_step_stops_chain(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    r = _runner({"1": 2})   # stage 1 exits non-zero
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "failed"
    assert out["exit_code"] == 2
    assert _stages(r.calls) == ["0a", "0", "1"]   # stopped after the failure
    assert out["steps"][2]["status"] == "failed"


def test_cancel_flag_stops_before_next_step(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline")
    job["cancel"] = True
    pj.write_job(tmp_path, job)
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "canceled"
    assert _stages(r.calls) == []   # never started any step


def test_oldest_queued_picked_first(tmp_path):
    a = pj.create_job(tmp_path, kind="stage", stage="0a")
    b = pj.create_job(tmp_path, kind="stage", stage="1")
    a["created_at"] = "2026-01-01T00:00:00+00:00"
    b["created_at"] = "2026-01-02T00:00:00+00:00"
    pj.write_job(tmp_path, a)
    pj.write_job(tmp_path, b)
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert pj.read_job(tmp_path, a["job_id"])["status"] == "succeeded"
    assert pj.read_job(tmp_path, b["job_id"])["status"] == "queued"  # still waiting


def test_reconcile_startup_marks_stale_running_failed(tmp_path):
    job = pj.create_job(tmp_path, kind="stage", stage="1")
    job["status"] = "running"
    pj.write_job(tmp_path, job)
    Manager(tmp_path, runner=_runner({})).reconcile_startup()
    out = pj.read_job(tmp_path, job["job_id"])
    assert out["status"] == "failed"
    assert "interrupted" in out["error"]


def test_pipeline_job_with_symbol_filters_only_aware_stages(tmp_path):
    job = pj.create_job(tmp_path, kind="pipeline", symbol="btc")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    by_stage = {c[0]: c[1] for c in r.calls}
    # symbol-aware stages get "btc"; 2b/5 get None (global)
    assert by_stage["0a"] == "btc"
    assert by_stage["3"] == "btc"
    assert by_stage["4"] == "btc"
    assert by_stage["2b"] is None
    assert by_stage["5"] is None


def test_stage_job_with_symbol_passes_it(tmp_path):
    pj.create_job(tmp_path, kind="stage", stage="3", symbol="btc")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert r.calls == [("3", "btc", False)]


def test_job_without_symbol_passes_none(tmp_path):
    pj.create_job(tmp_path, kind="stage", stage="3")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert r.calls == [("3", None, False)]


def test_stage3_stress_job_threads_stress_true(tmp_path):
    pj.create_job(tmp_path, kind="stage", stage="3", symbol="eth", stress=True)
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert r.calls == [("3", "eth", True)]


def test_non_stress_job_threads_stress_false(tmp_path):
    pj.create_job(tmp_path, kind="stage", stage="3", symbol="eth")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert r.calls == [("3", "eth", False)]


def test_stage_job_threads_interval(tmp_path):
    pj.create_job(tmp_path, kind="stage", stage="1", interval="30m")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert r.intervals == [("1", "30m")]


def test_pipeline_job_threads_interval_to_every_step(tmp_path):
    pj.create_job(tmp_path, kind="pipeline", interval="15m")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert all(iv == "15m" for _, iv in r.intervals)


def test_job_without_interval_defaults_1h(tmp_path):
    pj.create_job(tmp_path, kind="stage", stage="1")
    r = _runner({})
    Manager(tmp_path, runner=r).scan_once()
    assert r.intervals == [("1", "1H")]


# ---------------------------------------------------------------------------
# _default_runner argv tests
# ---------------------------------------------------------------------------

def _capture_argv(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env", {})

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    return captured


def test_default_runner_stage3_stress_appends_flag(tmp_path, monkeypatch):
    captured = _capture_argv(monkeypatch)
    pm._default_runner(tmp_path, "3", "eth", io.StringIO(), stress=True)
    assert "--stress" in captured["argv"]


def test_default_runner_stage3_no_stress_omits_flag(tmp_path, monkeypatch):
    captured = _capture_argv(monkeypatch)
    pm._default_runner(tmp_path, "3", "eth", io.StringIO(), stress=False)
    assert "--stress" not in captured["argv"]


def test_default_runner_non_stage3_stress_omits_flag(tmp_path, monkeypatch):
    captured = _capture_argv(monkeypatch)
    pm._default_runner(tmp_path, "1", "eth", io.StringIO(), stress=True)
    assert "--stress" not in captured["argv"]


def test_default_runner_sub_hour_interval_sets_env(tmp_path, monkeypatch):
    captured = _capture_argv(monkeypatch)
    pm._default_runner(tmp_path, "1", "eth", io.StringIO(), interval="30m")
    assert captured["env"].get("RESEARCH_INTERVAL") == "30m"


def test_default_runner_1h_interval_omits_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "stale")  # must be scrubbed
    captured = _capture_argv(monkeypatch)
    pm._default_runner(tmp_path, "1", "eth", io.StringIO(), interval="1H")
    assert "RESEARCH_INTERVAL" not in captured["env"]


def test_oldest_queued_prioritizes_live_refresh(tmp_path):
    import pipeline_jobs as pj
    from pipeline_manager import Manager
    # research job created FIRST (older), live_refresh SECOND (newer)
    pj.create_job(tmp_path, kind="pipeline", symbol="sol")
    live = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    mgr = Manager(tmp_path, runner=lambda *a, **k: 0)
    picked = mgr._oldest_queued()
    assert picked["job_id"] == live["job_id"]


def test_live_refresh_sets_env_on_0a_step(tmp_path):
    import pipeline_jobs as pj
    from pipeline_manager import Manager
    seen = {}

    def fake_runner(repo_root, stage_id, symbol, log_fp, stress=False,
                    interval="1H", live_refresh=False, config=None):
        seen[stage_id] = live_refresh
        return 0

    job = pj.create_job(tmp_path, kind="live_refresh", symbol="sol")
    mgr = Manager(tmp_path, runner=fake_runner)
    mgr.execute_job(job)
    assert seen.get("0a") is True
    assert seen.get("1") is False
