"""Layer-1 sandbox: OS-level isolation for LLM-generated feature ETL.

SandboxExecutor is the stable interface; DockerSandbox is the only impl.
Every run passes the layer-0 AST gate first, then executes inside a locked
container: --network=none (offline), --memory (OOM cap), --read-only rootfs
with a single writable /out mount, hard wall-clock timeout that reaps the
container itself (DockerSandbox._run_container).

Task 6 materialisation: the AST-checked `source` is written to a private temp
file and mounted read-only at /app/user_source.py; the fixed runner template
(research/hermes/_runner_template.py) is mounted read-only at /app/runner.py;
the real input parquet is mounted read-only at /in/<basename>. The LLM-authored
source never touches the container except as an inert, read-only file that the
runner `exec`s — it cannot write anywhere but /out.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from research.hermes.errors import HermesGuardError
from research.hermes.sandbox_ast import check_source

log = logging.getLogger(__name__)

# NON-FUNCTIONAL PLACEHOLDER: bare python:3.11-slim has no pandas/numpy/scipy/ta,
# so any real compute() the AST gate allows (sandbox_ast.ALLOWED_IMPORTS) will
# ModuleNotFoundError inside the container. Callers must pass image= pointing at
# a build with those deps baked in (see Dockerfile.sandbox-example at repo root)
# until a real Foundry-sandbox image is built and pinned here.
DEFAULT_IMAGE = "python:3.11-slim"

# Fixed runner script shipped alongside this module; mounted read-only into
# every container. The LLM never sees or can modify this path.
RUNNER_TEMPLATE_PATH = Path(__file__).resolve().parent / "_runner_template.py"


class SandboxError(HermesGuardError, RuntimeError):
    """Raised when the Docker sandbox itself is unavailable (daemon down, image not pinned by digest)."""


_OOM_EXIT_CODE = 137          # 128 + SIGKILL(9): what the cgroup OOM killer leaves behind
_OOM_STDERR_MARKERS = ("MemoryError", "Killed")

_REAP_TIMEOUT_S = 15          # `docker kill` on a spinning container measures ~0.5s
_DRAIN_TIMEOUT_S = 15         # reading back stderr once the container is dead


class SandboxRunFailed(SandboxError):
    """The container ran, but the LLM's code failed inside it.

    Distinct from a bare SandboxError (docker daemon down, image not pinned):
    those are infrastructure faults that must abort the sweep, whereas a
    non-zero exit, an OOM kill, or a wall-clock timeout are caused by the code
    under test and are exactly what forge()'s bounded repair loop exists for.
    Before this split every bad LLM factor killed the entire nightly run.

    Timeout is classified here too, and that is a deliberate trade-off: the
    wall clock covers the whole `docker run`, so a wedged daemon or a saturated
    host can trip it and get blamed on the LLM. The bound on that mistake is
    forge()'s max_retries plus run_foundry()'s should_early_stop -- a wedged
    host burns a few retries and then the sweep stops. The common case, by far,
    is an LLM writing `while True`.
    """
    def __init__(self, message: str, *, exit_code: int | None = None, oom: bool = False):
        super().__init__(message)
        self.exit_code = exit_code
        self.oom = oom


def _looks_like_oom(exit_code: int, stderr: str) -> bool:
    """Exit code 137 OR a MemoryError/Killed marker in stderr.

    Exit-code-only detection misses the common case: CPython raises MemoryError
    and exits 1 long before the cgroup OOM killer fires. `docker inspect
    .State.OOMKilled` is not available to us -- `--rm` has already reaped the
    container by the time we look. This is a heuristic; the message says
    "looks OOM-killed", never asserts it as the sole cause.
    """
    if exit_code == _OOM_EXIT_CODE:
        return True
    return any(m in stderr for m in _OOM_STDERR_MARKERS)


def is_docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


class SandboxExecutor(ABC):
    @abstractmethod
    def run(self, source: str, input_parquet, output_dir) -> str:
        ...


_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def _assert_image_pinned(image: str, allow_unpinned: bool) -> None:
    """Refuse a mutable tag: the image must be pinned by digest.

    A `:tag` can be re-pushed under you between the run that vetted a factor and
    the run that is supposed to reproduce it, so a factor's evidence card would
    describe code that no longer runs the same way. Callers that knowingly point
    at a locally-built test image pass allow_unpinned=True — an explicit, visible
    opt-out rather than a silent default.
    """
    if allow_unpinned or _DIGEST_RE.search(image):
        return
    raise SandboxError(
        f"sandbox image {image!r} is not pinned by digest; use "
        f"'name@sha256:<64-hex>' (or pass allow_unpinned=True for a local test image)"
    )


class DockerSandbox(SandboxExecutor):
    """Hardened container executor with AST gate + resource limits.

    Container-lifecycle-safe timeout: every `docker run` is given a fixed
    `--name talos_sbx_<uuid>`. The run stays synchronous/blocking (no `-d`) so
    stdout/stderr can still be read back on the happy path. When the wall clock
    expires, `_run_container` reaps the container by that name *before* draining
    the CLI's pipes — see its docstring for why the order is load-bearing and
    why `subprocess.run(timeout=)` cannot be used.
    """
    def __init__(self, image: str = DEFAULT_IMAGE, memory: str = "1g",
                 cpus: str = "1", timeout_s: int = 120, allow_unpinned: bool = False):
        self.image, self.memory, self.cpus, self.timeout_s = image, memory, cpus, timeout_s
        self.allow_unpinned = allow_unpinned

    def _build_command(
        self,
        in_container_path: str,
        output_dir: str,
        runner: str,
        source_mount: tuple[str, str] | None = None,
        input_mount: tuple[str, str] | None = None,
        name: str | None = None,
    ) -> list:
        """Build the hardened `docker run` argv.

        `source_mount` / `input_mount` are optional (host_path, container_path)
        pairs added as extra read-only `-v` mounts. They default to None so
        this method's 3-positional-arg call shape (used directly by tests)
        keeps working unchanged.

        `name` sets a fixed `--name` on the container so a caller can
        `docker kill`/`docker rm` it by name if the CLI-side wait times out.
        Defaults to a fresh `talos_sbx_<uuid>` when not supplied. Deliberately
        NOT `-d` (detached): the run must stay synchronous so `capture_output`
        can still read stdout/stderr back, and so `subprocess.run(timeout=)`
        actually bounds wall-clock time on the CLI call.
        """
        if name is None:
            name = f"talos_sbx_{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm",
            f"--name={name}",
            "--network=none",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            "--read-only",
            "--pids-limit=128",
            "-v", f"{output_dir}:/out:rw",
            "-v", f"{runner}:/app/runner.py:ro",
        ]
        if source_mount is not None:
            host, container = source_mount
            cmd += ["-v", f"{host}:{container}:ro"]
        if input_mount is not None:
            host, container = input_mount
            cmd += ["-v", f"{host}:{container}:ro"]
        cmd += [
            self.image,
            "python", "/app/runner.py", in_container_path, "/out/candidate.parquet",
        ]
        return cmd

    def _is_running(self, name: str) -> bool:
        """Ask the daemon whether the container is still up. False if we can't tell."""
        try:
            out = subprocess.run(["docker", "ps", "-q", "-f", f"name={name}"],
                                 capture_output=True, text=True, timeout=_REAP_TIMEOUT_S)
        except Exception:
            return False          # cannot verify; do not cry wolf
        return bool((out.stdout or "").strip())

    def _reap(self, name: str) -> bool:
        """Kill the container by its fixed --name, then verify it is really gone.

        `docker kill`/`docker rm` are best-effort (with --rm the container may
        already have been removed, making `rm` fail harmlessly), so a swallowed
        error tells us nothing. Ask the daemon instead. A container that outlives
        its reap burns a core in the background with nothing in the logs to
        explain it, which is the failure mode this module keeps rediscovering.
        """
        for verb in (["docker", "kill", name], ["docker", "rm", "-f", name]):
            try:
                subprocess.run(verb, capture_output=True, timeout=_REAP_TIMEOUT_S)
            except Exception:
                pass
        if self._is_running(name):
            log.warning("sandbox container %s survived `docker kill`; orphan left running", name)
            return False
        return True

    def _run_container(self, cmd: list, name: str) -> tuple[int, str, str]:
        """Run `docker run` under a wall clock that actually bounds the call.

        `subprocess.run(timeout=)` cannot be used here. When it fires it kills
        the process it launched -- the docker CLI client -- and then drains that
        client's pipes with `communicate()` and NO timeout (CPython
        Lib/subprocess.py, `_mswindows` branch). But the container is a child of
        dockerd, not of the CLI: killing the client neither stops the container
        nor closes the pipe it is still writing to, so the unbounded drain never
        returns and the caller's `except TimeoutExpired` never executes. An
        LLM's `while True` wedged an entire sweep this way.

        So: kill the CONTAINER first, by name. That makes the CLI exit on its
        own, which closes the pipes and lets the drain finish. Only if the CLI
        is itself wedged do we kill it, and then we give up on its stderr.

        The bare `except` mirrors stdlib subprocess.run(): `Popen.__exit__` ends
        in an unbounded `self.wait()` for every exception but KeyboardInterrupt,
        and on KeyboardInterrupt it kills nothing at all. Either way a Ctrl-C
        mid-sweep would strand the container and then block on it forever.
        """
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout_s)
            except subprocess.TimeoutExpired as exc:
                fate = "reaped" if self._reap(name) else "SURVIVED the reap"
                try:
                    _, stderr = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stderr = ""
                raise SandboxRunFailed(
                    f"sandbox timed out after {self.timeout_s}s; container {name} {fate}"
                    + (f"\n{stderr[-800:]}" if stderr else ""),
                    exit_code=None,
                ) from exc
            except BaseException:
                self._reap(name)
                proc.kill()
                raise
        return proc.returncode, stdout or "", stderr or ""

    def run(self, source: str, input_parquet, output_dir) -> str:
        check_source(source)  # layer-0 gate FIRST: untrusted source must never reach a subprocess call, even if docker itself is unavailable/misconfigured
        _assert_image_pinned(self.image, self.allow_unpinned)
        if not is_docker_available():
            raise SandboxError("docker daemon unavailable; cannot run sandboxed ETL")

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        tmp_source_path: str | None = None
        try:
            fd, tmp_source_path = tempfile.mkstemp(suffix="_user_source.py")
            with os.fdopen(fd, "w") as f:
                f.write(source)

            input_path = Path(input_parquet)
            container_input_path = f"/in/{input_path.name}"

            name = f"talos_sbx_{uuid.uuid4().hex[:12]}"
            cmd = self._build_command(
                container_input_path,
                str(output_dir),
                runner=str(RUNNER_TEMPLATE_PATH),
                source_mount=(tmp_source_path, "/app/user_source.py"),
                input_mount=(str(input_path), container_input_path),
                name=name,
            )
            returncode, _stdout, stderr = self._run_container(cmd, name)
            if returncode != 0:
                oom = _looks_like_oom(returncode, stderr)
                hint = (f"; looks OOM-killed — the code exceeded --memory={self.memory}"
                        if oom else "")
                raise SandboxRunFailed(
                    f"sandbox run failed (exit {returncode}){hint}\n{stderr[-800:]}",
                    exit_code=returncode, oom=oom,
                )
            return str(Path(output_dir) / "candidate.parquet")
        finally:
            if tmp_source_path is not None:
                Path(tmp_source_path).unlink(missing_ok=True)
