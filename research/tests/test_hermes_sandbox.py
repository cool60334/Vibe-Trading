import shutil
import subprocess
import pytest
from research.hermes.sandbox import DockerSandbox, is_docker_available
from research.hermes.sandbox_ast import UnsafeCodeError


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
    assert "/out:rw" in " ".join(cmd) or ":rw" in joined


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
