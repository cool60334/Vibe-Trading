# research/hermes/orchestrator.py
"""Talos Foundry orchestrator (Phase 1D) — wires 1B->1C->1A->1E with budget +
early stopping, triggered write-file->reconcile (never inline)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from research.hermes.sandbox import SandboxExecutor


def make_run_sandbox(sandbox: SandboxExecutor, scratch_dir: str | Path):
    """Adapt DockerSandbox.run into forge's run(code, panel)->Series.
    Writes panel to a temp parquet, runs the sandbox, reads the single-column
    candidate.parquet back and re-attaches panel's index."""
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    def run(code: str, panel: pd.DataFrame) -> pd.Series:
        # TemporaryDirectory guarantees cleanup (happy path + exceptions) so
        # per-hypothesis scratch dirs don't accumulate across a nightly run.
        with tempfile.TemporaryDirectory(dir=scratch) as job:
            in_path = Path(job) / "in.parquet"
            panel.to_parquet(in_path)
            out_path = sandbox.run(code, input_parquet=str(in_path), output_dir=str(job))
            out = pd.read_parquet(out_path)
            series = out.iloc[:, 0]
        # Positional correspondence only: this assumes the sandboxed compute()
        # preserved row order/count. A same-length reorder would NOT be caught.
        if len(series) != len(panel):
            raise ValueError(
                f"sandbox output length {len(series)} != panel length {len(panel)}; "
                "cannot restore index"
            )
        series.index = panel.index               # runner drops index; restore it
        return series

    return run
