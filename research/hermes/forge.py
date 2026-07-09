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
from research.hermes.sandbox import SandboxError
from research.hermes.sandbox_ast import check_source, UnsafeCodeError

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


@dataclass(frozen=True)
class ForgeResult:
    """Outcome of a bounded forge() repair loop.

    On success: `series` is the pd.Series computed by the winning attempt.
    On exhaustion: `code` still holds the LAST attempt's code (agy 5c) so a
    1D orchestrator can record the bad code on the evidence card/ledger even
    though the hypothesis was buried.
    """
    success: bool
    attempts: int
    code: Optional[str] = None
    series: object = None                 # pd.Series on success
    death_reason: Optional[str] = None


def forge(hypothesis: Hypothesis, llm: LLMCoder, run_sandbox, panel, max_retries: int = 3) -> ForgeResult:
    """Bounded repair loop: generate -> sandbox-run -> index-contract check ->
    PIT-at-boundary check, retrying on code errors with prior-attempt feedback.

    `run_sandbox(code, panel) -> pd.Series` executes the code (real
    DockerSandbox.run in 1D; a fake in tests). `SandboxError` signals
    infrastructure trouble (e.g. docker daemon down) rather than a bad
    hypothesis/code, so it is NOT treated as repairable: it propagates
    immediately instead of burning a retry (agy 5a). Everything else that can
    plausibly come from bad LLM code (AST-gate rejection, a lookahead leak, or
    a stripped/reindexed result) is fed back to the LLM as `prior_error` on
    the next attempt. After `max_retries` failed attempts the hypothesis is
    buried: the result carries `code=<last attempt's code>` (agy 5c) and a
    human-readable `death_reason`.
    """
    prior_code, prior_error = None, None
    last_error, last_code = "no attempt ran", None
    for attempt in range(1, max_retries + 1):
        prompt = build_prompt(hypothesis, prior_code, prior_error)
        code = extract_code(llm.complete(prompt))
        last_code = code                             # agy 5c: keep even if this attempt fails
        try:
            check_source(code)                       # layer-0 AST gate; raises UnsafeCodeError
            series = run_sandbox(code, panel)
            # agy: a stripped/reset/reindexed result must fail with a clear
            # contract message here, not an obscure broadcast/alignment error
            # further downstream in the pipeline.
            if not isinstance(series, pd.Series) or not series.index.equals(panel.index):
                raise ValueError(
                    "Contract violation: compute(df) must return a Series that "
                    "keeps df's DatetimeIndex unchanged (index was reset, "
                    "reindexed, or otherwise dropped)"
                )
            pit_check_via_sandbox(code, panel, series, run_sandbox)
            return ForgeResult(True, attempt, code=code, series=series)
        except SandboxError:
            raise                                    # agy 5a: infra error, not repairable, don't retry
        except (UnsafeCodeError, LookaheadError, ValueError, KeyError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            prior_code, prior_error = code, last_error
    return ForgeResult(False, max_retries, code=last_code, death_reason=last_error)
