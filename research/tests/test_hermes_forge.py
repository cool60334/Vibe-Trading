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
