import ast
from pathlib import Path

import pytest
from research.hermes.sandbox import DockerSandbox, is_docker_available
from research.hermes.sandbox_ast import UnsafeCodeError

RUNNER_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "hermes" / "_runner_template.py"


def test_ast_gate_runs_before_docker():
    # unsafe source must be rejected WITHOUT ever invoking docker
    sb = DockerSandbox(memory="512m", timeout_s=30)
    with pytest.raises(UnsafeCodeError):
        sb.run("import os\nos.system('echo hi')\n", input_parquet=None, output_dir="/tmp")


def test_docker_flags_are_hardened():
    sb = DockerSandbox(memory="512m", timeout_s=30)
    cmd = sb._build_command("/in/x.parquet", "/out", runner="/app/runner.py")
    joined = " ".join(cmd)
    assert "--network=none" in joined
    assert "--memory=512m" in joined
    assert "--read-only" in joined
    assert ":rw" in joined


@pytest.mark.skipif(not is_docker_available(), reason="docker daemon not running")
def test_causal_feature_executes_in_container(tmp_path):
    import pandas as pd
    src = "import pandas as pd\n\ndef compute(df):\n    return df['close'].pct_change(1)\n"
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    out = sb_run_out = DockerSandbox(memory="512m", timeout_s=60).run(
        src, input_parquet=str(inp), output_dir=str(tmp_path)
    )
    assert (tmp_path / "candidate.parquet").exists()


# --- exec-scoping regression coverage (no docker required) -----------------
#
# _runner_template.py execs the (already AST-gated) LLM-authored source
# inside the container. If that exec() is given two SEPARATE dicts for
# globals/locals, Python applies class-body scoping: top-level imports and
# helper defs land only in the locals dict, but any function defined by the
# source (including `compute`) has its __globals__ bound to the globals dict
# only. Since only `pd` is pre-injected into globals, `compute` would raise
# NameError for numpy/scipy/ta/math/statistics imports and for any sibling
# top-level helper function it calls. The fix is to exec() the source into a
# single shared namespace dict used as both globals and locals.

_MULTI_IMPORT_HELPER_SOURCE = (
    "import numpy as np\n"
    "def _helper(x):\n"
    "    return x * 2\n"
    "def compute(df):\n"
    "    return np.log(_helper(df['close']))\n"
)


def test_two_dict_exec_pattern_raises_nameerror_on_sibling_import_and_helper():
    """Documents the OLD bug: globals/locals split breaks compute's visibility
    into sibling top-level imports (np) and helper functions (_helper)."""
    import pandas as pd

    ns: dict = {}
    exec(_MULTI_IMPORT_HELPER_SOURCE, {"pd": pd, "__builtins__": __builtins__}, ns)
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    with pytest.raises(NameError):
        ns["compute"](df)


def test_single_shared_namespace_exec_resolves_sibling_import_and_helper():
    """The fixed pattern: one dict used as both globals and locals lets
    compute() see co-defined imports (np) and helper functions (_helper)."""
    import pandas as pd

    ns: dict = {"pd": pd, "__builtins__": __builtins__}
    exec(_MULTI_IMPORT_HELPER_SOURCE, ns)
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    result = ns["compute"](df)
    expected = (df["close"] * 2).apply(__import__("math").log)
    assert list(result) == list(expected)


def test_runner_template_execs_with_single_shared_namespace():
    """Regression guard on the actual shipped file: the exec() call inside
    _runner_template.py must be called with exactly one namespace argument
    (source, ns) — not (source, globals_dict, locals_dict) — so it can never
    regress to the two-dict class-body-scoping bug."""
    tree = ast.parse(RUNNER_TEMPLATE_PATH.read_text())
    exec_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "exec"
    ]
    assert len(exec_calls) == 1, "expected exactly one exec() call in the runner template"
    assert len(exec_calls[0].args) == 2, (
        "exec() must be called with (source, ns) — a single shared namespace — "
        "not (source, globals_dict, locals_dict), which triggers class-body "
        "scoping and hides sibling imports/helpers from compute()"
    )
