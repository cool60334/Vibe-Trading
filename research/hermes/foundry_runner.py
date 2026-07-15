"""Talos Foundry runner CLI (design law 2: Talos writes decision files, a
separate runner picks them up; it never inline-runs a stage itself).

`enqueue` writes a job.json; `run` reconciles the queued jobs. The runner never
touches the network — it reads an OHLCV parquet, it does not fetch candles."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.hermes.forge import ForgeBudget
from research.hermes.foundry_spend import (
    AlreadyRunning, default_ledger_path, record_spend, single_instance_lock, todays_spend,
)
from research.hermes.llm_client import build_llm_coder, LLMUnavailable
from research.hermes.orchestrator import (
    _now, _write_job_json, enqueue_foundry_job, run_foundry_job)
from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed

log = logging.getLogger(__name__)

EXIT_PAUSED = 201


def _positive_int(s: str) -> int:
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {v}")
    return v


def resolve_image_id(tag: str) -> str:
    """Resolve a human-readable tag to its immutable content-addressed image id.
    A tag can be re-pointed under you between the run that vets a factor and the
    run that reproduces it; the image id cannot. Raises SandboxError (infra) if
    the tag does not resolve on this host, so a config typo aborts before the
    first container starts rather than burning every hypothesis's retries."""
    try:
        proc = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                              capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SandboxError(f"could not inspect image {tag!r}: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError(
            f"sandbox image {tag!r} does not resolve on this host: "
            f"{(proc.stderr or '').strip()[:200]}")
    return proc.stdout.strip()


def load_ohlcv(path) -> pd.DataFrame:
    """Read an OHLCV parquet. Never networks. Validates a `close` column and a
    tz-aware index so a misaligned/naive table fails loudly here rather than
    producing NaN forward returns deep inside the gate."""
    df = pd.read_parquet(path)
    if "close" not in df.columns:
        raise ValueError(f"ohlcv parquet {path} has no 'close' column")
    if getattr(df.index, "tz", None) is None:
        raise ValueError(f"ohlcv parquet {path} index is not tz-aware (need UTC)")
    return df


def _queued_jobs(runs_dir) -> list:
    jobs_root = Path(runs_dir) / "foundry_jobs"
    out = []
    if not jobs_root.exists():
        return out
    for job_path in jobs_root.rglob("job.json"):
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue                                   # a half-written peer; next pass gets it
        if job.get("status") == "queued":
            out.append((job.get("created_at", ""), job_path, job))
    out.sort(key=lambda t: t[0])                       # deterministic: created_at order
    return out


def reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir,
                           budget=None, batch_max_llm_calls=None, shared_budget=None,
                           pause_file=None) -> list:
    """Run every queued foundry job in created_at order under ONE shared LLM-call
    budget (write-file->reconcile).

    The shared ForgeBudget is charged before each llm.complete() (forge.py), so a
    job that fails mid-sweep still leaves its spend counted — the batch total is a
    hard cap, not a sum of returned summaries (a failed job returns none).

    Before starting job N+1 we project its cost as the mean spend of the jobs
    attempted so far (jobs_attempted, updated in a `finally` so a mid-sweep
    failure still counts) and refuse to start it if `used + avg_cost` would
    blow the cap. A bare `used >= max_llm_calls` check is not enough here: a
    job can legitimately land UNDER the cap (2 used / 3 max) while still
    being the last one that safely fits — starting one more of similar size
    would overshoot. This is a conservative estimate, not an exact one: a
    batch of jobs with wildly uneven cost can stop earlier than the hard cap
    strictly requires, but it never launches a job blind past the ceiling.

    Abort rules: a bare SandboxError (infra, not SandboxRunFailed) OR an
    LLMUnavailable (bad key / no quota) stops the whole batch, because every
    later job would hit the same wall. Any other job failure leaves that job
    status=failed and the batch continues.

    A failure *before* run_foundry_job is ever called — e.g. load_ohlcv
    raising ValueError on a malformed parquet — would otherwise never be
    persisted: run_foundry_job is the only thing that writes status="failed"
    to job.json, and it never runs in that case. Left alone, the job would
    sit at status="queued" forever and _queued_jobs would re-select it on
    every future reconcile pass, silently poisoning the queue with no
    operator-visible failure. So this function writes status="failed" for
    that job itself (mirroring run_foundry_job's own write) before moving on
    to the next job."""
    if shared_budget is not None:
        shared = shared_budget                       # caller owns it (reads .used after)
    elif batch_max_llm_calls is not None:
        shared = ForgeBudget(max_llm_calls=batch_max_llm_calls)
    else:
        shared = None
    jobs_attempted = 0                                  # only jobs that reached run_foundry_job
    summaries = []
    for _created, job_path, job in _queued_jobs(runs_dir):
        if shared is not None and jobs_attempted > 0:
            avg_cost = shared.used / jobs_attempted
            if shared.used + avg_cost > shared.max_llm_calls:
                log.warning(
                    "batch LLM-call budget projected to be spent (%d used, "
                    "avg %.1f/job over %d jobs, cap %d); stopping",
                    shared.used, avg_cost, jobs_attempted, shared.max_llm_calls)
                break
        try:
            ohlcv = load_ohlcv(job["params"]["ohlcv_path"])
        except Exception as exc:                       # noqa: BLE001 job-level, keep going
            log.warning("job %s failed to load ohlcv: %s; continuing", job_path, exc)
            job["status"] = "failed"; job["error"] = str(exc); job["finished_at"] = _now()
            _write_job_json(job_path, job)
            continue
        try:
            summaries.append(run_foundry_job(
                job_path, manifests_dir=manifests_dir, llm=llm, sandbox=sandbox,
                zoo_dir=zoo_dir, ohlcv=ohlcv, budget=budget, forge_budget=shared,
                pause_file=pause_file))
        except LLMUnavailable:
            log.error("LLM backend unavailable on %s; aborting the batch", job_path)
            raise                                      # infra: every later job hits the same wall
        except SandboxError as exc:
            if not isinstance(exc, SandboxRunFailed):
                log.error("infra fault on %s; aborting the batch: %s", job_path, exc)
                raise                                  # Layer A: stop the whole queue
            log.warning("job %s failed (repairable, buried): %s", job_path, exc)
        except Exception as exc:                       # noqa: BLE001 job-level, keep going
            log.warning("job %s failed: %s; continuing", job_path, exc)
        finally:
            if shared is not None:
                jobs_attempted += 1                     # count even a mid-sweep failure
    return summaries


def build_llm(spec: str, *, model: str | None = None, max_tokens: int = 2048):
    """Construct the LLM client named by `spec`. For a supported provider the
    model is pinned explicitly (a paid run must never inherit an expensive model
    from a stray LANGCHAIN_MODEL_NAME)."""
    if spec in ("openrouter", "openai"):
        if not model:
            raise ValueError(f"{spec} requires an explicit model (a paid run must "
                             "not read the model from the environment)")
        return build_llm_coder(provider=spec, model=model, max_tokens=max_tokens)
    raise ValueError(f"unknown llm spec {spec!r}")


def _add_run_args(p) -> None:
    """Flags shared by the `run` and `mine` subcommands."""
    p.add_argument("--runs-dir", required=True)
    p.add_argument("--manifests-dir", required=True)
    p.add_argument("--zoo-dir", required=True)
    p.add_argument("--image", required=True, help="sandbox image tag; resolved to an immutable id")
    p.add_argument("--llm", default="openrouter")
    p.add_argument("--timeout-s", type=int, default=120)
    p.add_argument("--model", required=True, help="explicit provider model id (pinned)")
    p.add_argument("--i-will-spend-real-money", action="store_true",
                   help="required to actually call the paid LLM; without it, run refuses")
    p.add_argument("--batch-max-llm-calls", type=_positive_int, default=6)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--daily-max-llm-calls", type=_positive_int, default=None,
                   help="cross-run UTC-day call ceiling; omit for no daily guard")
    p.add_argument("--spend-ledger", default=None,
                   help="spend ledger path (default <runs-dir>/foundry_spend.jsonl)")
    p.add_argument("--pause-file", default=None,
                   help="killswitch: if this file exists between hypotheses, the "
                        "sweep stops cleanly and the run exits with EXIT_PAUSED (201)")


def _execute_run(args) -> int:
    """Reconcile queued foundry jobs under the daily spend guard + single-instance
    lock. Shared by the `run` and `mine` subcommands."""
    if not args.i_will_spend_real_money:
        print("refusing: a real run spends money. Re-run with "
              "--i-will-spend-real-money once your provider key is set.")
        return 2

    ledger = args.spend_ledger or str(default_ledger_path(args.runs_dir))
    daily = args.daily_max_llm_calls
    try:
        with single_instance_lock(args.runs_dir):
            today = None
            effective = args.batch_max_llm_calls
            if daily is not None:
                today = datetime.now(timezone.utc).date()          # pinned once
                remaining = daily - todays_spend(ledger, today)
                if remaining <= 0:
                    print(f"refusing: daily LLM-call budget {daily} already spent "
                          f"today ({today.isoformat()}).")
                    return 2
                effective = min(args.batch_max_llm_calls, remaining)

            image_id = resolve_image_id(args.image)                # infra pre-check; raises to abort
            sandbox = DockerSandbox(image=image_id, timeout_s=args.timeout_s, allow_unpinned=False)
            llm = build_llm(args.llm, model=args.model, max_tokens=args.max_tokens)
            shared = ForgeBudget(max_llm_calls=effective)
            record_error = None
            summaries = None
            try:
                summaries = reconcile_foundry_jobs(args.runs_dir, args.manifests_dir, llm, sandbox,
                                                   args.zoo_dir, shared_budget=shared,
                                                   pause_file=args.pause_file)
            finally:
                if daily is not None:
                    try:
                        record_spend(ledger, day=today, calls=shared.used,
                                     tokens=getattr(llm, "total_tokens", 0))
                    except Exception as e:      # noqa: BLE001 - MUST NOT return/raise here:
                        log.error("daily guard compromised: spend not recorded: %s", e)
                        record_error = e         # that would mask reconcile's own traceback
            if record_error is not None:
                return 2
            print(f"foundry run complete. tokens used (reported): {getattr(llm, 'total_tokens', 0)}")
            paused = any(s.get("killswitch_paused") for s in (summaries or []) if isinstance(s, dict))
            return EXIT_PAUSED if paused else 0
    except AlreadyRunning as exc:
        print(f"refusing: {exc}")
        return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry", description="Talos Factor Foundry runner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("enqueue", help="write a queued foundry job.json")
    q.add_argument("--symbol", required=True)
    q.add_argument("--runs-dir", required=True)
    q.add_argument("--oos-start", required=True)
    q.add_argument("--ohlcv-path", required=True)
    q.add_argument("--interval", default="1H")
    q.add_argument("--horizon-h", type=int, default=24)

    r = sub.add_parser("run", help="reconcile queued foundry jobs")
    _add_run_args(r)

    # `mine` = enqueue-then-run for one symbol. `run` alone reconciles an EMPTY
    # queue and forges nothing, so the auto-scheduler's foundry step uses `mine`.
    m = sub.add_parser("mine", help="enqueue a foundry job for a symbol, then reconcile it")
    m.add_argument("--symbol", required=True)
    m.add_argument("--oos-start", required=True)
    m.add_argument("--ohlcv-path", required=True)
    m.add_argument("--interval", default="1H")
    m.add_argument("--horizon-h", type=int, default=24)
    _add_run_args(m)

    args = ap.parse_args(argv)
    if args.cmd == "enqueue":
        enqueue_foundry_job(args.symbol, args.runs_dir, {
            "oos_start": args.oos_start, "interval": args.interval,
            "horizon_h": args.horizon_h, "ohlcv_path": args.ohlcv_path})
        return 0

    if args.cmd == "mine":
        enqueue_foundry_job(args.symbol, args.runs_dir, {
            "oos_start": args.oos_start, "interval": args.interval,
            "horizon_h": args.horizon_h, "ohlcv_path": args.ohlcv_path})

    return _execute_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
