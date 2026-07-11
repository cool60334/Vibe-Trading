import ast
import os
import subprocess
from pathlib import Path

import pytest
from research.hermes.sandbox import DockerSandbox, is_docker_available
from research.hermes.sandbox_ast import UnsafeCodeError
from research.tests.hermes_support import FakePopen

RUNNER_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "hermes" / "_runner_template.py"

# The container e2e needs a deps-baked image, NOT DockerSandbox's DEFAULT_IMAGE
# (python:3.11-slim has no pandas/pyarrow — a run against it fails, it does not
# skip). Build Dockerfile.sandbox-example and point this env at the tag, e.g.
#   docker build -f Dockerfile.sandbox-example -t talos-sandbox:test .
#   TALOS_SANDBOX_TEST_IMAGE=talos-sandbox:test python -m pytest research/tests/test_hermes_sandbox.py
SANDBOX_TEST_IMAGE = os.environ.get("TALOS_SANDBOX_TEST_IMAGE")


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


@pytest.mark.skipif(
    not is_docker_available() or not SANDBOX_TEST_IMAGE,
    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE pointing at a deps-baked image",
)
def test_causal_feature_executes_in_container(tmp_path):
    import pandas as pd
    src = "import pandas as pd\n\ndef compute(df):\n    return df['close'].pct_change(1)\n"
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    # local, hand-built test image: opting out of the digest pin is explicit
    DockerSandbox(image=SANDBOX_TEST_IMAGE, memory="512m", timeout_s=90,
                  allow_unpinned=True).run(
        src, input_parquet=str(inp), output_dir=str(tmp_path)
    )
    result = pd.read_parquet(tmp_path / "candidate.parquet")["candidate"].tolist()
    assert pd.isna(result[0])                # first pct_change is NaN
    assert result[1:] == [1.0, 0.5]          # container really ran compute()


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


# ── image must be pinned by digest before it ever runs real LLM code ────────
#
# agy pre-flight: a mutable :tag can be re-pushed under you between the run that
# vetted a factor and the run that reproduces it. Require image@sha256:... unless
# a caller explicitly opts out (local test images).

def test_run_refuses_unpinned_image(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxError
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    sb = DockerSandbox(image="python:3.11-slim", memory="256m", timeout_s=10)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxError, match="pinned|digest|sha256"):
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))


def test_allow_unpinned_opt_out_is_explicit(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox
    sb = DockerSandbox(image="talos-sandbox:test", allow_unpinned=True)
    assert sb.allow_unpinned is True          # opting out must be a visible choice


def test_digest_pinned_image_passes_validation():
    from research.hermes.sandbox import DockerSandbox, _assert_image_pinned
    _assert_image_pinned("python@sha256:" + "a" * 64, allow_unpinned=False)   # no raise


# ── Task 1: SandboxRunFailed classification (container ran, code failed) ─────
#
# These patch subprocess.Popen, which is what DockerSandbox drives the container
# with. Patching subprocess.run instead leaves the real docker CLI to be spawned
# for real -- the assertions then pass or fail on whatever the daemon happened
# to do, which is not a test of anything.

SAFE_SOURCE = "import pandas as pd\ndef compute(df):\n    return df['close']\n"


def _patch_popen(monkeypatch, fake: FakePopen) -> None:
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)


def test_nonzero_exit_is_run_failed_not_infra(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    _patch_popen(monkeypatch, FakePopen(returncode=1, stderr="KeyError: 'nonexistent'"))
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    with pytest.raises(SandboxRunFailed, match="nonexistent"):
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert issubclass(SandboxRunFailed, SandboxError)      # still a SandboxError


def test_oom_detected_by_exit_137(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    _patch_popen(monkeypatch, FakePopen(returncode=137))
    sb = DockerSandbox(allow_unpinned=True, memory="256m", timeout_s=5)
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert ei.value.oom is True and ei.value.exit_code == 137


def test_oom_detected_by_memoryerror_on_exit_1(tmp_path, monkeypatch):
    # agy 3b: CPython raises MemoryError and exits 1 -- the cgroup OOM killer
    # never fires, so exit-code-only detection misses this entirely.
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    _patch_popen(monkeypatch, FakePopen(returncode=1, stderr="Traceback...\nMemoryError\n"))
    sb = DockerSandbox(allow_unpinned=True, memory="256m", timeout_s=5)
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert ei.value.oom is True and ei.value.exit_code == 1


def test_ordinary_error_is_not_flagged_as_oom(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    _patch_popen(monkeypatch, FakePopen(returncode=1, stderr="KeyError: 'nope'"))
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert ei.value.oom is False


def test_timeout_is_run_failed_not_infra(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    _patch_popen(monkeypatch, FakePopen(timeout_on_first_communicate=True))
    # the reap's `docker kill`/`rm`/`ps`; empty `ps` output == container is gone
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    sb = DockerSandbox(allow_unpinned=True, timeout_s=1)
    with pytest.raises(SandboxRunFailed, match="timed out"):   # usually the LLM's infinite loop
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))


def test_daemon_down_stays_infra(monkeypatch, tmp_path):
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: False)
    sb = DockerSandbox(allow_unpinned=True)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxError) as ei:
        sb.run(safe, input_parquet="x", output_dir=str(tmp_path))
    assert not isinstance(ei.value, SandboxRunFailed)      # infra: must abort the sweep


def test_bare_image_id_is_accepted_as_pinned():
    from research.hermes.sandbox import _assert_image_pinned
    _assert_image_pinned("sha256:" + "e" * 64, allow_unpinned=False)   # must not raise


def test_registry_digest_still_accepted():
    from research.hermes.sandbox import _assert_image_pinned
    _assert_image_pinned("python@sha256:" + "a" * 64, allow_unpinned=False)   # must not raise


def test_mutable_tag_still_refused():
    from research.hermes.sandbox import _assert_image_pinned, SandboxError
    with pytest.raises(SandboxError, match="pinned|digest|sha256"):
        _assert_image_pinned("talos-sandbox:test", allow_unpinned=False)
