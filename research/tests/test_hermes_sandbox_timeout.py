import logging
import os
import subprocess

import pytest
from research.hermes.sandbox import (
    DockerSandbox,
    SandboxError,
    SandboxRunFailed,
    is_docker_available,
)
from research.tests.hermes_support import (
    HUNT_DEADLINE_S,
    FakePopen,
    SandboxContainerGuard,
    call_with_deadline,
)

SANDBOX_TEST_IMAGE = os.environ.get("TALOS_SANDBOX_TEST_IMAGE")
SAFE_SOURCE = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
SPIN_SOURCE = "import pandas as pd\ndef compute(df):\n    while True:\n        pass\n"

_needs_docker = pytest.mark.skipif(
    not is_docker_available() or not SANDBOX_TEST_IMAGE,
    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE")


def _recording_docker(log: list, ps_stdout: str = ""):
    """Stand-in for subprocess.run over the docker CLI (reap + liveness check)."""
    def fake_run(cmd, **_kw):
        log.append(" ".join(cmd[:2]))
        out = ps_stdout if cmd[:2] == ["docker", "ps"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return fake_run


def test_timeout_reaps_the_container_before_draining_the_cli(monkeypatch, tmp_path):
    """Order is load-bearing, not incidental.

    Draining the CLI's pipes while the container still runs is what wedged the
    sweep: the container keeps the pipe open, so the drain never returns. Reap
    the container first -- that makes the CLI exit and the pipes close.
    """
    log: list = []
    fake = FakePopen(timeout_on_first_communicate=True, log=log)
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(subprocess, "run", _recording_docker(log))

    sb = DockerSandbox(memory="256m", timeout_s=1, allow_unpinned=True)
    with pytest.raises(SandboxRunFailed, match="timed out"):
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))

    # "docker image" is the Task 2 pre-check (`_ensure_image`), run once before
    # the container ever starts; everything after it is the existing reap order.
    assert log == ["docker image", "communicate#1", "docker kill", "docker rm", "docker ps",
                   "communicate#2"], log
    assert not fake.killed, "killed the docker CLI; the container would have survived it"


def test_interrupt_while_waiting_reaps_the_container(monkeypatch, tmp_path):
    """Ctrl-C mid-sweep must not leave the container running.

    agy二審 finding 1, checked against CPython: `Popen.__exit__` calls
    `self.wait()` with no timeout for every exception type but KeyboardInterrupt,
    and on KeyboardInterrupt it waits a moment and returns without killing
    anything. Either way the container outlives the interpreter unless we reap it
    ourselves -- and an unreaped container means `__exit__`'s wait() is the next
    unbounded block. stdlib's own subprocess.run() has the same catch-all.
    """
    log: list = []
    fake = FakePopen(first_exception=KeyboardInterrupt(), log=log)
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(subprocess, "run", _recording_docker(log))

    sb = DockerSandbox(memory="256m", timeout_s=30, allow_unpinned=True)
    with pytest.raises(KeyboardInterrupt):
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))

    assert "docker kill" in log, f"container left running on interrupt: {log}"
    assert fake.killed, "docker CLI left running on interrupt"


def test_surviving_container_is_reported_not_silently_swallowed(monkeypatch, tmp_path, caplog):
    """agy二審 finding 2: `_reap` swallowing every error hides a live orphan.

    If `docker kill` fails (wedged daemon), the container keeps burning a core
    with nothing in the logs to explain it. Verify the reap instead of assuming
    it worked -- the exact failure mode this whole session keeps finding.
    """
    fake = FakePopen(timeout_on_first_communicate=True)
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    # `docker ps` still lists it: the kill did not take
    monkeypatch.setattr(subprocess, "run", _recording_docker([], ps_stdout="deadbeef\n"))

    sb = DockerSandbox(memory="256m", timeout_s=1, allow_unpinned=True)
    with caplog.at_level(logging.WARNING, logger="research.hermes.sandbox"):
        with pytest.raises(SandboxRunFailed, match="SURVIVED the reap"):
            sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert any("orphan" in r.message.lower() for r in caplog.records), caplog.text


def test_reaped_container_is_not_reported_as_an_orphan(monkeypatch, tmp_path):
    """The happy reap: `docker ps` comes back empty, so no false alarm."""
    fake = FakePopen(timeout_on_first_communicate=True)
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(subprocess, "run", _recording_docker([], ps_stdout=""))

    sb = DockerSandbox(memory="256m", timeout_s=1, allow_unpinned=True)
    with pytest.raises(SandboxRunFailed, match="reaped"):
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))


def test_wedged_cli_is_killed_rather_than_hanging_the_drain(monkeypatch, tmp_path):
    """If even the post-reap drain times out, give up on stderr and kill the CLI.

    Otherwise the fallback path reintroduces the very unbounded wait this whole
    change exists to remove.
    """
    fake = FakePopen(timeout_on_first_communicate=True, drain_hangs=True)
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(subprocess, "run", _recording_docker([]))

    sb = DockerSandbox(memory="256m", timeout_s=1, allow_unpinned=True)
    with pytest.raises(SandboxRunFailed, match="timed out"):
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert fake.killed          # last resort, and only after the container is already dead


# ── real container: the timeout must bound the CALL, not describe an intention ─
#
# The mocks above prove the kill LOGIC. They cannot prove the timeout FIRES:
# `subprocess.run(timeout=)` kills the docker CLI client and then drains its
# pipes with NO timeout at all (CPython Lib/subprocess.py, `_mswindows` branch).
# The container is a child of dockerd, not of the CLI, so it kept running, kept
# the pipe open, and the unbounded drain never returned -- the TimeoutExpired
# handler that reaps the container never executed. A mock that patches
# subprocess.run away cannot see any of that. Only a real container can.

@_needs_docker
def test_timeout_hunt_returns_within_a_bounded_wall_clock(tmp_path):
    """A `while True` in the container must surface as SandboxRunFailed promptly."""
    import pandas as pd
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    sb = DockerSandbox(image=SANDBOX_TEST_IMAGE, memory="256m", timeout_s=5,
                       allow_unpinned=True)

    with SandboxContainerGuard() as guard:
        thread, box = call_with_deadline(
            lambda: sb.run(SPIN_SOURCE, input_parquet=str(inp), output_dir=str(tmp_path)))
        assert not thread.is_alive(), (
            f"run() hung past {HUNT_DEADLINE_S}s despite timeout_s=5 -- the container "
            f"was never reaped and the drain never returned")
        exc = box.get("exc")
        assert isinstance(exc, SandboxRunFailed), f"expected SandboxRunFailed, got {box!r}"
        assert "timed out" in str(exc)
        assert guard.new() == [], "container survived the timeout"


@_needs_docker
def test_real_container_is_gone_after_timeout(tmp_path):
    """agy 1c: the daemon really reaps the container, not just the CLI process."""
    import pandas as pd
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    sb = DockerSandbox(image=SANDBOX_TEST_IMAGE, memory="256m", timeout_s=2,
                       allow_unpinned=True)

    with SandboxContainerGuard() as guard:
        thread, box = call_with_deadline(
            lambda: sb.run(SPIN_SOURCE, input_parquet=str(inp), output_dir=str(tmp_path)))
        assert not thread.is_alive(), f"run() hung past {HUNT_DEADLINE_S}s"
        assert isinstance(box.get("exc"), SandboxError), f"expected SandboxError, got {box!r}"
        assert guard.new() == [], "leaked container(s)"
