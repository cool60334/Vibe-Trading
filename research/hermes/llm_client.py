"""Talos Foundry LLM client: a thin adapter making the agent's OpenRouter
ChatOpenAI satisfy forge's LLMCoder protocol (.complete(prompt) -> str).

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
