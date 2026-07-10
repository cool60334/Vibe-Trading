import os
import subprocess

import pytest
from research.hermes.sandbox import DockerSandbox, SandboxRunFailed, is_docker_available

SANDBOX_TEST_IMAGE = os.environ.get("TALOS_SANDBOX_TEST_IMAGE")
_needs_docker = pytest.mark.skipif(
    not is_docker_available() or not SANDBOX_TEST_IMAGE,
    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE pointing at a deps-baked image")


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
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(bomb, input_parquet=str(inp), output_dir=str(tmp_path))
    assert ei.value.oom is True, f"not flagged OOM: exit={ei.value.exit_code}"
    out = subprocess.run(["docker", "ps", "-q", "-f", "name=talos_sbx_"],
                         capture_output=True, text=True, timeout=15)
    assert out.stdout.strip() == "", f"leaked container(s): {out.stdout!r}"


@_needs_docker
def test_infinite_loop_times_out_as_repairable(tmp_path):
    import pandas as pd
    inp = tmp_path / "in.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    spin = "import pandas as pd\ndef compute(df):\n    while True:\n        pass\n"
    sb = DockerSandbox(image=SANDBOX_TEST_IMAGE, memory="256m", timeout_s=5,
                       allow_unpinned=True)
    with pytest.raises(SandboxRunFailed, match="timed out"):
        sb.run(spin, input_parquet=str(inp), output_dir=str(tmp_path))
    out = subprocess.run(["docker", "ps", "-q", "-f", "name=talos_sbx_"],
                         capture_output=True, text=True, timeout=15)
    assert out.stdout.strip() == "", f"leaked container(s): {out.stdout!r}"
