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


def test_pit_check_rejects_panel_too_small_for_perturb_gap():
    # agy review gap #1: n <= PERTURB_GAP + 1 makes perturb_from <= 0, which
    # would previously make the comparison slice empty and np.allclose pass
    # trivially for ANY code. Must raise instead of silently passing.
    import numpy as np, pandas as pd
    from research.hermes.forge import pit_check_via_sandbox
    idx = pd.date_range("2024-01-01", periods=10, freq="1h")
    panel = pd.DataFrame({"close": np.arange(10.0)}, index=idx)
    leaky = lambda code, p: p["close"].shift(-1)          # blatant lookahead
    baseline = leaky("code", panel)
    with pytest.raises(ValueError, match="out of range"):
        pit_check_via_sandbox("code", panel, baseline, run=leaky)


def test_pit_check_rejects_non_numeric_panel():
    # agy review gap #2: a non-numeric column must fail with a clear error,
    # not an unhandled TypeError from `corrupt.iloc[perturb_from:] = 1e10`.
    import numpy as np, pandas as pd
    from research.hermes.forge import pit_check_via_sandbox
    idx = pd.date_range("2024-01-01", periods=300, freq="1h")
    panel = pd.DataFrame(
        {"close": np.arange(300.0), "label": ["x"] * 300}, index=idx
    )
    causal = lambda code, p: p["close"].pct_change(5)
    baseline = causal("code", panel)
    with pytest.raises(ValueError, match="all-numeric"):
        pit_check_via_sandbox("code", panel, baseline, run=causal)


def test_forge_succeeds_first_try():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class GoodLLM:
        def complete(self, p): return "```python\ndef compute(df):\n    return df['close'].pct_change(5)\n```"
    run = lambda code, pnl: pnl["close"].pct_change(5)
    res = forge(Hypothesis("h", "mom5", SOURCE_LLM), GoodLLM(), run, panel, max_retries=3)
    assert res.success and res.attempts == 1 and res.series is not None


def test_forge_repairs_then_succeeds():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    calls = {"n": 0}
    class FlakyLLM:
        def complete(self, p):
            calls["n"] += 1
            if calls["n"] == 1:
                return "```python\nimport os\ndef compute(df):\n    return df['close']\n```"  # AST fail
            return "```python\ndef compute(df):\n    return df['close'].pct_change(3)\n```"
    run = lambda code, pnl: pnl["close"].pct_change(3)
    res = forge(Hypothesis("h", "x", SOURCE_LLM), FlakyLLM(), run, panel, max_retries=3)
    assert res.success and res.attempts == 2          # repaired after AST rejection


def test_forge_buries_after_max_retries():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class BadLLM:
        def complete(self, p): return "```python\nimport socket\ndef compute(df):\n    return df['close']\n```"
    run = lambda code, pnl: pnl["close"]
    res = forge(Hypothesis("h", "x", SOURCE_LLM), BadLLM(), run, panel, max_retries=3)
    assert not res.success and res.attempts == 3 and res.death_reason
    assert res.code is not None                       # agy 5c: last bad code kept for 1D


def test_forge_reraises_infra_error_without_retrying():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.sandbox import SandboxError
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class GoodLLM:
        def complete(self, p): return "```python\ndef compute(df):\n    return df['close']\n```"
    def broken_infra(code, pnl): raise SandboxError("docker daemon unavailable")
    with pytest.raises(SandboxError):                 # agy 5a: infra error NOT retried
        forge(Hypothesis("h", "x", SOURCE_LLM), GoodLLM(), broken_infra, panel, max_retries=3)


def test_forge_buries_on_stripped_index_with_clear_reason():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class GoodLLM:
        def complete(self, p): return "```python\ndef compute(df):\n    return df['close']\n```"
    strip = lambda code, pnl: pnl["close"].reset_index(drop=True)   # index stripped
    res = forge(Hypothesis("h", "x", SOURCE_LLM), GoodLLM(), strip, panel, max_retries=2)
    assert not res.success and "index" in res.death_reason.lower()  # clear contract msg


def test_forge_buries_cleanly_when_run_sandbox_returns_non_series():
    # code-review fix: hasattr(series, "index") was True even for a plain
    # list (list.index is the unrelated builtin method), so a buggy
    # run_sandbox returning a list crashed forge() with an uncaught
    # AttributeError instead of being recorded as a clean death result.
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class GoodLLM:
        def complete(self, p): return "```python\ndef compute(df):\n    return df['close']\n```"
    not_a_series = lambda code, pnl: pnl["close"].tolist()   # returns list, not Series
    res = forge(Hypothesis("h", "x", SOURCE_LLM), GoodLLM(), not_a_series, panel, max_retries=2)
    assert not res.success and res.death_reason is not None
    assert "index" in res.death_reason.lower() or "series" in res.death_reason.lower()


# ── budget circuit breaker: must trip INSIDE the repair loop ────────────────
#
# agy pre-flight: the orchestrator's early-stop only fires between hypotheses.
# A hypothesis whose code keeps failing burns one LLM call per retry, so a
# nightly run can exhaust an API quota long before early-stop is consulted.
# The budget is charged per llm.complete() and, once exhausted, propagates like
# SandboxError -- it is infrastructure exhaustion, not a repairable code error.

def test_forge_charges_budget_per_llm_call():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge, ForgeBudget
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class GoodLLM:
        def complete(self, p): return "```python\ndef compute(df):\n    return df['close'].pct_change(5)\n```"
    run = lambda code, pnl: pnl["close"].pct_change(5)
    b = ForgeBudget(max_llm_calls=10)
    forge(Hypothesis("h", "x", SOURCE_LLM), GoodLLM(), run, panel, max_retries=3, budget=b)
    assert b.used == 1                                   # one call, one charge


def test_forge_budget_exhaustion_propagates_and_is_not_retried():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge, ForgeBudget, BudgetExhausted
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    calls = {"n": 0}
    class BadLLM:
        def complete(self, p):
            calls["n"] += 1
            return "```python\nimport socket\ndef compute(df):\n    return df['close']\n```"
    run = lambda code, pnl: pnl["close"]
    b = ForgeBudget(max_llm_calls=2)                      # trips on the 3rd attempt
    with pytest.raises(BudgetExhausted):
        forge(Hypothesis("h", "x", SOURCE_LLM), BadLLM(), run, panel, max_retries=5, budget=b)
    assert calls["n"] == 2                               # never called a 3rd time
