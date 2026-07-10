import os

import pytest
from research.hermes.sandbox import DockerSandbox, SandboxRunFailed, is_docker_available
from research.tests.hermes_support import (
    HUNT_DEADLINE_S,
    SandboxContainerGuard,
    call_with_deadline,
)

SANDBOX_TEST_IMAGE = os.environ.get("TALOS_SANDBOX_TEST_IMAGE")
_needs_docker = pytest.mark.skipif(
    not is_docker_available() or not SANDBOX_TEST_IMAGE,
    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE pointing at a deps-baked image")

_BOMB_DEADLINE_S = 120      # OOM-kill is measured at ~11s; timeout_s=90 is the inner bound


@_needs_docker
def test_memory_bomb_is_killed_and_reported_as_repairable(tmp_path):
    """The container's --memory cgroup is the memory defence. Prove it kills the
    bomb, that the failure surfaces as repairable (not infra), and that the host
    is unharmed with no orphaned container."""
    import pandas as pd
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    bomb = ("import numpy as np\nimport pandas as pd\n"
            "def compute(df):\n"
            "    x = np.ones((20000, 20000))\n"     # ~3.2 GB, far past --memory
            "    return df['close'] * x.sum()\n")
    sb = DockerSandbox(image=SANDBOX_TEST_IMAGE, memory="256m", timeout_s=90,
                       allow_unpinned=True)

    with SandboxContainerGuard() as guard:
        thread, box = call_with_deadline(
            lambda: sb.run(bomb, input_parquet=str(inp), output_dir=str(tmp_path)),
            _BOMB_DEADLINE_S)
        assert not thread.is_alive(), f"run() hung past {_BOMB_DEADLINE_S}s"
        exc = box.get("exc")
        assert isinstance(exc, SandboxRunFailed), f"expected SandboxRunFailed, got {box!r}"
        assert exc.oom is True, f"not flagged OOM: exit={exc.exit_code}"
        assert guard.new() == [], "leaked container(s)"


@_needs_docker
def test_infinite_loop_times_out_as_repairable(tmp_path):
    """Bounded by its own wall clock: a test for a timeout that can itself wait
    forever cannot detect a broken timeout -- it just becomes the hang."""
    import pandas as pd
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    spin = "import pandas as pd\ndef compute(df):\n    while True:\n        pass\n"
    sb = DockerSandbox(image=SANDBOX_TEST_IMAGE, memory="256m", timeout_s=5,
                       allow_unpinned=True)

    with SandboxContainerGuard() as guard:
        thread, box = call_with_deadline(
            lambda: sb.run(spin, input_parquet=str(inp), output_dir=str(tmp_path)))
        assert not thread.is_alive(), f"run() hung past {HUNT_DEADLINE_S}s despite timeout_s=5"
        exc = box.get("exc")
        assert isinstance(exc, SandboxRunFailed) and "timed out" in str(exc), f"got {box!r}"
        assert guard.new() == [], "leaked container(s)"
