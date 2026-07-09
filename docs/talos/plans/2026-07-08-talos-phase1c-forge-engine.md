# Talos Phase 1C — LLM Forge Engine + Bounded Repair Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 建 Foundry 唯一的 LLM 創意點——把 `Hypothesis`（1B）交給 LLM 產出 feature-transform 程式碼，在 **Phase 0 護欄內**（AST 閘 + Docker 沙盒 + PIT 驗證）**有界修復迴圈**執行：產碼→跑→錯誤回饋重試 ≤N，成功回可用 series，失敗記死因。**這是全系統唯一讓 LLM「亂撞」的地方，故護欄最厚。**

**Architecture:** `research/hermes/forge.py`。`LLMCoder` 是可注入介面（`.complete(prompt)->str`，單次 completion）。**預設實作＝輕量 OpenRouter-相容 completion client**（agy：**不用 agent swarm CLI**——`run_swarm` 是 `crypto_factor_lab` 多 agent 協作、回 JSON array，與 forge 要的單一 ```python block 規格錯位）；沿用 [[vibe_trading_provider_constraint]] OpenRouter 中介 + agent `.env` 金鑰。測試注 fake，無網路可全測。迴圈：`generate → extract code → check_source(AST) → DockerSandbox.run → index 契約斷言 → PIT-at-boundary 驗證 → 成功`；任一步失敗 → 帶前次 code+error 回饋 + 重試 ≤`max_retries`，耗盡 → death。**PIT 在沙盒邊界驗證**：已算的 baseline series 直接複用，只對 future-corrupted panel 再跑一次沙盒比對（agy 4a：省去 assert 內部重算，每 attempt 僅 2 次容器啟動）。

**Tech Stack:** Python 3.11、既有 `sandbox`（`DockerSandbox`/`check_source`/`is_docker_available`）、`pit`（`assert_no_lookahead`）、pandas。pytest research scope。

> **⚠️ P4 前置（overview 揭）**：`DockerSandbox` 現用 `subprocess.run(timeout=)` 只殺 docker **CLI client** 不殺**容器**（moby 不從 CLI 傳 SIGKILL 到 daemon）。LLM 產碼可能 `while True` 或 OOM 笛卡爾積把容器掛住。**Task 1 先硬化容器生命週期 timeout**，否則 P4 防線漏、修復迴圈的「超時＝失敗」判定不可靠。

**設計來源：** overview（1C + P4/P5）+ `talos-design.md` §3.3 有界修復迴圈 + §6 Phase 1。

---

## 設計原則

1. **護欄最厚**：LLM 產碼**必**過 AST 閘（layer-0）→ Docker 沙盒（layer-1，斷網/OOM/唯讀）→ PIT 邊界驗證。三層任一擋下即該次失敗。
2. **有界**：`max_retries`（預設 3，agy P5：別在「差一點」因子無限追問）+ 每次帶前次錯誤回饋。耗盡直接 death，記死因。
3. **1C 只 forge，不評估/不記帳**：產出可用 series；`GatekeeperResult`（1A）+ `factor_trial` ledger 記帳 + 證據卡（1E）由 **1D orchestrator** 串。1C 回 `ForgeResult`。
4. **LLM 可注入**：`LLMCoder` 介面；預設 subprocess 到 agent swarm，測試注 fake。無網路可全測（docker 部分 skip-if-no-daemon，比照 Phase 0）。

---

## File Structure

| 檔案 | 責任 |
|------|------|
| `research/hermes/sandbox.py`（**改**） | Task 1：容器生命週期安全 timeout（`--name` + 背景 + 逾時 `docker kill`） |
| `research/hermes/forge.py` | `LLMCoder` 介面 + `extract_code` + `pit_check_via_sandbox` + `forge()` 迴圈 + `ForgeResult` |
| `research/tests/test_hermes_sandbox_timeout.py` | 容器 timeout 硬化單測 |
| `research/tests/test_hermes_forge.py` | 迴圈 / 抽碼 / 修復 / death 假 LLM 單測 |

---

## Task 1: P4 — 容器生命週期安全 timeout（沙盒硬化前置）

**Files:** Modify `research/hermes/sandbox.py`; Test `research/tests/test_hermes_sandbox_timeout.py`. Reference: 現有 `DockerSandbox.run`（`subprocess.run(timeout=)`）。

現況：`subprocess.run(cmd, timeout=…)` 逾時只 kill docker CLI，容器續跑（`--rm` 只在容器自退時觸發，對無窮迴圈無效）。改：**維持同步阻塞 `docker run`**（**嚴禁 `-d`**——agy 1d：加 `-d` 則 CLI 秒回、timeout 永不觸發、`capture_output` 抓不到 stdout，徹底破壞既有 output 讀回），只加 `--name talos_sbx_<uuid>`；捕 `subprocess.TimeoutExpired` 時 `docker kill` + `docker rm -f` 獵殺容器，再 raise `SandboxError`。

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_sandbox_timeout.py
import subprocess
import pytest
from research.hermes.sandbox import DockerSandbox, SandboxError


def test_timeout_kills_container_not_just_cli(monkeypatch):
    sb = DockerSandbox(memory="256m", timeout_s=1)
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
    sb = DockerSandbox(image=img, memory="256m", timeout_s=2)
    # an infinite loop the AST gate allows (no banned calls) -> must be reaped
    infinite = "import pandas as pd\ndef compute(df):\n    while True:\n        pass\n"
    with pytest.raises(SandboxError):
        sb.run(infinite, input_parquet=str(inp), output_dir=str(tmp_path))
    # no talos_sbx_* container may survive
    out = subprocess.run(["docker", "ps", "-q", "-f", "name=talos_sbx_"],
                         capture_output=True, text=True, timeout=15)
    assert out.stdout.strip() == "", f"leaked container(s): {out.stdout!r}"
```

- [ ] **Step 2: Run — fails (timeout does not reap container).**

- [ ] **Step 3: Implement** — 在 `DockerSandbox.run` 的 timeout 路徑加容器獵殺。核心：給容器固定 `--name talos_sbx_<uuid>`，`subprocess.TimeoutExpired` 時呼叫 `docker kill` + `docker rm -f`（best-effort、各自 try），再 raise `SandboxError`。實作前 `Read` 現況 `run()` 對齊既有 `_build_command`/mount/tmp-source 清理路徑，勿破壞既有測試（`test_docker_flags_are_hardened` 等）。

```python
# sketch of the timeout-hardening inside run():
#   name = f"talos_sbx_{uuid.uuid4().hex[:12]}"
#   cmd = self._build_command(..., name=name)   # add --name <name> to docker run
#   try:
#       proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s, text=True)
#   except subprocess.TimeoutExpired as exc:
#       for verb in (["docker", "kill", name], ["docker", "rm", "-f", name]):
#           try: subprocess.run(verb, capture_output=True, timeout=15)
#           except Exception: pass
#       raise SandboxError(f"sandbox timed out after {self.timeout_s}s; container {name} reaped") from exc
```

`_build_command` 加 `name` 參數，插 `--name <name>`（維持既有位置參數呼叫相容，`name` 給預設）。

- [ ] **Step 4: Run — passes; 全 Phase 0 sandbox 測試無回歸。**

- [ ] **Step 5: Commit** `fix(hermes): container-lifecycle-safe sandbox timeout (Phase 1C P4 prereq)`

---

## Task 2: `LLMCoder` 介面 + prompt 建構

**Files:** Create `research/hermes/forge.py`; Test `research/tests/test_hermes_forge.py`.

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_forge.py
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
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
# research/hermes/forge.py
"""Talos Foundry LLM forge engine + bounded repair loop (Phase 1C).

The ONLY place an LLM writes code. Every attempt runs the full Phase-0 gauntlet:
AST allowlist gate -> Docker sandbox (offline, memory-capped, container-timeout-
reaped) -> PIT-at-boundary verification. Bounded: <= max_retries with prior-error
feedback, then buried. 1C forges only; scoring (1A) + ledger + evidence card are
wired by 1D."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from research.hermes.hypothesis import Hypothesis

_PROMPT = """You are writing a single Python factor for a crypto perp research pipeline.

Hypothesis to implement: {desc}

Hard contract (violating any of these fails the attempt):
- Define exactly one function `compute(df)` that returns a pandas Series.
- The returned Series MUST keep df's DatetimeIndex unchanged (do not reset/strip/reindex it).
- Use ONLY: pandas, numpy, scipy, ta, math, statistics. No I/O, no network, no os/sys.
- Point-in-time: a value at row t may depend only on rows <= t. No .shift(-k), no bfill/backfill.
Return ONLY a fenced ```python code block.
{repair}"""


class LLMCoder(Protocol):
    def complete(self, prompt: str) -> str: ...


def build_prompt(hypothesis: Hypothesis, prior_code: Optional[str] = None,
                 prior_error: Optional[str] = None) -> str:
    # agy 5b: feed back the previous CODE as well as the error, else the LLM
    # cannot tell which lines failed and re-emits the same mistake.
    repair = "" if not prior_error else (
        f"\nYour previous code:\n```python\n{prior_code or ''}\n```\n"
        f"failed with:\n{prior_error}\nFix it.")
    return _PROMPT.format(desc=hypothesis.description, repair=repair)
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): forge LLMCoder protocol + prompt builder (Phase 1C)`

---

## Task 3: 抽碼 + AST 閘整合

**Files:** Modify `forge.py` + test. Reference: `research/hermes/sandbox_ast.check_source`, `UnsafeCodeError`.

`extract_code`：從 LLM 回應抓第一個 ```python fenced block（無 fence 退回整段）。`generate_code`：`llm.complete(prompt)` → `extract_code` → `check_source`（AST 閘）；閘失敗回饋錯誤字串。

- [ ] **Step 1: Failing test**

```python
def test_extract_code_pulls_fenced_block():
    from research.hermes.forge import extract_code
    resp = "sure:\n```python\ndef compute(df):\n    return df['close']\n```\ndone"
    assert extract_code(resp).startswith("def compute(df):")


def test_generate_code_rejects_unsafe_via_ast_gate():
    from research.hermes.forge import generate_code
    class BadLLM:
        def complete(self, prompt): return "```python\nimport os\ndef compute(df):\n    return df\n```"
    from research.hermes.sandbox_ast import UnsafeCodeError
    with pytest.raises(UnsafeCodeError):
        generate_code(BadLLM(), "prompt")            # AST gate blocks os import
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
import re

from research.hermes.sandbox_ast import check_source

_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


def extract_code(response: str) -> str:
    m = _FENCE.search(response)
    return (m.group(1) if m else response).strip()


def generate_code(llm: LLMCoder, prompt: str) -> str:
    """LLM -> fenced code -> AST allowlist gate. Raises UnsafeCodeError on gate fail."""
    code = extract_code(llm.complete(prompt))
    check_source(code)                               # layer-0; raises UnsafeCodeError
    return code
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): forge code extraction + AST-gate integration (Phase 1C)`

---

## Task 4: PIT 邊界驗證（沙盒兩跑）

**Files:** Modify `forge.py` + test. Reference: `research/hermes/pit.assert_no_lookahead`, `LookaheadError`; `research/hermes/sandbox.DockerSandbox`.

在沙盒邊界驗 PIT：**baseline series 已由 forge 跑過、直接傳入**（agy 4a 省重算）；此函式只對 future-corrupted panel 再跑一次沙盒，斷言汙染前輸出與 baseline 不變。純函式測試用 fake sandbox（不需 docker）。

- [ ] **Step 1: Failing test**

```python
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
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
import numpy as np

from research.hermes.pit import LookaheadError, PROBE_FROM_DEFAULT, PERTURB_GAP


def pit_check_via_sandbox(code: str, panel, baseline, run, atol=1e-9, rtol=1e-9) -> None:
    """Verify point-in-time at the sandbox boundary. `baseline` is the series
    forge ALREADY computed on the clean panel (agy 4a: don't recompute it).
    We corrupt the future of the panel, run the sandbox ONCE more, and assert the
    pre-corruption region matches baseline. `run(code, panel)->Series` is injected
    (real DockerSandbox in 1D; fake in tests). Raises LookaheadError on leak."""
    n = len(panel)
    perturb_from = min(PROBE_FROM_DEFAULT, n - PERTURB_GAP - 1)
    corrupt = panel.copy()
    corrupt.iloc[perturb_from:] = 1e10
    corrupt.iloc[perturb_from + PERTURB_GAP:] = np.nan
    after = np.asarray(run(code, corrupt), dtype="float64")[:perturb_from]
    base = np.asarray(baseline, dtype="float64")[:perturb_from]
    if not np.allclose(base, after, atol=atol, rtol=rtol, equal_nan=True):
        drift = np.nanmax(np.abs(base - after))
        raise LookaheadError(f"factor peeks into the future via sandbox: drift {drift:.3e}")
```

> 需 `pit.py` 匯出 `PROBE_FROM_DEFAULT`/`PERTURB_GAP`（Phase 0 已定義為模組常數）。實作前 `Read pit.py` 確認常數名；若未匯出則於此模組重定相同值並註記來源，勿臆測。

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): PIT boundary check via sandbox two-run (Phase 1C)`

---

## Task 5: 有界修復迴圈 `forge()` + `ForgeResult`

**Files:** Modify `forge.py` + test.

串起來：`forge(hypothesis, llm, run_sandbox, panel, max_retries=3)`。每次：`generate_code`（含前次 code+error）→ `run_sandbox`（拿 series）→ **index 契約斷言**（agy 遺漏：失 index 給清楚回饋非晦澀 broadcast error）→ `pit_check_via_sandbox`（傳 baseline series）。code 執行錯 → 帶前次 code+error 重試。**`SandboxError`（docker 沒開＝基礎設施錯）直接上拋，不當可修復**（agy 5a）。成功回 `ForgeResult(success, code, series, attempts)`；耗盡回帶 `code=last_code` 的 death（agy 5c，供 1D 記爛 code）。

- [ ] **Step 1: Failing test**

```python
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
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
from research.hermes.pit import LookaheadError
from research.hermes.sandbox import SandboxError
from research.hermes.sandbox_ast import UnsafeCodeError


@dataclass(frozen=True)
class ForgeResult:
    success: bool
    attempts: int
    code: Optional[str] = None
    series: object = None                 # pd.Series on success
    death_reason: Optional[str] = None


def forge(hypothesis, llm, run_sandbox, panel, max_retries: int = 3) -> ForgeResult:
    """Bounded repair loop. run_sandbox(code, panel)->Series executes the code
    (real DockerSandbox in 1D; fake in tests). Infra errors (SandboxError) are
    NOT repairable and propagate; only code errors are retried with feedback."""
    prior_code = prior_error = None
    last_error, last_code = "no attempt ran", None
    for attempt in range(1, max_retries + 1):
        code = None
        try:
            code = generate_code(llm, build_prompt(hypothesis, prior_code, prior_error))
            last_code = code
            series = run_sandbox(code, panel)
            # agy: clear contract feedback beats an obscure broadcast error
            if not hasattr(series, "index") or not series.index.equals(panel.index):
                raise ValueError("Contract violation: returned Series must keep df's DatetimeIndex unchanged")
            pit_check_via_sandbox(code, panel, series, run_sandbox)
            return ForgeResult(True, attempt, code=code, series=series)
        except SandboxError:
            raise                                       # agy 5a: infra error, don't burn retries
        except (UnsafeCodeError, LookaheadError, ValueError, KeyError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            prior_code, prior_error = code, last_error
    return ForgeResult(False, max_retries, code=last_code, death_reason=last_error)
```

在 `research/hermes/__init__.py` 追加匯出 `forge`, `ForgeResult`, `LLMCoder`（先 `Read` 現況再改）。

> 例外集合刻意**排除裸 `RuntimeError`**（`SandboxError` 繼承它，已先 `except SandboxError: raise`）。code 執行錯以 `ValueError`/`KeyError`/`TypeError` 為主；若 sandbox 用其他 exception 表達 code 錯，實作前 `Read sandbox.run` 對齊。

- [ ] **Step 4: Run forge suite + 全 hermes 套件無回歸。**

- [ ] **Step 5: Commit** `feat(hermes): bounded repair-loop forge()+ForgeResult, export (Phase 1C)`

---

## Self-Review

**Spec coverage：** P4 容器 timeout（overview 前置）→ Task 1；LLM 介面+prompt → Task 2；抽碼+AST 閘 → Task 3；PIT 邊界驗證 → Task 4；有界修復迴圈+死因（P5 max_retries）→ Task 5。三層護欄（AST→sandbox→PIT）全在迴圈內。

**已知界線 / 待 1D：**
- **1C 只 forge**：不評 IC（1A）、不記 `factor_trial` ledger、不寫證據卡（1E）。`ForgeResult` 交 1D orchestrator 串 1A→1E。
- **真 `run_sandbox` 由 1D 注入** = `DockerSandbox.run` 包一層讀回 output parquet 成 Series。1C 測試注 fake（無 docker），符合 Phase 0 skip-if-no-daemon 慣例。
- **backlog #2（runner 輸出 index 契約）已在 1C 內驗**（agy 遺漏修正）：`forge` 拿 series 後、進 PIT 前立即 `series.index.equals(panel.index)` 斷言，失敗給 LLM 清楚「保留 DatetimeIndex」修復指令而非晦澀 broadcast error。
- **token/compute 預算（P5）**：`max_retries` 是每因子上限；跨因子 nightly 總預算 + early stopping 是 **1D** 職責，非 1C。

**Placeholder scan：** Task 1 明示「Read 現況 run() 對齊 mount/tmp 清理，勿破壞既有測試」。`_PROMPT` 是實際 prompt 非 placeholder。

**Type consistency：** `LLMCoder.complete`/`build_prompt`/`extract_code`/`generate_code`/`pit_check_via_sandbox`/`forge`/`ForgeResult` 跨 task 一致；`forge` 消費的 `run_sandbox(code, panel)->Series` 簽名在 Task 4/5 一致；例外集合（`UnsafeCodeError`/`LookaheadError`/`RuntimeError`…）涵蓋 AST 閘 + 沙盒 + PIT 三層失敗。

**憲法對齊：** prompt 硬契約（唯 compute/保 index/白名單 import/PIT 禁負 shift+bfill）＝把 §5 憲法編進 LLM 指令，且**不信任** LLM 遵守——AST 閘 + 沙盒 + PIT 是機器強制。

---

## 附錄：agy 二審（10 findings 全採納）

agy 核對 `sandbox.py`/`pit.py`/`sandbox_ast.py`/`stage0_discovery.py` 現況。確認無誤：現況只殺 CLI 不殺容器（1a）、uuid 不撞名（1b）、corrupt input 真抓容器內洩漏（4b）、PIT 補 AST 抓不到的動態 shift/`iloc[::-1]`/`rolling(center=True)`（4c）。折入修正：

- **1d（致命自相矛盾）**：文字說 `-d 背景` 與 sketch 的 blocking `capture_output+timeout` 衝突（加 `-d` 則 timeout 永不觸發）。**禁 `-d`**，維持同步阻塞 + `--name` + TimeoutExpired 時 kill/rm。
- **1c**：fake_run 只測 mock。加 docker-gated 真整合測試（`while True` + `docker ps -f name=talos_sbx_` 斷言容器消失）。
- **4a（效能）**：原設計每 attempt 3 次容器啟動（×3=9/因子）。改：baseline series 複用，pit 只跑 corrupt → 2 次/attempt。
- **5a**：`except` 太寬把 `SandboxError`（docker 沒開＝基礎設施錯）當可修復重試 3 次。改 `except SandboxError: raise` 上拋。
- **5b（致命盲點）**：`prior_error` 無 `prior_code` → LLM 重產同錯。回饋改帶前次 code+error。
- **5c**：death 無 code → 1D 無法記爛 code。追蹤 `last_code` 回傳。
- **LLM（規格錯位）**：agent swarm CLI 是多 agent JSON array，非單次 completion。棄 swarm，`LLMCoder` 預設＝輕量 OpenRouter completion client。
- **遺漏（backlog #2）**：index 契約驗證移入 `forge`（失 index 給清楚訊息，非晦澀 broadcast error）。

---

## Execution Handoff

計畫存 `docs/talos/plans/2026-07-08-talos-phase1c-forge-engine.md`，已納 agy 二審全部修正。選執行：Subagent-Driven（推薦）或 Inline，每 Task 停等核准。
