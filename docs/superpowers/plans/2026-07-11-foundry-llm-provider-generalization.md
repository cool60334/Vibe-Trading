# Talos Foundry LLM Provider Generalization (OpenAI) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Foundry's LLM client run against a direct OpenAI key (which the operator has) as well as OpenRouter, via one provider-parameterized factory.

**Architecture:** Rename the provider-agnostic adapter `OpenRouterCoder` → `ChatCoder` (keep a back-compat alias), add a deterministic `build_llm_coder(provider, model, max_tokens)` factory that validates the provider first, clears cross-provider base residue, and pins provider+model; make `build_openrouter_coder` a thin wrapper; widen `foundry_runner.build_llm`'s dispatch to `{openrouter, openai}`.

**Tech Stack:** Python 3.11, langchain-openai/-core, openai SDK, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-11-foundry-llm-provider-generalization-design.md`

## Global Constraints

- **No test spends money.** All tests monkeypatch `_agent_build_llm` and use `monkeypatch` for `os.environ` (the factory mutates process-global env; monkeypatch auto-restores so tests don't flap).
- **Deterministic factory.** `build_llm_coder` must validate the provider **first** (capabilities silently falls back to openai for an unknown name), then clear `OPENAI_API_BASE`/`OPENAI_BASE_URL` residue so a prior build or stale env cannot misroute the key.
- **One-directional coupling.** `research/` imports `agent/`; never edit `agent/`.
- **Supported providers:** `("openrouter", "openai")` only. Unknown → loud `ValueError`.
- **Three pytest scopes never mix.** Run research tests from repo root: `python -m pytest research/tests/`.
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Never push without being asked.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `research/hermes/llm_client.py` (modify) | rename adapter → `ChatCoder` (+alias); generic `build_llm_coder`; `build_openrouter_coder` wrapper | 1 |
| `research/tests/test_hermes_llm_client.py` (modify) | provider factory tests (alias, validation-first, base-pop, model-pin, back-compat) | 1 |
| `research/hermes/foundry_runner.py` (modify) | `build_llm` dispatch to `{openrouter, openai}` | 2 |
| `research/tests/test_hermes_foundry_runner.py` (modify) | `build_llm("openai")` returns a coder | 2 |

---

## Task 1: Generalize the client — `ChatCoder` + `build_llm_coder`

**Files:**
- Modify: `research/hermes/llm_client.py:59-100` (`OpenRouterCoder` class + `build_openrouter_coder`)
- Test: `research/tests/test_hermes_llm_client.py`

**Interfaces:**
- Consumes: `_agent_build_llm` (existing), `ChatCoder` (renamed).
- Produces:
  - `class ChatCoder` (the former `OpenRouterCoder`); `OpenRouterCoder = ChatCoder` alias.
  - `build_llm_coder(*, provider: str, model: str, max_tokens: int = 2048) -> ChatCoder`.
  - `build_openrouter_coder(*, model, max_tokens=2048)` → wrapper calling `build_llm_coder(provider="openrouter", ...)`.

**Why:** The adapter already wraps any provider's `ChatOpenAI`; only the factory was OpenRouter-specific. Generalizing it (deterministically, per the agy review) unlocks the operator's OpenAI key without touching the batch budget, spend gate, or error handling.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_llm_client.py (append; FakeMsg/FakeChat already defined in this file)
def test_openrouter_coder_alias_points_at_chatcoder():
    from research.hermes.llm_client import OpenRouterCoder, ChatCoder
    assert OpenRouterCoder is ChatCoder


def test_build_llm_coder_rejects_unknown_provider_before_any_side_effect(monkeypatch):
    import os
    import research.hermes.llm_client as mod
    called = {"agent": False}
    monkeypatch.setattr(mod, "_agent_build_llm",
                        lambda **k: called.__setitem__("agent", True), raising=False)
    before = os.environ.get("LANGCHAIN_PROVIDER")
    with pytest.raises(ValueError, match="unsupported provider"):
        mod.build_llm_coder(provider="anthropic", model="claude")
    assert called["agent"] is False                       # raised before building
    assert os.environ.get("LANGCHAIN_PROVIDER") == before  # no env mutation


def test_build_llm_coder_openai_clears_cross_provider_base(monkeypatch):
    import os
    import research.hermes.llm_client as mod
    monkeypatch.setenv("OPENAI_API_BASE", "https://openrouter.ai/api/v1")   # residue
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    class C:
        def bind(self, **kw): return self
        def invoke(self, m): return FakeMsg("c")
    monkeypatch.setattr(mod, "_agent_build_llm", lambda **k: C(), raising=False)
    mod.build_llm_coder(provider="openai", model="gpt-4o-mini")
    assert os.environ["LANGCHAIN_PROVIDER"] == "openai"
    assert "OPENAI_API_BASE" not in os.environ            # popped -> api.openai.com default
    assert "OPENAI_BASE_URL" not in os.environ
    assert "OPENROUTER_BASE_URL" not in os.environ        # openai never sets it


def test_build_llm_coder_pins_model_name_env(monkeypatch):
    import os
    import research.hermes.llm_client as mod
    class C:
        def bind(self, **kw): return self
        def invoke(self, m): return FakeMsg("c")
    monkeypatch.setattr(mod, "_agent_build_llm", lambda **k: C(), raising=False)
    mod.build_llm_coder(provider="openai", model="gpt-4o-mini")
    assert os.environ["LANGCHAIN_MODEL_NAME"] == "gpt-4o-mini"


def test_build_openrouter_coder_still_sets_provider_and_base(monkeypatch):
    import os
    import research.hermes.llm_client as mod
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    class C:
        def bind(self, **kw): return self
        def invoke(self, m): return FakeMsg("c")
    monkeypatch.setattr(mod, "_agent_build_llm", lambda **k: C(), raising=False)
    mod.build_openrouter_coder(model="x/y")               # back-compat wrapper
    assert os.environ["LANGCHAIN_PROVIDER"] == "openrouter"
    assert os.environ["OPENROUTER_BASE_URL"] == "https://openrouter.ai/api/v1"
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_llm_client.py -k "chatcoder or build_llm_coder" -v`
Expected: FAIL (`ChatCoder` / `build_llm_coder` do not exist).

- [ ] **Step 3: Implement**

Rename the class and add the alias (llm_client.py:59-60), and generalize the error message:

```python
class ChatCoder:
    """Adapts a bound ChatOpenAI (any provider) to forge's LLMCoder.complete."""

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
            raise LLMUnavailable(f"LLM backend unavailable: "
                                 f"{type(exc).__name__}: {exc}") from exc
        usage = getattr(msg, "usage_metadata", None)
        if isinstance(usage, dict):
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)
        return _flatten_content(msg.content)


OpenRouterCoder = ChatCoder      # back-compat alias
```

Replace `build_openrouter_coder` (llm_client.py:82-100) with the generic factory + a thin wrapper:

```python
_PROVIDER_BASE_DEFAULTS = {"openrouter": "https://openrouter.ai/api/v1"}
_SUPPORTED_PROVIDERS = ("openrouter", "openai")


def build_llm_coder(*, provider: str, model: str, max_tokens: int = 2048) -> ChatCoder:
    """Build a ChatCoder from the agent's ChatOpenAI for `provider`.

    Deterministic regardless of prior os.environ: validates the provider first
    (capabilities silently falls back to openai for an unknown name), clears the
    shared OPENAI_API_BASE/OPENAI_BASE_URL that _sync_provider_env writes (so an
    earlier build cannot misroute this provider's key), and pins provider+model.
    Only openrouter needs a base-url default; openai falls back to api.openai.com.
    Import/signature break -> clear RuntimeError (the coupling's fuse)."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider {provider!r}; "
                         f"use one of {_SUPPORTED_PROVIDERS}")
    os.environ["LANGCHAIN_PROVIDER"] = provider
    os.environ["LANGCHAIN_MODEL_NAME"] = model
    os.environ.pop("OPENAI_API_BASE", None)          # clear cross-provider residue
    os.environ.pop("OPENAI_BASE_URL", None)
    base = _PROVIDER_BASE_DEFAULTS.get(provider)
    if base:                                          # openrouter only
        os.environ.setdefault(f"{provider.upper()}_BASE_URL", base)
    try:
        chat = _agent_build_llm(model_name=model)
        bound = chat.bind(max_tokens=max_tokens)
    except Exception as exc:      # noqa: BLE001 - fail loud on any coupling break
        raise RuntimeError(
            f"could not build the agent {provider} client (model={model!r}): "
            f"{type(exc).__name__}: {exc}") from exc
    return ChatCoder(bound, max_tokens=max_tokens)


def build_openrouter_coder(*, model: str, max_tokens: int = 2048) -> ChatCoder:
    """Back-compat wrapper: build_llm_coder(provider='openrouter')."""
    return build_llm_coder(provider="openrouter", model=model, max_tokens=max_tokens)
```

Also update the module docstring's first line (llm_client.py:1) from "the agent's OpenRouter ChatOpenAI" to "the agent's ChatOpenAI (OpenRouter or OpenAI)".

- [ ] **Step 4: Run — verify pass, whole client suite green**

Run: `python -m pytest research/tests/test_hermes_llm_client.py -v`
Expected: PASS — the new tests plus every existing adapter/`build_openrouter_coder` test (they resolve through the alias and wrapper).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/llm_client.py research/tests/test_hermes_llm_client.py
git commit -m "feat(hermes): generalize LLM client to any provider (ChatCoder + build_llm_coder)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Widen `foundry_runner.build_llm` dispatch to `{openrouter, openai}`

**Files:**
- Modify: `research/hermes/foundry_runner.py` (import line ~17; `build_llm` ~line 146-153)
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `build_llm_coder` (Task 1).
- Produces: `build_llm(spec, *, model=None, max_tokens=2048)` returns a `ChatCoder` for `spec in {"openrouter", "openai"}`.

**Why:** `main`'s `run` already has `--llm` (default `openrouter`) and `--model`; widening the dispatch is the only wiring needed so `--llm openai --model gpt-4o-mini` works. No argparse change.

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_foundry_runner.py (append)
def test_build_llm_openai_returns_a_coder(monkeypatch):
    from research.hermes import foundry_runner as fr
    sentinel = object()
    seen = {}
    def fake_factory(*, provider, model, max_tokens):
        seen.update(provider=provider, model=model); return sentinel
    monkeypatch.setattr(fr, "build_llm_coder", fake_factory)
    assert fr.build_llm("openai", model="gpt-4o-mini") is sentinel
    assert seen == {"provider": "openai", "model": "gpt-4o-mini"}


def test_build_llm_openrouter_still_dispatches(monkeypatch):
    from research.hermes import foundry_runner as fr
    seen = {}
    monkeypatch.setattr(fr, "build_llm_coder",
                        lambda *, provider, model, max_tokens: seen.update(provider=provider))
    fr.build_llm("openrouter", model="x/y")
    assert seen["provider"] == "openrouter"


def test_build_llm_openai_requires_a_model():
    from research.hermes.foundry_runner import build_llm
    with pytest.raises(ValueError, match="model"):
        build_llm("openai", model=None)
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "build_llm_openai or openrouter_still" -v`
Expected: FAIL (`build_llm` only handles `"openrouter"`; `build_llm_coder` not imported).

- [ ] **Step 3: Implement**

Change the import (foundry_runner.py:17) to bring in the generic factory:

```python
from research.hermes.llm_client import build_llm_coder, LLMUnavailable
```

Replace `build_llm` (foundry_runner.py:146-153) — dispatch on the provider allowlist:

```python
def build_llm(spec: str, *, model: str | None = None, max_tokens: int = 2048):
    """Construct the LLM client named by `spec`. For a supported provider the
    model is pinned explicitly (a paid run must never inherit an expensive model
    from a stray LANGCHAIN_MODEL_NAME)."""
    if spec in ("openrouter", "openai"):
        if not model:
            raise ValueError(f"{spec} requires an explicit model (a paid run must "
                             "not read the model from the environment)")
        return build_llm_coder(provider=spec, model=model, max_tokens=max_tokens)
    raise ValueError(f"unknown llm spec {spec!r}")
```

(The `run` subparser already accepts `--llm` and `--model`; no argparse change. The operator runs `--llm openai --model gpt-4o-mini`.)

- [ ] **Step 4: Run — verify pass, full runner + client + orchestrator suites**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py research/tests/test_hermes_llm_client.py -v`
Expected: PASS.

- [ ] **Step 5: Full research suite + commit**

Run: `python -m pytest research/tests/ -q`
Expected: all pass/skip.

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): dispatch build_llm to openai as well as openrouter

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §The change 1 (ChatCoder rename+alias, build_llm_coder, wrapper) → Task 1. ✅
- §The change 2 (build_llm dispatch widen) → Task 2. ✅
- §The change 3 (error handling unchanged) → no task needed; the adapter's `except` list is provider-agnostic (same `openai` SDK types) — Task 1's rename keeps it. ✅
- §agy dispositions: validate-first (Task 1 `test_..._before_any_side_effect`), base-pop (Task 1 `..._clears_cross_provider_base`), model-pin (Task 1 `..._pins_model_name_env`), monkeypatch isolation (every Task 1 test), alias (Task 1). ✅
- §Testing matrix: all rows mapped to Task 1/2 tests. ✅
- Non-goals respected: no third provider, no USD, no budget/gate change. ✅

**Placeholder scan:** none. `gpt-4o-mini` is a concrete model in tests; no TBD/TODO.

**Type consistency:** `ChatCoder` defined in Task 1, aliased `OpenRouterCoder`. `build_llm_coder(*, provider, model, max_tokens)` identical in Task 1 def and Task 2 call. `build_llm(spec, *, model, max_tokens)` matches Task 2 def and its tests. `_SUPPORTED_PROVIDERS`/`_PROVIDER_BASE_DEFAULTS` used only in Task 1.

**Known deferred:** the real OpenAI endpoint is exercised only by the manual paid-run runbook (`--llm openai --model gpt-4o-mini --i-will-spend-real-money`); no automated test spends money.
