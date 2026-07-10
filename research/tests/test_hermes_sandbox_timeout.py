import subprocess
import pytest
from research.hermes.sandbox import DockerSandbox, SandboxError


def test_timeout_kills_container_not_just_cli(monkeypatch):
    sb = DockerSandbox(memory="256m", timeout_s=1, allow_unpinned=True)
    killed = {}
    # simulate the run exceeding its wall-clock budget
    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd, 1)
        if cmd[:2] == ["docker", "kill"] or cmd[:2] == ["docker", "rm"]:
            killed[cmd[1]] = cmd[2] if len(cmd) > 2 else True
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SandboxError):
        sb.run("import pandas as pd\ndef compute(df):\n    return df['close']\n",
               input_parquet="x.parquet", output_dir="/tmp/o")
    assert "kill" in killed or "rm" in killed          # container reaped on timeout


# agy 1c: the mock test above only proves the kill LOGIC fires; add a REAL
# integration test that the daemon actually reaps the container.
@pytest.mark.skipif(not __import__("research.hermes.sandbox", fromlist=["is_docker_available"]).is_docker_available()
                    or not __import__("os").environ.get("TALOS_SANDBOX_TEST_IMAGE"),
                    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE")
def test_real_container_is_gone_after_timeout(tmp_path, monkeypatch):
    import os, pandas as pd
    img = os.environ["TALOS_SANDBOX_TEST_IMAGE"]
    inp = tmp_path / "in.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}).to_parquet(inp)
    sb = DockerSandbox(image=img, memory="256m", timeout_s=2, allow_unpinned=True)
    # an infinite loop the AST gate allows (no banned calls) -> must be reaped
    infinite = "import pandas as pd\ndef compute(df):\n    while True:\n        pass\n"
    with pytest.raises(SandboxError):
        sb.run(infinite, input_parquet=str(inp), output_dir=str(tmp_path))
    # no talos_sbx_* container may survive
    out = subprocess.run(["docker", "ps", "-q", "-f", "name=talos_sbx_"],
                         capture_output=True, text=True, timeout=15)
    assert out.stdout.strip() == "", f"leaked container(s): {out.stdout!r}"
