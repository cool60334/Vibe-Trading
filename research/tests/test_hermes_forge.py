import pytest
from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
from research.hermes.forge import build_prompt, LLMCoder


def test_prompt_includes_hypothesis_and_contract():
    h = Hypothesis("h1", "zscore(funding_z)", SOURCE_LLM)
    p = build_prompt(h, prior_error=None)
    assert "zscore(funding_z)" in p
    assert "compute(df)" in p                       # required entrypoint contract
    assert "DatetimeIndex" in p                     # index contract (backlog #2)


def test_prompt_appends_prior_code_and_error_for_repair():
    h = Hypothesis("h1", "x", SOURCE_LLM)
    p = build_prompt(h, prior_code="def compute(df):\n    return foo",
                     prior_error="NameError: name 'foo' is not defined")
    assert "NameError" in p and "foo" in p            # agy 5b: prior CODE included too
    assert "previous" in p.lower()


def test_extract_code_pulls_fenced_block():
    from research.hermes.forge import extract_code
    resp = "sure:\n```python\ndef compute(df):\n    return df['close']\n```\ndone"
    assert extract_code(resp).startswith("def compute(df):")


def test_extract_code_accepts_py_shorthand_fence():
    from research.hermes.forge import extract_code
    resp = "```py\ndef compute(df):\n    return df['close']\n```"
    assert extract_code(resp) == "def compute(df):\n    return df['close']"


def test_extract_code_falls_back_to_whole_response_when_no_fence():
    from research.hermes.forge import extract_code
    assert extract_code("def compute(df):\n    return df") == "def compute(df):\n    return df"


def test_generate_code_rejects_unsafe_via_ast_gate():
    from research.hermes.forge import generate_code
    class BadLLM:
        def complete(self, prompt): return "```python\nimport os\ndef compute(df):\n    return df\n```"
    from research.hermes.sandbox_ast import UnsafeCodeError
    with pytest.raises(UnsafeCodeError):
        generate_code(BadLLM(), "prompt")            # AST gate blocks os import


def test_pit_check_flags_future_leak_via_sandbox(tmp_path):
    import numpy as np, pandas as pd
    from research.hermes.forge import pit_check_via_sandbox
    from research.hermes.pit import LookaheadError
    idx = pd.date_range("2024-01-01", periods=300, freq="1h")
    panel = pd.DataFrame({"close": np.arange(300.0)}, index=idx)
    leaky = lambda code, p: p["close"].shift(-1)          # peeks at t+1
    baseline = leaky("code", panel)                       # forge already ran it
    with pytest.raises(LookaheadError):
        pit_check_via_sandbox("code", panel, baseline, run=leaky)


def test_pit_check_passes_causal(tmp_path):
    import numpy as np, pandas as pd
    from research.hermes.forge import pit_check_via_sandbox
    idx = pd.date_range("2024-01-01", periods=300, freq="1h")
    panel = pd.DataFrame({"close": np.arange(300.0)}, index=idx)
    causal = lambda code, p: p["close"].pct_change(5)
    baseline = causal("code", panel)
    assert pit_check_via_sandbox("code", panel, baseline, run=causal) is None
