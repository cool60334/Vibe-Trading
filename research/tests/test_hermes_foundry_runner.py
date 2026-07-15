from pathlib import Path

import pytest


def test_scripted_llm_wraps_bodies_in_dirty_markdown_and_records_prompts():
    from research.tests.hermes_support import ScriptedLLM
    from research.hermes.forge import extract_code
    llm = ScriptedLLM(["def compute(df):\n    return df['close']"])
    out = llm.complete("PROMPT-A")
    assert "```" in out and "Certainly" in out            # dirty: fence + preamble
    assert extract_code(out) == "def compute(df):\n    return df['close']"
    assert llm.prompts == ["PROMPT-A"]


def test_resolve_image_id_returns_immutable_id(monkeypatch):
    import subprocess
    from research.hermes.foundry_runner import resolve_image_id
    def fake_run(cmd, **k):
        assert cmd[:4] == ["docker", "image", "inspect", "talos-sandbox:test"]
        return subprocess.CompletedProcess(cmd, 0, stdout="sha256:" + "e" * 64 + "\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_image_id("talos-sandbox:test") == "sha256:" + "e" * 64

def test_resolve_image_id_raises_on_unresolvable(monkeypatch):
    import subprocess
    from research.hermes.foundry_runner import resolve_image_id
    from research.hermes.sandbox import SandboxError
    monkeypatch.setattr(subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such image"))
    with pytest.raises(SandboxError, match="does not resolve|No such image"):
        resolve_image_id("nope:zzz")

def test_load_ohlcv_reads_close_and_utc_index(tmp_path):
    import pandas as pd
    from research.hermes.foundry_runner import load_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    p = tmp_path / "o.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(p)
    got = load_ohlcv(p)
    assert "close" in got.columns and got.index.tz is not None

def test_load_ohlcv_rejects_missing_close(tmp_path):
    import pandas as pd
    from research.hermes.foundry_runner import load_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    p = tmp_path / "o.parquet"
    pd.DataFrame({"volume": [1.0, 2, 3]}, index=idx).to_parquet(p)
    with pytest.raises(ValueError, match="close"):
        load_ohlcv(p)


def _queue_job(tmp_path, symbol, ohlcv_path, created_at):
    from research.hermes.orchestrator import enqueue_foundry_job
    p = enqueue_foundry_job(symbol, tmp_path, {
        "oos_start": "2025-01-01", "interval": "1H", "horizon_h": 24,
        "ohlcv_path": str(ohlcv_path)})
    import json
    job = json.loads(Path(p).read_text()); job["created_at"] = created_at
    Path(p).write_text(json.dumps(job)); return p

def test_jobs_run_in_created_at_order(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-02T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-01T00:00:00+00:00")
    seen = []
    def fake_run_job(job_path, **k):
        import json; seen.append(json.loads(Path(job_path).read_text())["symbol"])
        return {"candidate": 0}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert seen == ["btc", "eth"]                      # earlier created_at first

def test_infra_error_aborts_whole_queue(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.sandbox import SandboxError
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        if sym == "eth":
            raise SandboxError("docker image does not resolve")     # infra
        return {"candidate": 0}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    with pytest.raises(SandboxError):
        fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["eth"]                              # btc never ran

def test_job_level_failure_continues_queue(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        if sym == "eth":
            raise ValueError("bad factor")             # NOT infra
        return {"candidate": 1}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["eth", "btc"]                       # btc still ran despite eth failing

def test_bad_ohlcv_path_is_job_level_not_infra(tmp_path, monkeypatch):
    import json
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    bad_op = tmp_path / "bad.parquet"
    pd.DataFrame({"volume": [1.0, 2, 3]}, index=idx).to_parquet(bad_op)   # missing 'close'
    good_op = tmp_path / "good.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(good_op)
    eth_job_path = _queue_job(tmp_path, "eth", bad_op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", good_op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        return {"candidate": 1}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["btc"]                               # eth's bad ohlcv never reached run_foundry_job; btc still ran
    # the poison job must not be left at status="queued" forever -- reconcile
    # itself has to mark it failed since run_foundry_job (the only other
    # writer of status=failed) was never called for it.
    eth_job = json.loads(Path(eth_job_path).read_text())
    assert eth_job["status"] == "failed"
    assert "close" in eth_job["error"]
    assert "finished_at" in eth_job


def test_build_llm_openrouter_returns_a_coder(monkeypatch):
    from research.hermes import foundry_runner as fr
    sentinel = object()
    monkeypatch.setattr(fr, "build_llm_coder",
                        lambda *, provider, model, max_tokens: sentinel)
    assert fr.build_llm("openrouter", model="x/y", max_tokens=1000) is sentinel


def test_build_llm_openrouter_requires_a_model():
    from research.hermes.foundry_runner import build_llm
    with pytest.raises(ValueError, match="model"):
        build_llm("openrouter", model=None)


def test_batch_cap_is_a_shared_counter_across_jobs(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        forge_budget.charge_call(); forge_budget.charge_call()   # each job spends 2
        return {"candidate": 0, "llm_calls_used": forge_budget.used}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                              zoo_dir=tmp_path, batch_max_llm_calls=3)
    assert ran == ["eth"]          # eth spends 2; btc would exceed 3 -> not started


def test_batch_cap_holds_when_a_job_raises_mid_sweep(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        forge_budget.charge_call(); forge_budget.charge_call()
        raise ValueError("job blew up AFTER spending its calls")     # non-infra
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                              zoo_dir=tmp_path, batch_max_llm_calls=3)
    assert ran == ["eth"]          # eth's 2 calls still counted -> btc not started


def test_batch_max_llm_calls_zero_means_spend_nothing(tmp_path, monkeypatch):
    # Regression: batch_max_llm_calls=0 used to be treated the same as None
    # (Python truthiness), which meant "0" silently disabled the shared cap
    # instead of enforcing a zero-spend batch. It must build a real
    # ForgeBudget(max_llm_calls=0) whose very first charge_call() raises
    # BudgetExhausted -- a generic (non-infra, non-LLMUnavailable) exception,
    # so reconcile buries it as a job-level failure and the batch continues,
    # exactly like test_job_level_failure_continues_queue's non-infra case.
    #
    # Asserting only on ran/summaries doesn't distinguish the fix from the old
    # bug: under the old code batch_max_llm_calls=0 fell through to
    # forge_budget=None, and fake_run_job's forge_budget.charge_call() call
    # then raised AttributeError on None -- buried by the same except Exception
    # -- producing the identical ran == ["eth"] / summaries == [] outcome. So
    # assert directly on the forge_budget object handed to run_foundry_job:
    # it must be a real ForgeBudget(max_llm_calls=0), not None.
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.forge import ForgeBudget
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    ran = []
    captured = {}
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        captured["forge_budget"] = forge_budget
        forge_budget.charge_call()          # must raise before any spend happens
        return {"candidate": 0}             # never reached if the fix holds
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    summaries = fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                                          zoo_dir=tmp_path, batch_max_llm_calls=0)
    assert ran == ["eth"]           # job was entered (to prove it wasn't just skipped)
    assert summaries == []          # charge_call() raised before run_foundry_job could return
                                     # a summary -- proving no spend happened past the cap
    assert isinstance(captured["forge_budget"], ForgeBudget)   # not None -- the actual regression
    assert captured["forge_budget"].max_llm_calls == 0


def test_llm_unavailable_aborts_whole_batch(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.llm_client import LLMUnavailable
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        raise LLMUnavailable("bad api key")
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    with pytest.raises(LLMUnavailable):
        fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                                  zoo_dir=tmp_path, batch_max_llm_calls=99)
    assert ran == ["eth"]          # btc never ran


def test_main_enqueue_writes_a_queued_job(tmp_path):
    import json
    from research.hermes.foundry_runner import main
    rc = main(["enqueue", "--symbol", "eth", "--runs-dir", str(tmp_path),
               "--oos-start", "2025-01-01", "--ohlcv-path", str(tmp_path / "o.parquet")])
    assert rc == 0
    jobs = list((tmp_path / "foundry_jobs").rglob("job.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text())
    assert job["status"] == "queued" and job["symbol"] == "eth"
    assert job["params"]["oos_start"] == "2025-01-01"


def test_run_without_spend_flag_refuses_and_never_builds_llm(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    called = {"build": False}
    monkeypatch.setattr(fr, "build_llm", lambda *a, **k: called.__setitem__("build", True))
    rc = fr.main(["run", "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
                  "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
                  "--model", "x/y"])            # no --i-will-spend-real-money
    assert rc != 0                              # refused
    assert called["build"] is False            # never built the client


def test_run_with_spend_flag_builds_llm_and_reconciles(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    calls = {}
    monkeypatch.setattr(fr, "resolve_image_id", lambda tag: "sha256:" + "e" * 64)
    monkeypatch.setattr(fr, "DockerSandbox", lambda **k: object())
    fake_coder = object()
    monkeypatch.setattr(fr, "build_llm", lambda *a, **k: (calls.__setitem__("model", k.get("model")), fake_coder)[1])
    def fake_reconcile(runs_dir, manifests_dir, llm, sandbox, zoo_dir, shared_budget=None, **k):
        # Task 3 wires main to build its own ForgeBudget and hand it to reconcile
        # as shared_budget= (never a raw batch_max_llm_calls kwarg) -- see
        # test_reconcile_uses_a_passed_shared_budget for the reconcile-side contract.
        calls["reconciled"] = True; calls["batch"] = shared_budget.max_llm_calls; return []
    monkeypatch.setattr(fr, "reconcile_foundry_jobs", fake_reconcile)
    rc = fr.main(["run", "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
                  "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
                  "--model", "deepseek/deepseek-chat", "--i-will-spend-real-money",
                  "--batch-max-llm-calls", "4"])
    assert rc == 0 and calls["reconciled"] is True
    assert calls["model"] == "deepseek/deepseek-chat" and calls["batch"] == 4


def test_build_llm_openai_returns_a_coder(monkeypatch):
    from research.hermes import foundry_runner as fr
    sentinel = object()
    seen = {}
    def fake_factory(*, provider, model, max_tokens):
        seen.update(provider=provider, model=model); return sentinel
    monkeypatch.setattr(fr, "build_llm_coder", fake_factory)
    assert fr.build_llm("openai", model="gpt-4o-mini") is sentinel
    assert seen == {"provider": "openai", "model": "gpt-4o-mini"}


def test_build_llm_openrouter_still_dispatches(monkeypatch):
    from research.hermes import foundry_runner as fr
    seen = {}
    monkeypatch.setattr(fr, "build_llm_coder",
                        lambda *, provider, model, max_tokens: seen.update(provider=provider))
    fr.build_llm("openrouter", model="x/y")
    assert seen["provider"] == "openrouter"


def test_build_llm_openai_requires_a_model():
    from research.hermes.foundry_runner import build_llm
    with pytest.raises(ValueError, match="model"):
        build_llm("openai", model=None)


def test_reconcile_uses_a_passed_shared_budget(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.forge import ForgeBudget
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    def fake_run_job(job_path, *, forge_budget=None, **k):
        forge_budget.charge_call(); forge_budget.charge_call()   # spend 2 on the shared obj
        return {"candidate": 0, "llm_calls_used": forge_budget.used}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    shared = ForgeBudget(max_llm_calls=5)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                              zoo_dir=tmp_path, shared_budget=shared)
    assert shared.used == 2          # main can read the same object afterwards


def _run_args(tmp_path, **extra):
    a = ["run", "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
         "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
         "--model", "gpt-4o-mini", "--i-will-spend-real-money"]
    for k, v in extra.items():
        a += [f"--{k}", str(v)]
    return a

def _stub_run_deps(monkeypatch, fr, reconcile):
    monkeypatch.setattr(fr, "resolve_image_id", lambda tag: "sha256:" + "e" * 64)
    monkeypatch.setattr(fr, "DockerSandbox", lambda **k: object())
    monkeypatch.setattr(fr, "build_llm", lambda *a, **k: type("C", (), {"total_tokens": 0})())
    monkeypatch.setattr(fr, "reconcile_foundry_jobs", reconcile)


def test_mine_enqueues_a_job_before_running(tmp_path, monkeypatch):
    # The auto-scheduler's foundry step runs `mine`, which must ENQUEUE a foundry
    # job for the symbol and THEN reconcile it. Plain `run` on an empty queue
    # forges nothing (the bug this fixes).
    import json
    from research.hermes import foundry_runner as fr
    called = {"reconcile": 0}
    _stub_run_deps(monkeypatch, fr,
                   lambda *a, **k: (called.__setitem__("reconcile", called["reconcile"] + 1) or []))
    rc = fr.main(["mine", "--symbol", "eth",
                  "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
                  "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
                  "--model", "gpt-4o-mini", "--oos-start", "2025-01-01",
                  "--ohlcv-path", str(tmp_path / "ohlcv_eth.parquet"),
                  "--i-will-spend-real-money"])
    assert rc == 0
    assert called["reconcile"] == 1                       # ran AFTER enqueue
    jobs = list((tmp_path / "foundry_jobs").rglob("job.json"))
    assert len(jobs) == 1                                 # exactly one job enqueued
    j = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert j["symbol"] == "eth"
    assert j["params"]["oos_start"] == "2025-01-01"
    assert j["params"]["ohlcv_path"].endswith("ohlcv_eth.parquet")


def test_remaining_zero_refuses_and_skips_reconcile(tmp_path, monkeypatch):
    import json
    from research.hermes import foundry_runner as fr
    (tmp_path / "foundry_spend.jsonl").write_text(
        json.dumps({"date": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat(), "calls": 40}) + "\n")
    called = {"reconcile": False}
    _stub_run_deps(monkeypatch, fr, lambda *a, **k: called.__setitem__("reconcile", True))
    rc = fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert rc == 2 and called["reconcile"] is False


def test_effective_shrinks_to_remaining(tmp_path, monkeypatch):
    import json
    from research.hermes import foundry_runner as fr
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    (tmp_path / "foundry_spend.jsonl").write_text(json.dumps({"date": today, "calls": 37}) + "\n")
    seen = {}
    def reconcile(*a, shared_budget=None, **k): seen["cap"] = shared_budget.max_llm_calls
    _stub_run_deps(monkeypatch, fr, reconcile)
    fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert seen["cap"] == 3          # min(6, 40-37)


def test_spend_recorded_even_when_reconcile_aborts(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    from research.hermes.llm_client import LLMUnavailable
    from research.hermes.foundry_spend import todays_spend
    from datetime import datetime, timezone
    def reconcile(*a, shared_budget=None, **k):
        shared_budget.charge_call(); shared_budget.charge_call()
        raise LLMUnavailable("bad key")
    _stub_run_deps(monkeypatch, fr, reconcile)
    with pytest.raises(LLMUnavailable):           # reconcile's exception is NOT masked
        fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert todays_spend(tmp_path / "foundry_spend.jsonl",
                        datetime.now(timezone.utc).date()) == 2   # spend still recorded


def test_argparse_rejects_nonpositive_budgets(tmp_path):
    from research.hermes import foundry_runner as fr
    with pytest.raises(SystemExit):
        fr.main(_run_args(tmp_path, **{"batch-max-llm-calls": 0}))
    with pytest.raises(SystemExit):
        fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 0}))


def test_daily_omitted_skips_the_guard(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    seen = {"reconciled": False}
    _stub_run_deps(monkeypatch, fr,
                   lambda *a, **k: seen.__setitem__("reconciled", True))
    rc = fr.main(_run_args(tmp_path, **{"batch-max-llm-calls": 6}))   # no --daily
    assert rc == 0 and seen["reconciled"] is True
    assert not (tmp_path / "foundry_spend.jsonl").exists()   # no ledger touched


def test_record_failure_returns_nonzero_when_reconcile_succeeded(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    _stub_run_deps(monkeypatch, fr, lambda *a, **k: None)     # reconcile succeeds
    monkeypatch.setattr(fr, "record_spend",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rc = fr.main(_run_args(tmp_path, **{"daily-max-llm-calls": 40, "batch-max-llm-calls": 6}))
    assert rc == 2
