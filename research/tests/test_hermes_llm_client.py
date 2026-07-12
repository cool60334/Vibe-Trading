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


@pytest.fixture(autouse=True)
def _clean_llm_client_env(monkeypatch):
    """build_llm_coder/build_openrouter_coder write LANGCHAIN_PROVIDER,
    LANGCHAIN_MODEL_NAME, and *_BASE_URL keys straight to os.environ (not
    through monkeypatch), so a bare monkeypatch.delenv(..., raising=False)
    called before the key exists records no undo entry and the value leaks
    into later tests. Scrub before every test so each starts from a known
    state regardless of what an earlier test in this file left behind."""
    for key in ("LANGCHAIN_PROVIDER", "LANGCHAIN_MODEL_NAME",
                "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENROUTER_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    yield


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
