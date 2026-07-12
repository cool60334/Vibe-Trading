"""Talos Foundry LLM client: a thin adapter making the agent's ChatOpenAI
(OpenRouter or OpenAI) satisfy forge's LLMCoder protocol (.complete(prompt) -> str).

The heavy lifting (OpenRouter base URL, key, reasoning handling, retries,
timeout) lives in agent/src/providers/llm.py; this module only adapts its
LangChain ChatOpenAI to a plain-string interface and classifies provider
failures as LLMUnavailable so the batch aborts instead of burning retries."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from research.hermes.errors import HermesGuardError

_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _agent_build_llm(*, model_name, callbacks=None):
    """Import and call agent/src/providers/llm.py's build_llm. Isolated in one
    function so tests can monkeypatch it and the fail-loud fuse has one seam."""
    if str(_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_DIR))
    from src.providers.llm import build_llm as agent_build_llm
    return agent_build_llm(model_name=model_name, callbacks=callbacks)


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
