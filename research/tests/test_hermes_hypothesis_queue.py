import pytest

from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO, SOURCE_LLM
from research.hermes.hypothesis_queue import dedupe


def test_dedupe_keeps_executable_on_collision():
    latex = Hypothesis("zoo_a", "close.shift(5)", SOURCE_ZOO)
    py = Hypothesis("llm_a", "close.shift(5)", SOURCE_LLM)        # same fingerprint
    # both same fingerprint; keep the executable one regardless of order
    out = dedupe([latex, py])
    assert len(out) == 1 and out[0].id == "llm_a"
    out2 = dedupe([py, latex])
    assert len(out2) == 1 and out2[0].id == "llm_a"


def test_dedupe_distinct_fingerprints_all_kept():
    a = Hypothesis("a", "close.shift(5)", SOURCE_ZOO)
    b = Hypothesis("b", "close.shift(9)", SOURCE_ZOO)
    assert len(dedupe([a, b])) == 2
