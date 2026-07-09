"""Talos Foundry LLM forge engine + bounded repair loop (Phase 1C).

The ONLY place an LLM writes code. Every attempt runs the full Phase-0 gauntlet:
AST allowlist gate -> Docker sandbox (offline, memory-capped, container-timeout-
reaped) -> PIT-at-boundary verification. Bounded: <= max_retries with prior-error
feedback, then buried. 1C forges only; scoring (1A) + ledger + evidence card are
wired by 1D."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np
import pandas as pd

from research.hermes.hypothesis import Hypothesis
from research.hermes.pit import LookaheadError, PROBE_FROM_DEFAULT, PERTURB_GAP
from research.hermes.sandbox_ast import check_source

_FENCE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL)

_PROMPT = """You are writing a single Python factor for a crypto perp research pipeline.

Hypothesis to implement: {desc}

Hard contract (violating any of these fails the attempt):
- Define exactly one function `compute(df)` that returns a pandas Series.
- The returned Series MUST keep df's DatetimeIndex unchanged (do not reset/strip/reindex it).
- Use ONLY: pandas, numpy, scipy, ta, math, statistics. No I/O, no network, no os/sys.
- Point-in-time: a value at row t may depend only on rows <= t. No .shift(-k), no bfill/backfill.
Return ONLY a fenced ```python code block.
{repair}"""


class LLMCoder(Protocol):
    def complete(self, prompt: str) -> str: ...


def build_prompt(hypothesis: Hypothesis, prior_code: Optional[str] = None,
                 prior_error: Optional[str] = None) -> str:
    # agy 5b: feed back the previous CODE as well as the error, else the LLM
    # cannot tell which lines failed and re-emits the same mistake.
    repair = "" if not prior_error else (
        f"\nYour previous code:\n```python\n{prior_code or ''}\n```\n"
        f"failed with:\n{prior_error}\nFix it.")
    return _PROMPT.format(desc=hypothesis.description, repair=repair)


def extract_code(response: str) -> str:
    """Pull the first ```python fenced block out of an LLM response.

    Falls back to the whole response (stripped) if no fence is present.
    """
    m = _FENCE.search(response)
    return (m.group(1) if m else response).strip()


def generate_code(llm: LLMCoder, prompt: str) -> str:
    """LLM -> fenced code -> AST allowlist gate.

    Raises UnsafeCodeError (from sandbox_ast.check_source) on gate failure.
    """
    code = extract_code(llm.complete(prompt))
    check_source(code)                               # layer-0; raises UnsafeCodeError
    return code


def pit_check_via_sandbox(code: str, panel, baseline, run, atol=1e-9, rtol=1e-9) -> None:
    """Verify point-in-time at the sandbox boundary. `baseline` is the series
    forge ALREADY computed on the clean panel (agy 4a: don't recompute it).
    We corrupt the future of the panel, run the sandbox ONCE more, and assert the
    pre-corruption region matches baseline. `run(code, panel)->Series` is injected
    (real DockerSandbox in 1D; fake in tests). Raises LookaheadError on leak."""
    n = len(panel)
    perturb_from = min(PROBE_FROM_DEFAULT, n - PERTURB_GAP - 1)
    if perturb_from <= 0 or perturb_from >= n:
        raise ValueError(f"perturb_from {perturb_from} out of range for n={n}")

    if not all(pd.api.types.is_numeric_dtype(dt) for dt in panel.dtypes):
        raise ValueError(
            "pit_check_via_sandbox requires an all-numeric panel; got dtypes: "
            + str(dict(panel.dtypes))
        )

    corrupt = panel.copy()
    corrupt.iloc[perturb_from:] = 1e10
    corrupt.iloc[perturb_from + PERTURB_GAP:] = np.nan
    after = np.asarray(run(code, corrupt), dtype="float64")[:perturb_from]
    base = np.asarray(baseline, dtype="float64")[:perturb_from]
    if not np.allclose(base, after, atol=atol, rtol=rtol, equal_nan=True):
        drift = np.nanmax(np.abs(base - after))
        raise LookaheadError(f"factor peeks into the future via sandbox: drift {drift:.3e}")
