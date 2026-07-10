# research/hermes/orchestrator.py
"""Talos Foundry orchestrator (Phase 1D) — wires 1B->1C->1A->1E with budget +
early stopping, triggered write-file->reconcile (never inline)."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pandas as pd


def make_run_sandbox(sandbox, scratch_dir):
    """Adapt DockerSandbox.run into forge's run(code, panel)->Series.
    Writes panel to a temp parquet, runs the sandbox, reads the single-column
    candidate.parquet back and re-attaches panel's index."""
    scratch = Path(scratch_dir)

    def run(code: str, panel: pd.DataFrame) -> pd.Series:
        job = scratch / f"sbx_{uuid.uuid4().hex[:12]}"
        job.mkdir(parents=True, exist_ok=True)
        in_path = job / "in.parquet"
        panel.to_parquet(in_path)
        out_path = sandbox.run(code, input_parquet=str(in_path), output_dir=str(job))
        out = pd.read_parquet(out_path)
        series = out.iloc[:, 0]
        series.index = panel.index               # runner drops index; restore it
        return series

    return run
