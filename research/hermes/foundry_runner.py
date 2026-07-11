"""Talos Foundry runner CLI (design law 2: Talos writes decision files, a
separate runner picks them up; it never inline-runs a stage itself).

`enqueue` writes a job.json; `run` reconciles the queued jobs. The runner never
touches the network — it reads an OHLCV parquet, it does not fetch candles."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

import pandas as pd

from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed

log = logging.getLogger(__name__)


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
