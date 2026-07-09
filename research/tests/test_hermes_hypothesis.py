import pytest
from research.hermes.hypothesis import (
    Hypothesis, string_fingerprint, is_python_expr, SOURCE_ZOO,
)


def test_fingerprint_normalises_whitespace_and_case():
    assert string_fingerprint("close.shift(5)") == string_fingerprint("  CLOSE.shift(5)  ")


def test_fingerprint_distinguishes_different_input_columns():
    # agy 二審: AST-normalising vars WRONGLY merged these; string hash keeps them apart
    assert string_fingerprint("close / close.shift(5) - 1") != \
           string_fingerprint("volume / volume.shift(5) - 1")


def test_is_python_expr_detects_executable():
    assert is_python_expr("close / close.shift(5) - 1") is True
    assert is_python_expr(r"\mathrm{close}_t / \mathrm{close}_{t-5} - 1") is False   # LaTeX


def test_hypothesis_autofills_fingerprint_and_rejects_bad_source():
    h = Hypothesis(id="zoo_roc5", description="close/close.shift(5)-1", source=SOURCE_ZOO)
    assert len(h.fingerprint) == 64
    with pytest.raises(ValueError):
        Hypothesis(id="x", description="y", source="bogus")
