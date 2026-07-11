# Talos Foundry OpenRouter LLM Client + Batch Cost Ceiling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `build_llm("openrouter")` as a thin adapter over the agent's OpenRouter client, add a batch-level call-count ceiling via one shared `ForgeBudget`, and gate a small paid run behind an explicit flag.

**Architecture:** A new `research/hermes/llm_client.py` holds `OpenRouterCoder` (satisfies `LLMCoder.complete(prompt)->str` over `ChatOpenAI.invoke`) and a `build_openrouter_coder` factory that reuses `agent/src/providers/llm.py`'s `build_llm`. `reconcile_foundry_jobs` builds ONE shared `ForgeBudget` and threads it through `run_foundry_job`/`run_foundry` (charged before each call, so failed jobs' spend still counts), and aborts the batch on `LLMUnavailable`. `main`'s `run` subcommand requires `--model` and `--i-will-spend-real-money`, else refuses.

**Tech Stack:** Python 3.11, langchain-openai / langchain-core, openai SDK, pytest. No new dependencies (all already installed).

**Spec:** `docs/superpowers/specs/2026-07-11-foundry-llm-client-design.md`

## Global Constraints

- **No test spends money.** Every test monkeypatches the agent `build_llm` or hands `OpenRouterCoder` a fake chat. The real OpenRouter endpoint is hit only by the manual, user-gated paid run.
- **One-directional coupling.** `research/` imports `agent/` (via `sys.path`); never edit `agent/` for this work.
- **Fail loud, catch narrow.** The import/signature fuse raises a clear `RuntimeError`; the adapter catches only concrete provider auth/quota/connection exceptions and re-raises `LLMUnavailable` — never a bare `except Exception` (which would mask a code bug as a quota error).
- **Batch cap is one shared `ForgeBudget`, charged before each call** ([forge.py:167]) — never a sum of returned summaries (a failed job returns none, so its spend would vanish).
- **`LLMUnavailable` aborts the batch** via an explicit `except LLMUnavailable: raise` placed BEFORE `reconcile`'s generic `except Exception: continue`.
- **Three pytest scopes never mix.** Run research tests from repo root: `python -m pytest research/tests/`.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Never push without being asked.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `research/hermes/llm_client.py` (create) | `LLMUnavailable`, `OpenRouterCoder` (complete/flatten/narrow-catch), `build_openrouter_coder` factory | 1, 2 |
| `research/tests/test_hermes_llm_client.py` (create) | adapter + factory unit tests (fake chat / monkeypatched build_llm) | 1, 2 |
| `research/hermes/orchestrator.py` (modify) | `run_foundry` + `run_foundry_job` gain optional `forge_budget=None` | 3 |
| `research/tests/test_hermes_orchestrator.py` (modify) | shared-budget threading test | 3 |
| `research/hermes/foundry_runner.py` (modify) | shared `ForgeBudget` in `reconcile`, batch cap, `LLMUnavailable` abort; `build_llm("openrouter")`; `main` run flags | 4, 5 |
| `research/tests/test_hermes_foundry_runner.py` (modify) | batch-cap + abort + flag-gating tests | 4, 5 |

---

## Task 1: `OpenRouterCoder` + `LLMUnavailable`

**Files:**
- Create: `research/hermes/llm_client.py`
- Test: `research/tests/test_hermes_llm_client.py`

**Interfaces:**
- Consumes: `HermesGuardError` from `research.hermes.errors`.
- Produces:
  - `class LLMUnavailable(HermesGuardError)`.
  - `class OpenRouterCoder` with `__init__(self, chat, max_tokens: int)` and `complete(self, prompt: str) -> str`; exposes `self.total_tokens: int` (accumulated usage).

**Why:** The adapter is where `.invoke()`'s `AIMessage` becomes the plain string forge expects, and where provider auth/quota failures become the `LLMUnavailable` that aborts the batch. Both are pure logic, testable with a fake chat and no network.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_llm_client.py (create)
from types import SimpleNamespace

import pytest


class FakeMsg:
    """AIMessage-shaped: text lives in .content, usage in .usage_metadata."""
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class FakeChat:
    """Stands in for a bound ChatOpenAI. Records invoke() input; returns a
    pre-set FakeMsg or raises a pre-set exception."""
    def __init__(self, msg=None, exc=None):
        self._msg, self._exc = msg, exc
        self.invoked_with = None

    def invoke(self, messages):
        self.invoked_with = messages
        if self._exc is not None:
            raise self._exc
        return self._msg


def test_complete_flattens_text_blocks():
    from research.hermes.llm_client import OpenRouterCoder
    chat = FakeChat(msg=FakeMsg([{"type": "text", "text": "def compute(df): ..."}]))
    coder = OpenRouterCoder(chat, max_tokens=2048)
    assert coder.complete("hi") == "def compute(df): ..."


def test_complete_passes_plain_string_content():
    from research.hermes.llm_client import OpenRouterCoder
    coder = OpenRouterCoder(FakeChat(msg=FakeMsg("plain code")), max_tokens=2048)
    assert coder.complete("hi") == "plain code"


def test_complete_strips_inline_think_block():
    from research.hermes.llm_client import OpenRouterCoder
    coder = OpenRouterCoder(FakeChat(msg=FakeMsg("<think>musing</think>real")), max_tokens=2048)
    assert coder.complete("hi") == "real"


def test_complete_wraps_auth_error_as_llm_unavailable():
    import openai
    from research.hermes.llm_client import OpenRouterCoder, LLMUnavailable
    err = openai.AuthenticationError.__new__(openai.AuthenticationError)  # no live response needed
    coder = OpenRouterCoder(FakeChat(exc=err), max_tokens=2048)
    with pytest.raises(LLMUnavailable):
        coder.complete("hi")


def test_complete_does_not_swallow_a_code_bug():
    from research.hermes.llm_client import OpenRouterCoder, LLMUnavailable
    coder = OpenRouterCoder(FakeChat(exc=TypeError("bug in adapter")), max_tokens=2048)
    with pytest.raises(TypeError):                 # NOT relabelled LLMUnavailable
        coder.complete("hi")


def test_complete_accumulates_token_usage():
    from research.hermes.llm_client import OpenRouterCoder
    chat = FakeChat(msg=FakeMsg("code", usage={"total_tokens": 42}))
    coder = OpenRouterCoder(chat, max_tokens=2048)
    coder.complete("hi")
    assert coder.total_tokens == 42


def test_complete_sends_a_human_message():
    from research.hermes.llm_client import OpenRouterCoder
    chat = FakeChat(msg=FakeMsg("code"))
    OpenRouterCoder(chat, max_tokens=2048).complete("the-prompt")
    sent = chat.invoked_with
    assert isinstance(sent, list) and len(sent) == 1
    assert getattr(sent[0], "content", None) == "the-prompt"      # a HumanMessage
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_llm_client.py -v`
Expected: FAIL (`research.hermes.llm_client` does not exist).

- [ ] **Step 3: Implement**

```python
# research/hermes/llm_client.py (create)
"""Talos Foundry LLM client: a thin adapter making the agent's OpenRouter
ChatOpenAI satisfy forge's LLMCoder protocol (.complete(prompt) -> str).

The heavy lifting (OpenRouter base URL, key, reasoning handling, retries,
timeout) lives in agent/src/providers/llm.py; this module only adapts its
LangChain ChatOpenAI to a plain-string interface and classifies provider
failures as LLMUnavailable so the batch aborts instead of burning retries."""
from __future__ import annotations

import re

from research.hermes.errors import HermesGuardError

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMUnavailable(HermesGuardError):
    """The LLM backend is unusable: bad/absent key, exhausted quota, or an
    unreachable endpoint. Infrastructure, not a repairable code error — it must
    abort the batch, since every subsequent job would hit the same wall."""


def _flatten_content(content) -> str:
    """AIMessage.content is a str or a list of content blocks. Join the text of
    the blocks rather than str()-ing the list, which would inject Python syntax
    (brackets, quotes) into forge's code parser."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "".join(parts)
    else:
        text = str(content)
    # Defensive: the agent client already routes reasoning into
    # additional_kwargs, but strip an inline <think> block just in case a model
    # emits chain-of-thought in the content itself.
    return _THINK_RE.sub("", text).strip()


class OpenRouterCoder:
    """Adapts a bound ChatOpenAI to forge's LLMCoder.complete(prompt) -> str."""

    def __init__(self, chat, max_tokens: int):
        self._chat = chat
        self.max_tokens = max_tokens
        self.total_tokens = 0

    def complete(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage
        import openai
        try:
            msg = self._chat.invoke([HumanMessage(content=prompt)])
        except (openai.AuthenticationError, openai.PermissionDeniedError,
                openai.RateLimitError, openai.APIConnectionError) as exc:
            raise LLMUnavailable(f"OpenRouter backend unavailable: "
                                 f"{type(exc).__name__}: {exc}") from exc
        usage = getattr(msg, "usage_metadata", None)
        if isinstance(usage, dict):
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)
        return _flatten_content(msg.content)
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest research/tests/test_hermes_llm_client.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/llm_client.py research/tests/test_hermes_llm_client.py
git commit -m "feat(hermes): OpenRouterCoder adapter + LLMUnavailable classification

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `build_openrouter_coder` factory (reuses agent build_llm)

**Files:**
- Modify: `research/hermes/llm_client.py`
- Test: `research/tests/test_hermes_llm_client.py`

**Interfaces:**
- Consumes: `OpenRouterCoder` (Task 1); `agent/src/providers/llm.py`'s `build_llm(*, model_name, callbacks) -> ChatOpenAI`.
- Produces: `build_openrouter_coder(*, model: str, max_tokens: int = 2048) -> OpenRouterCoder`.

**Why:** The factory is the one-directional coupling point. It imports the agent's battle-tested client, pins the model explicitly (never trusting env for a paid run), binds `max_tokens`, sets the OpenRouter provider/base-url env the agent needs, and fails loud if the upstream import or signature breaks.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_llm_client.py (append)
def test_build_openrouter_coder_passes_explicit_model(monkeypatch):
    import research.hermes.llm_client as mod
    captured = {}
    class BoundChat:
        def invoke(self, m): return FakeMsg("code")
    class RawChat:
        def bind(self, **kw): captured["bind"] = kw; return BoundChat()
    def fake_build_llm(*, model_name=None, callbacks=None):
        captured["model_name"] = model_name
        return RawChat()
    monkeypatch.setattr(mod, "_agent_build_llm", fake_build_llm, raising=False)
    coder = mod.build_openrouter_coder(model="deepseek/deepseek-chat", max_tokens=1234)
    assert captured["model_name"] == "deepseek/deepseek-chat"   # explicit, not env
    assert captured["bind"]["max_tokens"] == 1234               # max_tokens bound
    assert coder.complete("x") == "code"


def test_build_openrouter_coder_sets_provider_and_base_url(monkeypatch):
    import os
    import research.hermes.llm_client as mod
    monkeypatch.delenv("LANGCHAIN_PROVIDER", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    class C:
        def bind(self, **kw): return self
        def invoke(self, m): return FakeMsg("c")
    monkeypatch.setattr(mod, "_agent_build_llm", lambda **k: C(), raising=False)
    mod.build_openrouter_coder(model="x/y")
    assert os.environ["LANGCHAIN_PROVIDER"] == "openrouter"
    assert os.environ["OPENROUTER_BASE_URL"] == "https://openrouter.ai/api/v1"


def test_build_openrouter_coder_fails_loud_on_bad_import(monkeypatch):
    import research.hermes.llm_client as mod
    def boom(**kwargs):
        raise ImportError("agent providers not on path")
    monkeypatch.setattr(mod, "_agent_build_llm", boom, raising=False)
    with pytest.raises(RuntimeError, match="agent.*build_llm|could not build"):
        mod.build_openrouter_coder(model="x/y")
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_llm_client.py -k build_openrouter -v`
Expected: FAIL (`build_openrouter_coder` / `_agent_build_llm` do not exist).

- [ ] **Step 3: Implement**

```python
# research/hermes/llm_client.py — module level, near the top
import os
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"

def _agent_build_llm(*, model_name, callbacks=None):
    """Import and call agent/src/providers/llm.py's build_llm. Isolated in one
    function so tests can monkeypatch it and the fail-loud fuse has one seam."""
    if str(_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_DIR))
    from src.providers.llm import build_llm as agent_build_llm
    return agent_build_llm(model_name=model_name, callbacks=callbacks)
```

```python
# research/hermes/llm_client.py — append
def build_openrouter_coder(*, model: str, max_tokens: int = 2048) -> OpenRouterCoder:
    """Build an OpenRouterCoder from the agent's OpenRouter ChatOpenAI.

    The model is pinned explicitly (never read from LANGCHAIN_MODEL_NAME) so a
    stray .env cannot hijack an expensive model on a paid run. The agent resolves
    the OpenRouter key/base-url from LANGCHAIN_PROVIDER=openrouter, so we set that
    and default the base URL — without it the agent falls back to api.openai.com.
    Any import/signature failure is re-raised as a clear RuntimeError (the
    one-directional coupling's fuse)."""
    os.environ["LANGCHAIN_PROVIDER"] = "openrouter"
    os.environ.setdefault("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    try:
        chat = _agent_build_llm(model_name=model)
        bound = chat.bind(max_tokens=max_tokens)
    except Exception as exc:      # noqa: BLE001 - fail loud on any coupling break
        raise RuntimeError(
            f"could not build the agent OpenRouter client (model={model!r}): "
            f"{type(exc).__name__}: {exc}") from exc
    return OpenRouterCoder(bound, max_tokens=max_tokens)
```

- [ ] **Step 4: Run — verify pass, whole file green**

Run: `python -m pytest research/tests/test_hermes_llm_client.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/llm_client.py research/tests/test_hermes_llm_client.py
git commit -m "feat(hermes): build_openrouter_coder factory over agent build_llm (model-pinned, fail-loud)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Optional shared `forge_budget` through `run_foundry` / `run_foundry_job`

**Files:**
- Modify: `research/hermes/orchestrator.py` (`run_foundry` ~line 266-337; `run_foundry_job` ~line 390-417)
- Test: `research/tests/test_hermes_orchestrator.py`

**Interfaces:**
- Consumes: `ForgeBudget` (existing).
- Produces: `run_foundry(..., forge_budget=None)` and `run_foundry_job(..., forge_budget=None)`. When `forge_budget` is passed, `run_foundry` uses it instead of building a fresh per-job one, so a caller can share one counter across jobs.

**Why:** This is the seam that makes the batch cap a live shared counter. `run_foundry` today builds its own `ForgeBudget` per call; adding an optional override lets `reconcile` pass one shared budget through, charged before each call, so a job that fails mid-sweep still leaves its spend counted.

- [ ] **Step 1: Write the failing test**

Mirror the proven harness in `test_run_foundry_respects_budget_and_early_stop`
(same file, ~line 162) — it already drives `run_foundry` to completion with
`load_features`/`build_queue`/`process_hypothesis` stubbed and the module-level
`_TEST_OOS` + `_ohlcv_for(idx)` fixtures. Add one test that pre-exhausts a shared
budget and passes it in:

```python
# research/tests/test_hermes_orchestrator.py (append)
def test_run_foundry_uses_a_passed_shared_forge_budget(tmp_path, monkeypatch):
    """A shared ForgeBudget passed in is used verbatim; run_foundry does not build
    a fresh one. Pre-exhaust it and confirm the cap bites (not budget's 99)."""
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.forge import ForgeBudget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    feats = pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: feats)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis("h0", "x0", SOURCE_ZOO)])

    shared = ForgeBudget(max_llm_calls=2)
    shared.used = 2                                  # already exhausted
    seen = {}
    def fake_process(hyp, *a, forge_budget=None, **k):
        seen["is_shared"] = forge_budget is shared
        forge_budget.charge_call()                   # raises BudgetExhausted (used>=max)
        return "candidate"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)

    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=5, max_llm_calls=99),
                          zoo_dir=tmp_path, run_sandbox=object(), oos_start=_TEST_OOS,
                          ohlcv=_ohlcv_for(idx), forge_budget=shared)
    assert seen["is_shared"] is True
    assert summary.get("budget_exhausted") is True   # shared cap bit, not budget's 99
```

If `_TEST_OOS` / `_ohlcv_for` are named differently in the file, use whatever
that harness uses — the point is to reuse the existing working fixtures, not to
hand-roll new ones.

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_orchestrator.py::test_run_foundry_uses_a_passed_shared_forge_budget -v`
Expected: FAIL (`run_foundry` has no `forge_budget` parameter).

- [ ] **Step 3: Implement**

In `run_foundry`'s signature (orchestrator.py:266-268), add `forge_budget=None` as a keyword parameter. Replace the internal construction (orchestrator.py:337):

```python
    # was: forge_budget = ForgeBudget(max_llm_calls=budget.max_llm_calls)
    forge_budget = forge_budget or ForgeBudget(max_llm_calls=budget.max_llm_calls)
```

In `run_foundry_job`'s signature (orchestrator.py:390), add `forge_budget=None`, and forward it in the `run_foundry(...)` call (orchestrator.py:408-410):

```python
        summary = run_foundry(job["symbol"], manifests_dir, cfg, llm, sandbox,
                              budget or Budget(), zoo_dir=zoo_dir, ohlcv=ohlcv,
                              oos_start=p["oos_start"], val_frac=p.get("val_frac", 0.2),
                              forge_budget=forge_budget)
```

- [ ] **Step 4: Run — verify pass, no regression**

Run: `python -m pytest research/tests/test_hermes_orchestrator.py -q`
Expected: PASS (the new test + all existing orchestrator tests; the default `forge_budget=None` preserves current behaviour).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/orchestrator.py research/tests/test_hermes_orchestrator.py
git commit -m "feat(hermes): optional shared forge_budget through run_foundry/run_foundry_job

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Shared batch budget + `LLMUnavailable` abort in `reconcile`

**Files:**
- Modify: `research/hermes/foundry_runner.py` (`reconcile_foundry_jobs`, ~line 76-107)
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `run_foundry_job(..., forge_budget=...)` (Task 3), `ForgeBudget`/`BudgetExhausted` (forge), `LLMUnavailable` (Task 1).
- Produces: `reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir, budget=None, batch_max_llm_calls=None) -> list`. Builds one shared `ForgeBudget`, threads it through every job, stops when it is exhausted, and aborts the whole batch on `LLMUnavailable`.

**Why:** This is where the money cap becomes real. One shared `ForgeBudget` charged before each call survives a job that fails mid-sweep; an `LLMUnavailable` (bad key / no quota) aborts rather than failing every remaining job the same way.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_runner.py (append; _queue_job helper already exists)
def test_batch_cap_is_a_shared_counter_across_jobs(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        forge_budget.charge_call(); forge_budget.charge_call()   # each job spends 2
        return {"candidate": 0, "llm_calls_used": forge_budget.used}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                              zoo_dir=tmp_path, batch_max_llm_calls=3)
    assert ran == ["eth"]          # eth spends 2; btc would exceed 3 -> not started


def test_batch_cap_holds_when_a_job_raises_mid_sweep(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        forge_budget.charge_call(); forge_budget.charge_call()
        raise ValueError("job blew up AFTER spending its calls")     # non-infra
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                              zoo_dir=tmp_path, batch_max_llm_calls=3)
    assert ran == ["eth"]          # eth's 2 calls still counted -> btc not started


def test_llm_unavailable_aborts_whole_batch(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.llm_client import LLMUnavailable
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, *, forge_budget=None, **k):
        import json
        ran.append(json.loads(Path(job_path).read_text())["symbol"])
        raise LLMUnavailable("bad api key")
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    with pytest.raises(LLMUnavailable):
        fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(),
                                  zoo_dir=tmp_path, batch_max_llm_calls=99)
    assert ran == ["eth"]          # btc never ran
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "batch_cap or llm_unavailable" -v`
Expected: FAIL (`reconcile_foundry_jobs` has no `batch_max_llm_calls`; no shared budget; `LLMUnavailable` swallowed by `except Exception`).

- [ ] **Step 3: Implement**

Add imports at the top of `foundry_runner.py`:

```python
from research.hermes.forge import ForgeBudget
from research.hermes.llm_client import LLMUnavailable
```

Rewrite `reconcile_foundry_jobs` (keep the ohlcv-load and ordering logic; change the budget wiring and add the abort branch):

```python
def reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir,
                           budget=None, batch_max_llm_calls=None) -> list:
    """Run every queued foundry job in created_at order under ONE shared LLM-call
    budget (write-file->reconcile).

    The shared ForgeBudget is charged before each llm.complete() (forge.py), so a
    job that fails mid-sweep still leaves its spend counted — the batch total is a
    hard cap, not a sum of returned summaries (a failed job returns none).

    Abort rules: a bare SandboxError (infra, not SandboxRunFailed) OR an
    LLMUnavailable (bad key / no quota) stops the whole batch, because every
    later job would hit the same wall. Any other job failure leaves that job
    status=failed and the batch continues."""
    shared = ForgeBudget(max_llm_calls=batch_max_llm_calls) if batch_max_llm_calls else None
    summaries = []
    for _created, job_path, job in _queued_jobs(runs_dir):
        if shared is not None and shared.used >= shared.max_llm_calls:
            log.warning("batch LLM-call budget spent (%d/%d); stopping",
                        shared.used, shared.max_llm_calls)
            break
        try:
            ohlcv = load_ohlcv(job["params"]["ohlcv_path"])
        except Exception as exc:                       # noqa: BLE001 job-level, keep going
            log.warning("job %s failed to load ohlcv: %s; continuing", job_path, exc)
            job["status"] = "failed"; job["error"] = str(exc); job["finished_at"] = _now()
            _write_job_json(job_path, job)
            continue
        try:
            summaries.append(run_foundry_job(
                job_path, manifests_dir=manifests_dir, llm=llm, sandbox=sandbox,
                zoo_dir=zoo_dir, ohlcv=ohlcv, budget=budget, forge_budget=shared))
        except LLMUnavailable:
            log.error("LLM backend unavailable on %s; aborting the batch", job_path)
            raise                                      # infra: every later job hits the same wall
        except SandboxError as exc:
            if not isinstance(exc, SandboxRunFailed):
                log.error("infra fault on %s; aborting the batch: %s", job_path, exc)
                raise
            log.warning("job %s failed (repairable, buried): %s", job_path, exc)
        except Exception as exc:                       # noqa: BLE001 job-level, keep going
            log.warning("job %s failed: %s; continuing", job_path, exc)
    return summaries
```

> The `except LLMUnavailable` must sit BEFORE `except SandboxError` and `except Exception` — it is a `HermesGuardError`, not a `SandboxError`, so a later position would let the generic branch swallow it.

- [ ] **Step 4: Run — verify pass, no regression**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -v`
Expected: PASS (new batch/abort tests + the existing reconcile tests; `batch_max_llm_calls=None` preserves current behaviour with no shared budget).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): shared batch LLM-call budget + LLMUnavailable abort in reconcile

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `build_llm("openrouter")` wiring + `main` run spend gate

**Files:**
- Modify: `research/hermes/foundry_runner.py` (`build_llm` ~line 110-118; `main` run subcommand ~line 133-152)
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `build_openrouter_coder` (Task 2), `reconcile_foundry_jobs(..., batch_max_llm_calls=...)` (Task 4).
- Produces: `build_llm(spec, *, model=None, max_tokens=2048)` returns an `OpenRouterCoder` for `"openrouter"`; `main`'s `run` gains `--model` (required), `--i-will-spend-real-money` (flag), `--batch-max-llm-calls` (default 6), `--max-tokens` (default 2048), and prints usage at the end.

**Why:** The last seam. `build_llm` stops being a `NotImplementedError`; `main` refuses to spend without the explicit flag, pins the model, and reports what it spent.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_runner.py (append)
def test_build_llm_openrouter_returns_a_coder(monkeypatch):
    from research.hermes import foundry_runner as fr
    sentinel = object()
    monkeypatch.setattr(fr, "build_openrouter_coder",
                        lambda *, model, max_tokens: sentinel)
    assert fr.build_llm("openrouter", model="x/y", max_tokens=1000) is sentinel


def test_build_llm_openrouter_requires_a_model():
    from research.hermes.foundry_runner import build_llm
    with pytest.raises(ValueError, match="model"):
        build_llm("openrouter", model=None)


def test_run_without_spend_flag_refuses_and_never_builds_llm(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    called = {"build": False}
    monkeypatch.setattr(fr, "build_llm", lambda *a, **k: called.__setitem__("build", True))
    rc = fr.main(["run", "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
                  "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
                  "--model", "x/y"])            # no --i-will-spend-real-money
    assert rc != 0                              # refused
    assert called["build"] is False            # never built the client


def test_run_with_spend_flag_builds_llm_and_reconciles(tmp_path, monkeypatch):
    from research.hermes import foundry_runner as fr
    calls = {}
    monkeypatch.setattr(fr, "resolve_image_id", lambda tag: "sha256:" + "e" * 64)
    monkeypatch.setattr(fr, "DockerSandbox", lambda **k: object())
    fake_coder = object()
    monkeypatch.setattr(fr, "build_llm", lambda *a, **k: (calls.__setitem__("model", k.get("model")), fake_coder)[1])
    def fake_reconcile(runs_dir, manifests_dir, llm, sandbox, zoo_dir, **k):
        calls["reconciled"] = True; calls["batch"] = k.get("batch_max_llm_calls"); return []
    monkeypatch.setattr(fr, "reconcile_foundry_jobs", fake_reconcile)
    rc = fr.main(["run", "--runs-dir", str(tmp_path), "--manifests-dir", str(tmp_path),
                  "--zoo-dir", str(tmp_path), "--image", "talos-sandbox:test",
                  "--model", "deepseek/deepseek-chat", "--i-will-spend-real-money",
                  "--batch-max-llm-calls", "4"])
    assert rc == 0 and calls["reconciled"] is True
    assert calls["model"] == "deepseek/deepseek-chat" and calls["batch"] == 4
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "build_llm or spend_flag" -v`
Expected: FAIL (`build_llm` raises `NotImplementedError`; `run` has no `--model`/`--i-will-spend-real-money`).

- [ ] **Step 3: Implement**

Replace `build_llm` (foundry_runner.py:110-118):

```python
def build_llm(spec: str, *, model: str | None = None, max_tokens: int = 2048):
    """Construct the LLM client named by `spec`. For openrouter, the model is
    pinned explicitly (a paid run must never inherit an expensive model from a
    stray LANGCHAIN_MODEL_NAME)."""
    if spec == "openrouter":
        if not model:
            raise ValueError("openrouter requires an explicit model (a paid run "
                             "must not read the model from the environment)")
        return build_openrouter_coder(model=model, max_tokens=max_tokens)
    raise ValueError(f"unknown llm spec {spec!r}")
```

Add the import at the top of `foundry_runner.py`:

```python
from research.hermes.llm_client import build_openrouter_coder, LLMUnavailable
```
(Combine with Task 4's `LLMUnavailable` import — a single line `from research.hermes.llm_client import build_openrouter_coder, LLMUnavailable`.)

Extend the `run` subparser (after foundry_runner.py:139) and rewrite the run branch (foundry_runner.py:148-151):

```python
    r.add_argument("--model", required=True, help="explicit OpenRouter model id (pinned)")
    r.add_argument("--i-will-spend-real-money", action="store_true",
                   help="required to actually call the paid LLM; without it, run refuses")
    r.add_argument("--batch-max-llm-calls", type=int, default=6)
    r.add_argument("--max-tokens", type=int, default=2048)
```

```python
    # run branch
    if not args.i_will_spend_real_money:
        print("refusing: a real run spends money. Re-run with "
              "--i-will-spend-real-money once you have set OPENROUTER_API_KEY.")
        return 2
    image_id = resolve_image_id(args.image)          # infra pre-check; raises to abort
    sandbox = DockerSandbox(image=image_id, timeout_s=args.timeout_s, allow_unpinned=False)
    llm = build_llm(args.llm, model=args.model, max_tokens=args.max_tokens)
    reconcile_foundry_jobs(args.runs_dir, args.manifests_dir, llm, sandbox, args.zoo_dir,
                           batch_max_llm_calls=args.batch_max_llm_calls)
    used = getattr(llm, "total_tokens", 0)
    print(f"foundry run complete. tokens used (reported): {used}")
    return 0
```

- [ ] **Step 4: Run — verify pass, full runner + client suites**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py research/tests/test_hermes_llm_client.py -v`
Expected: PASS.

- [ ] **Step 5: Full research suite + commit**

Run: `python -m pytest research/tests/ -q`
Expected: all pass/skip.

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): wire build_llm(openrouter) + spend-gated run with pinned model

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §1 N1 adapter (flatten, narrow catch, tokens) → Task 1. ✅
- §1 N1 factory (agent build_llm, model pin, base-url env, fail-loud) → Task 2. ✅
- §1/§2 N2 shared ForgeBudget → Task 3 (threading) + Task 4 (shared counter in reconcile). ✅
- §1 N3 spend barrier + model pin + usage print → Task 5. ✅
- §3 LLMUnavailable abort ordering → Task 4 (explicit branch before generic). ✅
- §3 error classes → Task 1 (LLMUnavailable), Task 4 (abort). ✅
- §4 test matrix: every row maps to a task's test (adapter 6 rows → Task 1/2; batch/flag 5 rows → Task 4/5). ✅
- Non-goals respected: no USD (prints calls/tokens only), no token budgeting, no shared module, no real-API test. ✅

**Placeholder scan:** no TBD/TODO. `<cheap-openrouter-coding-model>` appears only in the spec's runbook, not this plan; here the model is always an explicit `--model` value. Task 3's `_tiny_*` helpers are specified concretely (reuse existing or a 40-row UTC-indexed frame).

**Type consistency:** `build_openrouter_coder(*, model, max_tokens)` identical in Task 2 def and Task 5 call. `OpenRouterCoder(chat, max_tokens)` / `.complete` / `.total_tokens` consistent across Task 1/2/5. `reconcile_foundry_jobs(..., batch_max_llm_calls=None)` identical in Task 4 def and Task 5 call. `run_foundry_job(..., forge_budget=...)` matches Task 3 def and Task 4 call. `LLMUnavailable` defined in Task 1, imported in Task 4. `_agent_build_llm` monkeypatch seam consistent across Task 2 tests and impl.

**Known deferred (documented in spec, not a plan gap):** the real OpenRouter endpoint is exercised only by the manual paid-run runbook; no automated test spends money.
