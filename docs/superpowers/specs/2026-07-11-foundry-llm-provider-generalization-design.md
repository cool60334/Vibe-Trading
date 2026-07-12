# Talos Foundry — LLM Client Provider Generalization (OpenAI)

**Date:** 2026-07-11
**Status:** design approved, pending implementation
**Scope:** generalize the OpenRouter-only LLM client factory so the Foundry can
also run against a direct OpenAI key (which the operator already has). Small
additive delta on `docs/superpowers/specs/2026-07-11-foundry-llm-client-design.md`.

---

## Problem

The Foundry's LLM client (`build_openrouter_coder`) forces
`LANGCHAIN_PROVIDER=openrouter` and sets `OPENROUTER_BASE_URL`. The operator has
no OpenRouter account/key (the only key in the repo is a commented placeholder in
`agent/.env`), but **does** have an active `OPENAI_API_KEY` (agent/.env line 14).
Without a provider seam the paid smoke run cannot happen at all.

The agent's `build_llm` is already provider-agnostic (driven by
`LANGCHAIN_PROVIDER`), and `agent/src/providers/capabilities.py` maps
`openai -> (OPENAI_API_KEY, OPENAI_BASE_URL)`. The adapter (`OpenRouterCoder`) is
also provider-agnostic — it only wraps `ChatOpenAI.invoke`. Only the factory and
`foundry_runner.build_llm`'s dispatch are OpenRouter-specific.

**Constraint note:** the "platform must use OpenRouter" rule
([[vibe_trading_provider_constraint]]) is only about reaching **Claude/Anthropic**.
Generating factor code with OpenAI's own models is unaffected.

---

## Non-goals

- Any provider beyond `openrouter` and `openai` (add more when a key exists for
  them; YAGNI).
- Per-provider pricing / USD accounting (unchanged: calls + tokens only).
- Changing the batch budget, spend gate, or error classification — all reused
  as-is.

---

## The change

### 1. `research/hermes/llm_client.py`

- Rename the adapter `OpenRouterCoder` → `ChatCoder` (it wraps any provider's
  `ChatOpenAI`; the old name lies when used for OpenAI). Keep
  `OpenRouterCoder = ChatCoder` as a back-compat alias so existing imports/tests
  keep working.
- Add a provider-parameterized factory:

```python
_PROVIDER_BASE_DEFAULTS = {"openrouter": "https://openrouter.ai/api/v1"}
_SUPPORTED_PROVIDERS = ("openrouter", "openai")

def build_llm_coder(*, provider: str, model: str, max_tokens: int = 2048) -> ChatCoder:
    """Build a ChatCoder from the agent's ChatOpenAI for `provider`.

    Sets LANGCHAIN_PROVIDER so the agent resolves the right key/base-url. Only
    openrouter needs a base-url default; openai uses OPENAI_BASE_URL or
    ChatOpenAI's own api.openai.com default. The model is pinned explicitly so a
    stray LANGCHAIN_MODEL_NAME cannot hijack an expensive model on a paid run.
    Any import/signature break is re-raised as a clear RuntimeError."""
```

- `build_openrouter_coder(*, model, max_tokens=2048)` becomes a thin wrapper:
  `return build_llm_coder(provider="openrouter", model=model, max_tokens=max_tokens)`.

### 2. `research/hermes/foundry_runner.py`

- `build_llm(spec, *, model=None, max_tokens=2048)`: accept `spec in {"openrouter",
  "openai"}` and dispatch to `build_llm_coder(provider=spec, ...)`. The explicit-model
  requirement is unchanged.
- `main`'s `run` already has `--llm` (default `openrouter`); no argparse change —
  the operator passes `--llm openai --model gpt-4o-mini`.

### 3. Error handling — unchanged

OpenAI direct uses the same `openai` SDK exceptions the adapter already catches
(`AuthenticationError`, `RateLimitError`, ...), so auth/quota failures still
surface as `LLMUnavailable` and abort the batch. No change.

---

## Testing (no test spends money)

Add to `research/tests/test_hermes_llm_client.py`:

| test | assertion |
|---|---|
| `test_build_llm_coder_openai_sets_provider_and_no_openrouter_base` | `provider="openai"` → `LANGCHAIN_PROVIDER=openai`; `OPENROUTER_BASE_URL` **not** set by the factory |
| `test_build_llm_coder_openrouter_still_sets_base` | `provider="openrouter"` → base-url default set (back-compat) |
| `test_build_llm_coder_rejects_unknown_provider` | `provider="anthropic"` → `ValueError` |
| `test_openrouter_coder_alias_points_at_chatcoder` | `OpenRouterCoder is ChatCoder` |

Add to `research/tests/test_hermes_foundry_runner.py`:

| test | assertion |
|---|---|
| `test_build_llm_openai_returns_a_coder` | `build_llm("openai", model="gpt-4o-mini")` → a `ChatCoder` (monkeypatched factory) |

All existing `build_openrouter_coder` / adapter tests must stay green via the
alias and wrapper.

---

## Paid-run runbook (OpenAI variant)

Not a test. Run once, after explicit approval, with the operator's existing
`OPENAI_API_KEY` (resolved from `agent/.env`):

```bash
export LANGCHAIN_PROVIDER=openai        # the factory also sets this
export MAX_RETRIES=1
python -m research.hermes.foundry_runner run \
  --runs-dir runs --manifests-dir research/manifests \
  --zoo-dir agent/src/factors/zoo --image talos-sandbox:test \
  --llm openai --model gpt-4o-mini --i-will-spend-real-money \
  --batch-max-llm-calls 6 --max-tokens 2048
```

Estimated cost: `gpt-4o-mini` (~$0.15/$0.60 per M) under a 6-call / 2048-token cap
≈ **1–2 US cents**, hard-bounded.

---

## Self-review

- **Placeholders:** none. `gpt-4o-mini` is a concrete recommended model; the
  operator may override via `--model`.
- **Consistency:** `build_llm_coder(*, provider, model, max_tokens)` is used
  identically by the wrapper and `build_llm`. `ChatCoder` (aliased
  `OpenRouterCoder`) is one class. The spend gate, batch budget, and
  `LLMUnavailable` abort are reused unchanged.
- **Scope:** a single small delta — one generic factory + a two-value dispatch +
  a rename-with-alias. No new subsystem.
- **Ambiguity:** supported providers are an explicit allowlist
  (`_SUPPORTED_PROVIDERS`); an unknown provider is a loud `ValueError`.
