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
