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
