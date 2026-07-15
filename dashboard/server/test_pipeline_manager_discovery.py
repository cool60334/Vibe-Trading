import pipeline_jobs as pj
import pipeline_manager as pm


def _run_with_fake(tmp_path, job, rc_by_step):
    seen = {}
    def runner(repo_root, step_id, symbol, log_fp, *a, **k):
        seen[step_id] = k.get("config") or (a[-1] if a else None)
        return rc_by_step.get(step_id, 0)
    mgr = pm.Manager(tmp_path, runner=runner)
    mgr.execute_job(job)
    return pj.read_job(tmp_path, job["job_id"]), seen


def test_foundry_step_returning_201_marks_job_paused(tmp_path):
    job = pj.create_job(tmp_path, kind="foundry_mine", symbol="eth", config={})
    done, _ = _run_with_fake(tmp_path, job, {"foundry": pm.EXIT_PAUSED})
    assert done["status"] == "paused"


def test_bridge_failure_does_not_run_stages_but_soft_fail_zero_does(tmp_path):
    # bridge returns 0 (soft-fail path) → stages proceed
    job = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    done, seen = _run_with_fake(tmp_path, job, {})   # all zero
    assert done["status"] == "succeeded"
    assert "0a" in seen                              # stages ran after bridge


def test_foundry_mine_is_selected_after_discovery_pipeline(tmp_path):
    pj.create_job(tmp_path, kind="foundry_mine", symbol="eth", config={})
    dp = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    mgr = pm.Manager(tmp_path)
    assert mgr._oldest_queued()["job_id"] == dp["job_id"]   # pipeline first, mine last


def test_overlay_kept_on_failure_cleaned_on_success(tmp_path):
    job = pj.create_job(tmp_path, kind="discovery_pipeline", symbol="eth", config={})
    ov = pj.log_path(tmp_path, job["job_id"]).parent
    ov.mkdir(parents=True, exist_ok=True)
    (ov / "foundry_overlay_eth.parquet").write_bytes(b"x")
    def runner(repo_root, step_id, symbol, log_fp, *a, **k):
        return 0 if step_id == "bridge" else 1     # a stage fails
    pm.Manager(tmp_path, runner=runner).execute_job(job)
    assert (ov / "foundry_overlay_eth.parquet").exists()   # retained on failure
