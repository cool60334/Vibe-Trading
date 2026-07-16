"""Talos Foundry LLM forge engine + bounded repair loop (Phase 1C).

The ONLY place an LLM writes code. Every attempt runs the full Phase-0 gauntlet:
AST allowlist gate -> Docker sandbox (offline, memory-capped, container-timeout-
reaped) -> PIT-at-boundary verification. Bounded: <= max_retries with prior-error
feedback, then buried. 1C forges only; scoring (1A) + ledger + evidence card are
wired by 1D."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np
import pandas as pd

from research.hermes.errors import HermesGuardError
from research.hermes.hypothesis import Hypothesis
from research.hermes.pit import LookaheadError, PROBE_FROM_DEFAULT, PERTURB_GAP
from research.hermes.sandbox import SandboxError, SandboxRunFailed
from research.hermes.sandbox_ast import check_source, UnsafeCodeError

_FENCE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL)

_PROMPT = """You are writing a single Python factor for a crypto perp research pipeline.

Hypothesis to implement: {desc}

`df` has EXACTLY these columns and nothing else:
{columns}

Hard contract (violating any of these fails the attempt):
- Define exactly one function `compute(df)` that returns a pandas Series.
- Read ONLY the column names listed above, spelled exactly as listed. They are the
  only data that exists. A name that merely sounds right (e.g. 'open_interest' when
  the list says 'oi_z') does not exist and fails the attempt.
- The returned Series MUST keep df's DatetimeIndex unchanged (do not reset/strip/reindex it).
- Use ONLY: pandas, numpy, scipy, ta, math, statistics. No I/O, no network, no os/sys.
- Point-in-time: a value at row t may depend only on rows <= t. No .shift(-k), no bfill/backfill.
Return ONLY a fenced ```python code block.
{repair}"""


class LLMCoder(Protocol):
    def complete(self, prompt: str) -> str: ...


def build_prompt(hypothesis: Hypothesis, columns, prior_code: Optional[str] = None,
                 prior_error: Optional[str] = None) -> str:
    """Prompt the code-writing model for one factor.

    `columns` is REQUIRED and has no default on purpose. The first real ideation
    run buried 11 of 20 hypotheses in forge, and the sandbox tracebacks named the
    cause: KeyError 'open_interest', KeyError 'stablecoin_supply'. Neither column
    exists -- the panel carries oi_z/oi_mom/oi_change_24h and
    stablecoin_supply_z. The prompt described the hypothesis in prose ("short
    when OI rises") and never said what df actually holds, so the model had no
    way to know the real names and guessed.

    That also explains why the repair loop could not converge: 6 of those 11 died
    as "LLM repeated identical code". Feeding back KeyError 'open_interest'
    without a column list asks the model to fix a name it still cannot see, so it
    re-emitted the same code and seen_shas buried it. The feedback was
    unactionable, not ignored.

    A default of None here would let a future caller silently reintroduce exactly
    that bug, so there isn't one.
    """
    # agy 5b: feed back the previous CODE as well as the error, else the LLM
    # cannot tell which lines failed and re-emits the same mistake.
    repair = "" if not prior_error else (
        f"\nYour previous code:\n```python\n{prior_code or ''}\n```\n"
        f"failed with:\n{prior_error}\nFix it.")
    return _PROMPT.format(desc=hypothesis.description, repair=repair,
                          columns="\n".join(f"- {c}" for c in columns))


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


class BudgetExhausted(HermesGuardError, RuntimeError):
    """Raised when the run's LLM-call budget is spent.

    Like SandboxError this is INFRASTRUCTURE exhaustion, not a repairable code
    error: it must propagate out of the repair loop rather than burn a retry.
    """


@dataclass
class ForgeBudget:
    """Run-wide circuit breaker, charged once per llm.complete() call.

    The orchestrator's early-stop only fires BETWEEN hypotheses; a hypothesis
    whose code keeps failing spends one LLM call per retry, so a nightly sweep
    can drain an API quota long before early-stop is ever consulted. Counting
    calls (not tokens) is what the LLMCoder protocol can honestly report today:
    `.complete(prompt) -> str` carries no usage. Swap in token/USD accounting
    once the client reports it; the trip point stays here.
    """
    max_llm_calls: int = 60
    used: int = 0

    def charge_call(self) -> None:
        if self.used >= self.max_llm_calls:
            raise BudgetExhausted(
                f"LLM call budget exhausted after {self.used} calls "
                f"(max_llm_calls={self.max_llm_calls})"
            )
        self.used += 1


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


def forge(hypothesis: Hypothesis, llm: LLMCoder, run_sandbox, panel,
          max_retries: int = 3, budget: "ForgeBudget | None" = None) -> ForgeResult:
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
    seen_shas: set = set()
    for attempt in range(1, max_retries + 1):
        if budget is not None:
            budget.charge_call()          # trips BEFORE spending the call; propagates
        # panel.columns, not the idea's declared `fields`: forge already holds the
        # real frame, so this is the ground truth the sandbox will actually see,
        # and it covers every source (zoo/derived/academic/llm) uniformly rather
        # than only the ones that carry a declaration.
        prompt = build_prompt(hypothesis, panel.columns, prior_code, prior_error)
        code = extract_code(llm.complete(prompt))
        last_code = code                             # agy 5c: keep even if this attempt fails
        code_sha = hashlib.sha256(code.encode()).hexdigest()
        if code_sha in seen_shas:
            # the repair feedback produced no change; another sandbox run cannot
            # produce a different outcome, and another LLM call costs budget.
            # NOTE: this check must sit BEFORE check_source() -- an AST-illegal
            # repeat would otherwise be swallowed by the repairable branch.
            return ForgeResult(False, attempt, code=code,
                               death_reason=f"LLM repeated identical code after: {last_error}")
        seen_shas.add(code_sha)
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
        except SandboxRunFailed as exc:
            # the container ran; the LLM's code is what failed. Feed the container's
            # stderr back verbatim -- it is the most useful repair signal we have.
            last_error = f"SandboxRunFailed: {exc}"
            prior_code, prior_error = code, last_error
        except SandboxError:
            raise                                    # agy 5a: infra error, not repairable, don't retry
        except (UnsafeCodeError, LookaheadError, ValueError, KeyError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            prior_code, prior_error = code, last_error
    return ForgeResult(False, max_retries, code=last_code, death_reason=last_error)
