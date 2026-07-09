"""Talos Foundry LLM forge engine + bounded repair loop (Phase 1C).

The ONLY place an LLM writes code. Every attempt runs the full Phase-0 gauntlet:
AST allowlist gate -> Docker sandbox (offline, memory-capped, container-timeout-
reaped) -> PIT-at-boundary verification. Bounded: <= max_retries with prior-error
feedback, then buried. 1C forges only; scoring (1A) + ledger + evidence card are
wired by 1D."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from research.hermes.hypothesis import Hypothesis

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
