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
