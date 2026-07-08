"""Layer-1 sandbox: OS-level isolation for LLM-generated feature ETL.

SandboxExecutor is the stable interface; DockerSandbox is the only impl.
Every run passes the layer-0 AST gate first, then executes inside a locked
container: --network=none (offline), --memory (OOM cap), --read-only rootfs
with a single writable /out mount, hard timeout via subprocess.

NOTE: full source-materialisation (writing the AST-checked source into the
container, mounting a real runner script, wiring the input parquet) is
completed in Task 6. This module establishes the hardened command surface
and the AST-gate-before-docker ordering only.
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from research.hermes.sandbox_ast import check_source

DEFAULT_IMAGE = "python:3.11-slim"


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
    def __init__(self, image: str = DEFAULT_IMAGE, memory: str = "1g",
                 cpus: str = "1", timeout_s: int = 120):
        self.image, self.memory, self.cpus, self.timeout_s = image, memory, cpus, timeout_s

    def _build_command(self, in_container_path: str, output_dir: str, runner: str) -> list:
        return [
            "docker", "run", "--rm",
            "--network=none",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            "--read-only",
            "--pids-limit=128",
            "-v", f"{output_dir}:/out:rw",
            "-v", f"{runner}:/app/runner.py:ro",
            self.image,
            "python", "/app/runner.py", in_container_path, "/out/candidate.parquet",
        ]

    def run(self, source: str, input_parquet, output_dir) -> str:
        check_source(source)                       # layer-0 gate FIRST
        if not is_docker_available():
            raise RuntimeError("docker daemon unavailable; cannot run sandboxed ETL")
        # NOTE: writing `source` + input mount + runner materialisation is completed
        # in Task 6 wiring; this method establishes the hardened command surface.
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        cmd = self._build_command(str(input_parquet), str(output_dir), runner="/app/runner.py")
        proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"sandbox run failed: {proc.stderr[-500:]}")
        return str(Path(output_dir) / "candidate.parquet")
