"""Layer-1 sandbox: OS-level isolation for LLM-generated feature ETL.

SandboxExecutor is the stable interface; DockerSandbox is the only impl.
Every run passes the layer-0 AST gate first, then executes inside a locked
container: --network=none (offline), --memory (OOM cap), --read-only rootfs
with a single writable /out mount, hard timeout via subprocess.

Task 6 materialisation: the AST-checked `source` is written to a private temp
file and mounted read-only at /app/user_source.py; the fixed runner template
(research/hermes/_runner_template.py) is mounted read-only at /app/runner.py;
the real input parquet is mounted read-only at /in/<basename>. The LLM-authored
source never touches the container except as an inert, read-only file that the
runner `exec`s — it cannot write anywhere but /out.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from research.hermes.errors import HermesGuardError
from research.hermes.sandbox_ast import check_source

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
    """Raised when the Docker sandbox is unavailable or a sandboxed run fails."""


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


class DockerSandbox(SandboxExecutor):
    """Hardened container executor with AST gate + resource limits.

    Known limitation: subprocess.run(..., timeout=...) only terminates the docker CLI client
    process on timeout, not the container itself (moby doesn't propagate SIGKILL from CLI to
    daemon). Full container-lifecycle-safe timeout handling remains a follow-up.
    """
    def __init__(self, image: str = DEFAULT_IMAGE, memory: str = "1g",
                 cpus: str = "1", timeout_s: int = 120):
        self.image, self.memory, self.cpus, self.timeout_s = image, memory, cpus, timeout_s

    def _build_command(
        self,
        in_container_path: str,
        output_dir: str,
        runner: str,
        source_mount: tuple[str, str] | None = None,
        input_mount: tuple[str, str] | None = None,
    ) -> list:
        """Build the hardened `docker run` argv.

        `source_mount` / `input_mount` are optional (host_path, container_path)
        pairs added as extra read-only `-v` mounts. They default to None so
        this method's 3-positional-arg call shape (used directly by tests)
        keeps working unchanged.
        """
        cmd = [
            "docker", "run", "--rm",
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

    def run(self, source: str, input_parquet, output_dir) -> str:
        check_source(source)  # layer-0 gate FIRST: untrusted source must never reach a subprocess call, even if docker itself is unavailable/misconfigured
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

            cmd = self._build_command(
                container_input_path,
                str(output_dir),
                runner=str(RUNNER_TEMPLATE_PATH),
                source_mount=(tmp_source_path, "/app/user_source.py"),
                input_mount=(str(input_path), container_input_path),
            )
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s, text=True)
            if proc.returncode != 0:
                raise SandboxError(f"sandbox run failed: {proc.stderr[-500:]}")
            return str(Path(output_dir) / "candidate.parquet")
        finally:
            if tmp_source_path is not None:
                Path(tmp_source_path).unlink(missing_ok=True)
