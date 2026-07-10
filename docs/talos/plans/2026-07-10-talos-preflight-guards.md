# Talos Pre-flight Guards — Sandbox Failure Classification (v2, agy-reviewed)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 補上 Foundry 真跑前的最後技術護欄。原本列的兩項是「AST 擋記憶體炸彈」與「相同 code hash 短路」；agy 二審後兩者都**縮小到真正有效的那一半**——記憶體防線本來就在 `--memory` cgroup，真正的 bug 是**把容器內的失敗誤判成基礎設施故障**；code hash 短路只有**迴圈內**版本安全。

**Architecture:** 改 `research/hermes/sandbox.py`（失敗分類）與 `forge.py`（可修復路徑、迴圈內短路）。**不動** `sandbox_ast.py`、**不動** `orchestrator.py`。

> **⚠️ v2 = agy 二審後重寫。** 砍掉兩個 Task：
> - **AST 字面量配置啟發式（原 Task 4）——刪除。** `np.random.rand()` 是雙層 `Attribute`，名稱抽取抓不到；`np.arange(0, 10**9)` 的 `args[0]` 是 `0`，會被算成 0 個元素（語意全錯）；任何叫 `zeros`/`full` 的自訂方法傳大數都會誤殺。真正的防線是 `--memory` cgroup，這個啟發式只增複雜度不增安全。
> - **跨 run `code_sha256` 短路（原 Task 6）——刪除。** `oos_start` 隨季度滾動：一段碼在舊窗被埋，在新窗可能是合格因子。只鍵 `code_sha256` 會讓它**永遠失去重評機會**——一個靜默扼殺好因子的 false negative。要做對得鍵到 `(code_sha256, oos_start, interval, features_hash)`，複雜度遠超收益（1B 已用 description fingerprint 去重）。

---

## 前置（本計畫的真正主體）：`SandboxError` 把「LLM 爛碼」誤判成「基礎設施故障」

agy 核實無誤：`sandbox.py:187` 對**任何** `returncode != 0` 拋 `SandboxError`；`forge.py:183` 的第一個 `except SandboxError: raise` 把它當基礎設施錯**向上拋、不重試**（agy 5a 的裁定，對 daemon 掛掉是對的）。`_runner_template.py` 沒有 try/except，`orchestrator.make_run_sandbox` 也沒有防護。所以：

| LLM 幹的事 | 容器結果 | 現況 |
|---|---|---|
| `df['nonexistent']` → KeyError | 退出碼 1 | `SandboxError` → **整個 nightly sweep 死** |
| `while True:` | timeout | `SandboxError` → **整個 sweep 死** |
| `np.zeros((10000, 10000))` | OOM-kill，退出碼 137 | `SandboxError` → **整個 sweep 死** |

**「有界修復迴圈」實際只能修 3 種錯**（AST 閘拒絕、index 契約違反、PIT 洩漏）——**容器內的執行期錯誤一個都修不了**，還會炸掉整晚。這是本專案第 6 個「防線存在但沒作用」。

**記憶體炸彈的防線一直都在**（`_build_command` 的 `--memory` cgroup）。它會殺掉炸彈、不會拖垮宿主機。缺的是：**把那次 OOM 正確歸類成「LLM 的錯、可修復」**，而不是炸掉整晚。

---

## File Structure

| 檔案 | 改動 |
|------|------|
| `research/hermes/sandbox.py` | `SandboxRunFailed(SandboxError)`；退出碼 + stderr 關鍵字分類；帶回 stderr |
| `research/hermes/forge.py` | `SandboxRunFailed` 走可修復路徑；迴圈內相同 code 短路 |
| `research/tests/test_hermes_sandbox.py` | 分類單測 |
| `research/tests/test_hermes_sandbox_limits.py`（新） | docker-gated 真容器：記憶體炸彈、無窮迴圈 |
| `research/tests/test_hermes_forge.py` | 可修復 vs infra、短路 |

**貫穿原則（本專案 6 次 bug 的共同教訓）**：每道護欄都要有一條「**生產路徑真的呼叫了它**」的測試；能用真容器就別只用 mock。

---

## Task 1: 拆分沙盒失敗語意（可修復 vs 基礎設施）

**Files:** Modify `research/hermes/sandbox.py`; Test `research/tests/test_hermes_sandbox.py`.

`SandboxError` 保持「基礎設施」語意（daemon 掛、image 未 pin）並繼續向上拋。新增子類 `SandboxRunFailed`：**容器跑起來了，但 LLM 的碼失敗**——非零退出碼、OOM、wall-clock timeout。帶回 stderr 供修復回饋。

**OOM 判定（agy 3b）**：不能只看 `exit == 137`。Python 內存配置失敗會拋 `MemoryError` 並**優雅退出（exit 1）**，根本不觸發 cgroup OOM-killer。必須 `exit == 137` **或** stderr 含 `MemoryError`/`Killed`。

**timeout 歸類（agy 2）**：`subprocess.run(timeout=)` 量的是整個 `docker run` 的牆鐘時間，宿主機 I/O 壅塞或 daemon 卡住也會觸發，因此把它一律歸給 LLM **會有 infra 誤判**。可接受：`max_retries` 上限 + `should_early_stop` 連敗中止已經界定損失，不會在宿主機爆掉時無限重試。**這個取捨要寫進 docstring，不是留在腦子裡。**

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_sandbox.py
def test_nonzero_exit_is_run_failed_not_infra(tmp_path, monkeypatch):
    import subprocess
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    class R: returncode = 1; stdout = ""; stderr = "KeyError: 'nonexistent'"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxRunFailed, match="nonexistent"):
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert issubclass(SandboxRunFailed, SandboxError)      # still a SandboxError


def test_oom_detected_by_exit_137(tmp_path, monkeypatch):
    import subprocess
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    class R: returncode = 137; stdout = ""; stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    sb = DockerSandbox(allow_unpinned=True, memory="256m", timeout_s=5)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert ei.value.oom is True and ei.value.exit_code == 137


def test_oom_detected_by_memoryerror_on_exit_1(tmp_path, monkeypatch):
    # agy 3b: CPython raises MemoryError and exits 1 -- the cgroup OOM killer
    # never fires, so exit-code-only detection misses this entirely.
    import subprocess
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    class R: returncode = 1; stdout = ""; stderr = "Traceback...\nMemoryError\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    sb = DockerSandbox(allow_unpinned=True, memory="256m", timeout_s=5)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert ei.value.oom is True and ei.value.exit_code == 1


def test_ordinary_error_is_not_flagged_as_oom(tmp_path, monkeypatch):
    import subprocess
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    class R: returncode = 1; stdout = ""; stderr = "KeyError: 'nope'"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxRunFailed) as ei:
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert ei.value.oom is False


def test_timeout_is_run_failed_not_infra(tmp_path, monkeypatch):
    import subprocess
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd, 1)
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    sb = DockerSandbox(allow_unpinned=True, timeout_s=1)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxRunFailed, match="timed out"):   # usually the LLM's infinite loop
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))


def test_daemon_down_stays_infra(monkeypatch, tmp_path):
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: False)
    sb = DockerSandbox(allow_unpinned=True)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxError) as ei:
        sb.run(safe, input_parquet="x", output_dir=str(tmp_path))
    assert not isinstance(ei.value, SandboxRunFailed)      # infra: must abort the sweep
```

- [ ] **Step 2: Run — fails (`SandboxRunFailed` does not exist).**

- [ ] **Step 3: Implement**

```python
# research/hermes/sandbox.py
_OOM_EXIT_CODE = 137          # 128 + SIGKILL(9): what the cgroup OOM killer leaves behind
_OOM_STDERR_MARKERS = ("MemoryError", "Killed")


class SandboxRunFailed(SandboxError):
    """The container ran, but the LLM's code failed inside it.

    Distinct from a bare SandboxError (docker daemon down, image not pinned):
    those are infrastructure faults that must abort the sweep, whereas a
    non-zero exit, an OOM kill, or a wall-clock timeout are caused by the code
    under test and are exactly what forge()'s bounded repair loop exists for.
    Before this split every bad LLM factor killed the entire nightly run.

    Timeout is classified here too, and that is a deliberate trade-off: the
    wall clock covers the whole `docker run`, so a wedged daemon or a saturated
    host can trip it and get blamed on the LLM. The bound on that mistake is
    forge()'s max_retries plus run_foundry()'s should_early_stop -- a wedged
    host burns a few retries and then the sweep stops. The common case, by far,
    is an LLM writing `while True`.
    """
    def __init__(self, message: str, *, exit_code: int | None = None, oom: bool = False):
        super().__init__(message)
        self.exit_code = exit_code
        self.oom = oom


def _looks_like_oom(exit_code: int, stderr: str) -> bool:
    """Exit code 137 OR a MemoryError/Killed marker in stderr.

    Exit-code-only detection misses the common case: CPython raises MemoryError
    and exits 1 long before the cgroup OOM killer fires. `docker inspect
    .State.OOMKilled` is not available to us -- `--rm` has already reaped the
    container by the time we look. This is a heuristic; the message says
    "looks OOM-killed", never asserts it as the sole cause.
    """
    if exit_code == _OOM_EXIT_CODE:
        return True
    return any(m in stderr for m in _OOM_STDERR_MARKERS)
```

`run()` 內：timeout 分支改拋 `SandboxRunFailed(f"sandbox timed out after {self.timeout_s}s; container {name} reaped", exit_code=None)`（獵殺容器的邏輯**不變**）。非零退出改：

```python
            if proc.returncode != 0:
                stderr = proc.stderr or ""
                oom = _looks_like_oom(proc.returncode, stderr)
                hint = (f"; looks OOM-killed — the code exceeded --memory={self.memory}"
                        if oom else "")
                raise SandboxRunFailed(
                    f"sandbox run failed (exit {proc.returncode}){hint}\n{stderr[-800:]}",
                    exit_code=proc.returncode, oom=oom,
                )
```

> **agy 3a 已查核無誤**：我們自己的 timeout 路徑先 `catch TimeoutExpired` 並拋出，**永遠走不到** `returncode == 137` 的檢查，所以「自家 `docker kill` 產生的 137 被誤判成 OOM」不會發生。
>
> 實作前 `Read` 現行 `run()` 的 timeout 獵殺區塊，勿破壞 `test_timeout_kills_container_not_just_cli`（它斷言 kill/rm 被呼叫）。

- [ ] **Step 4: Run — passes；全 sandbox 測試無回歸。**

- [ ] **Step 5: Commit** `fix(hermes): classify container-side failures as repairable, not infra`

---

## Task 2: forge 把 `SandboxRunFailed` 當可修復，並把 stderr 餵回 LLM

**Files:** Modify `research/hermes/forge.py`; Test `research/tests/test_hermes_forge.py`.

`except` 順序關鍵：`SandboxRunFailed` 必須排在 `SandboxError` **之前**（子類先攔），否則永遠走不到可修復路徑。

- [ ] **Step 1: Failing test**

```python
def test_container_runtime_error_is_repaired_not_propagated():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.sandbox import SandboxRunFailed
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    calls = {"n": 0}
    class LLM:
        def complete(self, p):
            calls["n"] += 1
            if calls["n"] == 1:
                assert "KeyError" not in p                  # first attempt has no feedback
                return "```python\ndef compute(df):\n    return df['nope']\n```"
            assert "KeyError" in p                          # container stderr fed back
            return "```python\ndef compute(df):\n    return df['close'].pct_change(3)\n```"
    def run(code, pnl):
        if "nope" in code:
            raise SandboxRunFailed("sandbox run failed (exit 1)\nKeyError: 'nope'", exit_code=1)
        return pnl["close"].pct_change(3)
    res = forge(Hypothesis("h", "x", SOURCE_LLM), LLM(), run, panel, max_retries=3)
    assert res.success and res.attempts == 2               # repaired; the sweep survives


def test_oom_is_buried_after_retries_not_propagated():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.sandbox import SandboxRunFailed
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    seq = iter(["a", "b"])                                 # distinct code each attempt
    class LLM:
        def complete(self, p):
            return f"```python\nimport numpy as np\ndef compute(df):\n    x = '{next(seq)}'\n    return df['close']\n```"
    def run(code, pnl):
        raise SandboxRunFailed("looks OOM-killed (exit 137)", exit_code=137, oom=True)
    res = forge(Hypothesis("h", "x", SOURCE_LLM), LLM(), run, panel, max_retries=2)
    assert not res.success and "OOM" in res.death_reason   # buried, not raised


def test_infra_error_still_propagates():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.sandbox import SandboxError
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    class LLM:
        def complete(self, p): return "```python\ndef compute(df):\n    return df['close']\n```"
    def run(code, pnl): raise SandboxError("docker daemon unavailable")
    with pytest.raises(SandboxError):
        forge(Hypothesis("h", "x", SOURCE_LLM), LLM(), run, panel, max_retries=3)
```

> `test_oom_is_buried_...` 的 LLM 刻意每次吐**不同**的碼，否則 Task 3 的相同-code 短路會先攔下來，測到的就不是 OOM 埋葬路徑了。

- [ ] **Step 2: Run — fails (前兩個：`SandboxRunFailed` 向上拋).**

- [ ] **Step 3: Implement** — 在 `forge()` 的 except 鏈最前面插入子類分支：

```python
        except SandboxRunFailed as exc:
            # the container ran; the LLM's code is what failed. Feed the container's
            # stderr back verbatim -- it is the most useful repair signal we have.
            last_error = f"SandboxRunFailed: {exc}"
            prior_code, prior_error = code, last_error
        except SandboxError:
            raise                                    # agy 5a: infra, not repairable
        except (UnsafeCodeError, LookaheadError, ValueError, KeyError, TypeError) as exc:
            ...
```

並在 `forge` 的 import 加 `SandboxRunFailed`。

- [ ] **Step 4: Run — passes；`test_forge_reraises_infra_error_without_retrying` 仍綠（它拋裸 `SandboxError`）。**

- [ ] **Step 5: Commit** `fix(hermes): repair container-side code failures instead of aborting the sweep`

---

## Task 3: 迴圈內相同 code 短路

**Files:** Modify `research/hermes/forge.py`; Test `research/tests/test_hermes_forge.py`.

若 LLM 在修復迴圈中吐出**與前次逐字相同**的 code，再跑一次沙盒不會有不同結果——直接埋葬，省下剩餘的 LLM call 與容器啟動。

**順序鎖死（agy 6）**：sha 檢查必須在 `extract_code` **之後**、`check_source` **之前**。若排在 `check_source` 之後，一段 AST 違規的碼會先拋 `UnsafeCodeError` 進可修復分支，短路永遠不會被執行到。下面的測試用 `import socket`（AST 違規）正是為了把這個順序鎖進斷言。

- [ ] **Step 1: Failing test**

```python
def test_identical_repeated_code_short_circuits_before_the_ast_gate():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    calls = {"n": 0}
    class StubbornLLM:
        def complete(self, p):
            calls["n"] += 1
            # AST-illegal: if the sha check ran AFTER check_source this would just
            # loop max_retries times through the repairable branch.
            return "```python\nimport socket\ndef compute(df):\n    return df['close']\n```"
    res = forge(Hypothesis("h", "x", SOURCE_LLM), StubbornLLM(), lambda c, p: p["close"],
                panel, max_retries=5)
    assert not res.success
    assert calls["n"] == 2                      # 2nd attempt repeated verbatim -> bury
    assert "identical" in res.death_reason.lower()


def test_distinct_code_each_attempt_uses_the_full_retry_budget():
    import numpy as np, pandas as pd
    from research.hermes.forge import forge
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    idx = pd.date_range("2024-01-01", periods=200, freq="1h")
    panel = pd.DataFrame({"close": np.arange(200.0)}, index=idx)
    calls = {"n": 0}
    class VariedLLM:
        def complete(self, p):
            calls["n"] += 1
            return f"```python\nimport socket\ndef compute(df):\n    x = {calls['n']}\n    return df['close']\n```"
    res = forge(Hypothesis("h", "x", SOURCE_LLM), VariedLLM(), lambda c, p: p["close"],
                panel, max_retries=3)
    assert not res.success and calls["n"] == 3   # short-circuit must not fire on distinct code
```

- [ ] **Step 2: Run — fails (LLM called 5 times in the first test).**

- [ ] **Step 3: Implement** — `import hashlib`；迴圈前 `seen_shas: set = set()`；取得 `code` 後、進 `try` 之前：

```python
        code_sha = hashlib.sha256(code.encode()).hexdigest()
        if code_sha in seen_shas:
            # the repair feedback produced no change; another sandbox run cannot
            # produce a different outcome, and another LLM call costs budget.
            # NOTE: this check must sit BEFORE check_source() -- an AST-illegal
            # repeat would otherwise be swallowed by the repairable branch.
            return ForgeResult(False, attempt, code=code,
                               death_reason=f"LLM repeated identical code after: {last_error}")
        seen_shas.add(code_sha)
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): bury a hypothesis when the LLM repeats identical code`

---

## Task 4: 真容器整合測試——記憶體炸彈與無窮迴圈

**Files:** Create `research/tests/test_hermes_sandbox_limits.py`.

Task 1–3 是 mock，只證明分類與短路邏輯。**真正的記憶體防線是 `--memory` cgroup**，必須用真容器證明它會殺掉炸彈、浮上來是 `SandboxRunFailed`（可修復）、且不留下孤兒容器。docker-gated，比照既有慣例。

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_sandbox_limits.py
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
```

- [ ] **Step 2: Run with docker up + image built**

```bash
docker build -f Dockerfile.sandbox-example -t talos-sandbox:test .
TALOS_SANDBOX_TEST_IMAGE=talos-sandbox:test python -m pytest research/tests/test_hermes_sandbox_limits.py -v
```
Expected: both PASS. Without daemon/env: SKIP.

- [ ] **Step 3: 觀察真實退出碼再定案。** `assert ei.value.oom is True` 是**故意嚴格**的：它同時檢驗 `_looks_like_oom` 的兩條路徑（137 或 stderr 關鍵字）在真環境中至少有一條命中。若都沒中，**先印出實際 `exit_code` 與 stderr 尾巴，依真實觀察擴充 `_OOM_STDERR_MARKERS`，勿臆測。**

- [ ] **Step 4: Commit** `test(hermes): real-container memory-bomb + infinite-loop limits`

---

## Self-Review

**Spec coverage：** 記憶體炸彈 → Task 1（OOM 正確歸類，含 `MemoryError`/exit-1 漏判）+ Task 4（真容器證 `--memory` 有效）。相同 code 短路 → Task 3（僅迴圈內）。前置 bug → Task 1+2。

**agy 二審砍掉的（連同理由，避免後人重新提案）：**
- **AST 字面量配置啟發式**：`np.random.rand()` 是雙層 `Attribute`（抓不到）；`np.arange(0, 10**9)` 的 `args[0]=0`（判成 0 元素，語意全錯）；任何叫 `zeros`/`full` 的自訂方法傳大數都誤殺。真防線是 `--memory`，這只增複雜度不增安全。
- **跨 run `code_sha256` 短路**：`oos_start` 隨季度滾動，舊窗被埋的碼在新窗可能合格。只鍵 sha 會**永遠**剝奪重評機會——靜默扼殺好因子。要做對得鍵 `(sha, oos_start, interval, features_hash)`，複雜度遠超收益。

**誠實界線（寫進 docstring，不是留在腦子裡）：**
- **timeout 歸類含 infra 誤判**：宿主機卡住也會 timeout 並被歸咎給 LLM。損失由 `max_retries` + `should_early_stop` 界定，不會無限重試。
- **OOM 判定是啟發式**：`docker inspect .State.OOMKilled` 因 `--rm` 已不可得；用 `exit==137` **或** stderr 含 `MemoryError`/`Killed`。訊息措辭是「looks OOM-killed」，不斷言唯一成因。
- **agy 3a 已查核無誤**：自家 timeout 路徑先 catch `TimeoutExpired`，走不到 `returncode` 檢查，故自家 `docker kill` 的 137 不會被誤判為 OOM。

**這 6 個 bug 的共同模式（每 Task 都要對抗）：** 護欄存在、單測綠、**生產路徑沒呼叫**。Task 4 用真容器而非 mock；Task 3 的順序由「AST 違規碼」測試反向鎖定；Task 2 的 except 順序由「infra 仍向上拋」反向鎖定。

**Type consistency：** `SandboxRunFailed(SandboxError)` 子類關係在 Task 1 測試中斷言；`.oom` / `.exit_code` 屬性在 Task 1、2、4 三處一致；forge 的 except 順序（子類先）在 Task 2 測試中鎖定。

**Placeholder scan：** Task 1/4 明示「實作前 Read 現行 `run()`；OOM 標記先跑真容器觀察再擴充」。`_OOM_STDERR_MARKERS` 是可擴充常數，非 placeholder。

---

## Execution Handoff

計畫 v2 存 `docs/talos/plans/2026-07-10-talos-preflight-guards.md`，已納 agy 二審全部裁定（含刪除兩個 Task）。選執行：Subagent-Driven（推薦）或 Inline，每 Task 停等核准。**Task 4 需要 docker daemon + 建好的 sandbox image。**
