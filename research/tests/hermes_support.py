"""Test doubles and wall-clock guards shared by the sandbox test modules.

Not collected by pytest (no `test_` prefix).
"""
from __future__ import annotations

import subprocess
import threading

# timeout_s in these tests is 2-5s; the budget below covers image start plus a
# `docker kill` (measured ~0.5s on a spinning container) with generous slack.
HUNT_DEADLINE_S = 40


class FakePopen:
    """Stands in for the `docker run` child process.

    DockerSandbox drives the container through subprocess.Popen, not
    subprocess.run -- patching the latter leaves the real docker CLI to be
    spawned for real, and the assertions then pass or fail on whatever the
    daemon happened to do. Set `timeout_on_first_communicate` to simulate a
    container that outlives its wall clock; `drain_hangs` additionally wedges
    the post-reap drain.
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "",
                 timeout_on_first_communicate: bool = False,
                 log: list | None = None, drain_hangs: bool = False,
                 first_exception: BaseException | None = None):
        self.returncode = returncode
        self._stdout, self._stderr = stdout, stderr
        self._timeout_first = timeout_on_first_communicate
        self._drain_hangs = drain_hangs
        self._first_exception = first_exception
        self.communicate_calls = 0
        self.killed = False
        self.log = log if log is not None else []

    def __enter__(self) -> "FakePopen":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        self.log.append(f"communicate#{self.communicate_calls}")
        if self.communicate_calls == 1:
            if self._first_exception is not None:
                raise self._first_exception
            if self._timeout_first:
                raise subprocess.TimeoutExpired("docker run", timeout or 1)
        if self._drain_hangs:
            raise subprocess.TimeoutExpired("docker run", timeout or 1)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.log.append("cli-kill")


def _live_sandbox_containers() -> set[str]:
    out = subprocess.run(["docker", "ps", "-q", "-f", "name=talos_sbx_"],
                         capture_output=True, text=True, timeout=15)
    return set(out.stdout.split())


class SandboxContainerGuard:
    """Scope container assertions and cleanup to containers this test started.

    A blanket `docker kill $(docker ps -q -f name=talos_sbx_)` would also kill a
    real Foundry sweep running on the same host -- an irreversible action taken
    on someone else's work because the name filter is global. Snapshot what was
    already running, and only ever touch the difference.
    """

    def __enter__(self) -> "SandboxContainerGuard":
        self._before = _live_sandbox_containers()
        return self

    def new(self) -> list[str]:
        """Containers that appeared since __enter__ -- i.e. ours."""
        return sorted(_live_sandbox_containers() - self._before)

    def __exit__(self, *_exc) -> bool:
        # Runs even when an assertion failed, so a regression leaves nothing
        # spinning and unblocks the daemon thread still inside sandbox.run().
        for cid in self.new():
            subprocess.run(["docker", "kill", cid], capture_output=True, timeout=20)
        return False


def call_with_deadline(fn, deadline_s: float = HUNT_DEADLINE_S):
    """Run fn() on a daemon thread and stop waiting after deadline_s.

    A test for a timeout that can itself wait forever cannot detect a broken
    timeout -- it just becomes the hang. Every real-container test must fail,
    not wedge, when the sandbox stops honouring its own timeout_s.
    """
    box: dict = {}

    def target():
        try:
            box["ret"] = fn()
        except BaseException as exc:      # noqa: BLE001 - inspected by the caller
            box["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(deadline_s)
    return thread, box


class ScriptedLLM:
    """A fake LLMCoder that returns pre-written bodies in call order, wrapped in
    a markdown fence with a chatty preamble so forge's build_prompt/extract_code
    parse path is actually exercised (a clean string would bypass it). Records
    every prompt it is handed so a test can assert the hypothesis description
    reached the LLM."""

    def __init__(self, bodies: list):
        self._bodies = list(bodies)
        self._i = 0
        self.prompts: list = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        body = self._bodies[min(self._i, len(self._bodies) - 1)]
        self._i += 1
        return f"Certainly! Here is the factor you asked for:\n\n```python\n{body}\n```\nHope this helps."
