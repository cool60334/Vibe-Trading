# Talos Foundry First-Real-Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `run_foundry()` runnable for the first time via a standalone CLI, verified end-to-end at zero LLM cost with a scripted fake LLM against a real Docker container.

**Architecture:** Relax the sandbox image pin to accept a content-addressed image ID and classify a missing image as infra (abort) not repairable (60 wasted retries); add a `foundry_runner.py` CLI (`enqueue` writes a job.json, `run` reconciles the queue — design law 2, Talos never inline-runs); prove the whole production path (entry → reconcile → run_foundry → forge → sandbox → gatekeeper → evidence card) with a docker-gated e2e whose scripted LLM emits dirty markdown so the real prompt/parse path is walked.

**Tech Stack:** Python 3.11, pandas, pyarrow, Docker CLI, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-10-foundry-first-run-design.md`

## Global Constraints

- **Three pytest scopes never mix.** Research tests run from repo root: `python -m pytest research/tests/`. Do NOT combine with `agent/tests/` or `dashboard/server` pytest.
- **Test doubles never enter production modules.** `ScriptedLLM` lives in `research/tests/hermes_support.py`, never in `research/hermes/`.
- **The runner never touches the network.** `load_ohlcv` reads a parquet only. Fetching candles is a separate step, out of scope here.
- **Do not assert "a factor passes the gate" in the e2e.** Hand-picking a factor that clears `gross_ic_min`/`dsr_min` manufactures alpha. Assert engineering facts only (metrics finite, outcome ∈ {candidate, rejected}, evidence card written).
- **Every guard gets a test that the production path actually calls it.** Real container over mock wherever a resource/lifecycle guard is involved.
- **Commit style:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Never push without being asked.
- **Docker-gated tests** skip unless `is_docker_available()` and `TALOS_SANDBOX_TEST_IMAGE` is set. The built test image is `talos-sandbox:test`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `research/hermes/sandbox.py` (modify) | pin accepts bare image ID; image-existence pre-check with cache; exit-125/daemon-down promotion | 1, 2, 3 |
| `research/tests/hermes_support.py` (modify) | `ScriptedLLM` test double emitting dirty markdown | 4 |
| `research/tests/test_hermes_orchestrator.py` (modify) | characterization tests for the untested candidate/graveyard merge | 5 |
| `research/hermes/foundry_runner.py` (create) | `resolve_image_id`, `load_ohlcv`, `reconcile_foundry_jobs`, `build_llm`, `main` | 6, 7, 8 |
| `research/tests/test_hermes_foundry_runner.py` (create) | unit tests for the runner (mock, no docker) | 6, 7, 8 |
| `research/tests/fixtures/ohlcv_eth_1h.parquet` (create) | one-time OKX fetch, committed | 9 |
| `research/tests/test_hermes_foundry_e2e.py` (create) | docker-gated full-chain e2e via the real reconcile path | 10 |

---

## Task 1: Pin accepts a bare content-addressed image ID

**Files:**
- Modify: `research/hermes/sandbox.py:108-125` (`_DIGEST_RE`, `_assert_image_pinned`)
- Test: `research/tests/test_hermes_sandbox.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_assert_image_pinned(image, allow_unpinned)` now also accepts a bare `sha256:<64hex>` without raising.

**Why:** A locally-built image has empty `RepoDigests` (verified), so the only reference that satisfies the current `@sha256:` regex is a stitched `name@sha256:<image-id>` that docker then rejects (`No such image`). A bare image ID is content-addressed and immutable — equivalent locking for single-host use.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_sandbox.py (append)
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
```

- [ ] **Step 2: Run — verify the new bare-id test fails**

Run: `python -m pytest research/tests/test_hermes_sandbox.py::test_bare_image_id_is_accepted_as_pinned -v`
Expected: FAIL (the current regex requires a leading `@`, so a bare `sha256:...` raises SandboxError).

- [ ] **Step 3: Implement**

```python
# research/hermes/sandbox.py — replace _DIGEST_RE and the guard body
# A registry digest (name@sha256:...) OR a bare local image ID (sha256:...).
# The regex validates SHAPE only; the daemon is the arbiter of resolvability
# (see _assert_image_exists). A manifest digest and a config/image-id digest are
# both 64 hex chars and cannot be told apart by a regex in principle.
_DIGEST_RE = re.compile(r"(?:^|@)sha256:[0-9a-f]{64}$")
```

The `_assert_image_pinned` body is unchanged (`if allow_unpinned or _DIGEST_RE.search(image): return`). Update its docstring's `use 'name@sha256:...'` hint to `use 'name@sha256:<64-hex>' or a bare 'sha256:<64-hex>' image id`.

- [ ] **Step 4: Run — verify pass, no regression**

Run: `python -m pytest research/tests/test_hermes_sandbox.py -v`
Expected: PASS, including the pre-existing `test_run_refuses_unpinned_image` and `test_digest_pinned_image_passes_validation`.

- [ ] **Step 5: Commit**

```bash
git add research/hermes/sandbox.py research/tests/test_hermes_sandbox.py
git commit -m "fix(hermes): accept a bare content-addressed image id as a valid pin

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Image-existence pre-check, verified once per sandbox

**Files:**
- Modify: `research/hermes/sandbox.py` (add `_assert_image_exists`; call it in `run()`; cache on the instance)
- Test: `research/tests/test_hermes_sandbox.py`

**Interfaces:**
- Consumes: `_assert_image_pinned` (Task 1).
- Produces: `DockerSandbox._ensure_image()` verifies the image exists exactly once per instance, raising a bare `SandboxError` if `docker image inspect` fails.

**Why:** If the image is unresolvable, `docker run` exits non-zero and the current classifier calls it `SandboxRunFailed` (repairable) → every hypothesis burns 3 retries and dies with a "your code failed" reason, when the truth is a config typo. Catch it before the first container starts. Verify **once** — the image is immutable per instance, so asking the daemon per hypothesis is waste.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_sandbox.py (append)
def test_missing_image_is_infra_not_repairable(tmp_path, monkeypatch):
    import subprocess
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    def fake_run(cmd, **k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such image")
        raise AssertionError(f"should not reach: {cmd}")
    monkeypatch.setattr(subprocess, "run", fake_run)
    sb = DockerSandbox(image="sha256:" + "e" * 64, allow_unpinned=False, timeout_s=5)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    with pytest.raises(SandboxError) as ei:
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert not isinstance(ei.value, SandboxRunFailed)     # infra: must abort the sweep

def test_image_verified_once_not_per_hypothesis(tmp_path, monkeypatch):
    import subprocess
    from research.hermes.sandbox import DockerSandbox
    from research.tests.hermes_support import FakePopen
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    calls = {"inspect": 0}
    def fake_run(cmd, **k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            calls["inspect"] += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:e...\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")   # reap etc.
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen(returncode=0))
    sb = DockerSandbox(image="sha256:" + "e" * 64, allow_unpinned=False, timeout_s=5)
    safe = "import pandas as pd\ndef compute(df):\n    return df['close']\n"
    for _ in range(3):
        sb.run(safe, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert calls["inspect"] == 1          # cached after the first run
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_sandbox.py::test_missing_image_is_infra_not_repairable -v`
Expected: FAIL (`_assert_image_exists` does not exist; `run()` never inspects).

- [ ] **Step 3: Implement**

```python
# research/hermes/sandbox.py — module-level helper
def _assert_image_exists(image: str) -> None:
    """Ask the daemon whether the image resolves. A missing image is infra, not
    an LLM code failure: without this, `docker run` exits non-zero and the
    classifier would call it repairable, burning every hypothesis's retries on a
    config typo. Raises a bare SandboxError (aborts the sweep)."""
    try:
        proc = subprocess.run(["docker", "image", "inspect", image],
                              capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SandboxError(f"could not inspect sandbox image {image!r}: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError(
            f"sandbox image {image!r} does not resolve on this host "
            f"(docker image inspect exit {proc.returncode}): {(proc.stderr or '').strip()[:200]}")
```

In `DockerSandbox.__init__`, add `self._image_verified = False`. Add a method:

```python
    def _ensure_image(self) -> None:
        if not self._image_verified:
            _assert_image_exists(self.image)
            self._image_verified = True
```

In `run()`, right after the `is_docker_available()` check (sandbox.py:266), add `self._ensure_image()`.

- [ ] **Step 4: Run — verify pass, no regression**

Run: `python -m pytest research/tests/test_hermes_sandbox.py -v`
Expected: PASS. (`test_daemon_down_stays_infra` still passes — it returns `is_docker_available()==False`, which raises before `_ensure_image`.)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/sandbox.py research/tests/test_hermes_sandbox.py
git commit -m "feat(hermes): verify the sandbox image resolves before running any hypothesis

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Promote exit-125 / daemon-down to infra mid-run

**Files:**
- Modify: `research/hermes/sandbox.py:289-296` (`run()`'s non-zero-exit branch)
- Test: `research/tests/test_hermes_sandbox.py`

**Interfaces:**
- Consumes: `_run_container` (existing), `SandboxError`/`SandboxRunFailed`.
- Produces: `run()` raises a bare `SandboxError` for exit 125 or a daemon-down stderr; other non-zero exits stay `SandboxRunFailed`.

**Why:** The pre-check (Task 2) has a TOCTOU window — the image can be deleted, or the daemon can die, after inspection. `docker run` exit **125** is docker's own "couldn't start" code (verified: a missing image gives exit 125 `Unable to find image ... locally`); LLM code cannot produce 125, so this rule has no risk of misjudging a real code failure.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_sandbox.py (append)
def test_exit_125_is_promoted_to_infra(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    from research.tests.hermes_support import FakePopen
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr("research.hermes.sandbox.DockerSandbox._ensure_image", lambda self: None)
    _patch_popen(monkeypatch, FakePopen(returncode=125, stderr="Unable to find image 'x' locally"))
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    with pytest.raises(SandboxError) as ei:
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert not isinstance(ei.value, SandboxRunFailed)     # infra, abort the sweep

def test_daemon_down_midrun_is_infra(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed
    from research.tests.hermes_support import FakePopen
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr("research.hermes.sandbox.DockerSandbox._ensure_image", lambda self: None)
    _patch_popen(monkeypatch, FakePopen(returncode=1, stderr="Cannot connect to the Docker daemon"))
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    with pytest.raises(SandboxError) as ei:
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
    assert not isinstance(ei.value, SandboxRunFailed)

def test_ordinary_nonzero_exit_stays_repairable(tmp_path, monkeypatch):
    from research.hermes.sandbox import DockerSandbox, SandboxRunFailed
    from research.tests.hermes_support import FakePopen
    monkeypatch.setattr("research.hermes.sandbox.is_docker_available", lambda: True)
    monkeypatch.setattr("research.hermes.sandbox.DockerSandbox._ensure_image", lambda self: None)
    _patch_popen(monkeypatch, FakePopen(returncode=1, stderr="KeyError: 'nope'"))
    sb = DockerSandbox(allow_unpinned=True, timeout_s=5)
    with pytest.raises(SandboxRunFailed):     # container ran; the code failed
        sb.run(SAFE_SOURCE, input_parquet="x.parquet", output_dir=str(tmp_path))
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_sandbox.py::test_exit_125_is_promoted_to_infra -v`
Expected: FAIL (currently a 125 exit becomes `SandboxRunFailed`, a subclass of `SandboxError`, so `not isinstance(...)` fails).

- [ ] **Step 3: Implement**

```python
# research/hermes/sandbox.py — module-level
_DOCKER_STARTUP_EXIT = 125     # docker's own "couldn't start the container" code
_DAEMON_DOWN_MARKERS = ("Cannot connect to the Docker daemon", "Unable to find image")

def _looks_like_infra(exit_code: int, stderr: str) -> bool:
    """True when docker itself failed to start the container, not the code inside
    it. Exit 125 is docker's dedicated startup-failure code; LLM code cannot emit
    it. The stderr markers catch a daemon that died or an image deleted after the
    pre-check (TOCTOU)."""
    if exit_code == _DOCKER_STARTUP_EXIT:
        return True
    return any(m in stderr for m in _DAEMON_DOWN_MARKERS)
```

In `run()`, replace the non-zero branch (sandbox.py:289-296) with:

```python
            if returncode != 0:
                stderr = stderr or ""
                if _looks_like_infra(returncode, stderr):
                    raise SandboxError(
                        f"docker could not start the sandbox container "
                        f"(exit {returncode}): {stderr.strip()[:300]}")
                oom = _looks_like_oom(returncode, stderr)
                hint = (f"; looks OOM-killed — the code exceeded --memory={self.memory}"
                        if oom else "")
                raise SandboxRunFailed(
                    f"sandbox run failed (exit {returncode}){hint}\n{stderr[-800:]}",
                    exit_code=returncode, oom=oom,
                )
```

(Note: `_run_container` returns `(returncode, _stdout, stderr)`; the local `stderr` is already bound at the call site — keep the existing unpacking `returncode, _stdout, stderr = self._run_container(cmd, name)`.)

- [ ] **Step 4: Run — verify pass, no regression**

Run: `python -m pytest research/tests/test_hermes_sandbox.py research/tests/test_hermes_sandbox_timeout.py research/tests/test_hermes_sandbox_limits.py -v`
Expected: PASS. The timeout path is untouched (it raises before the returncode branch).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/sandbox.py research/tests/test_hermes_sandbox.py
git commit -m "fix(hermes): classify exit-125 / daemon-down as infra, not repairable code failure

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `ScriptedLLM` test double emitting dirty markdown

**Files:**
- Modify: `research/tests/hermes_support.py`
- Test: `research/tests/test_hermes_foundry_runner.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `ScriptedLLM(bodies: list[str])` with `.complete(prompt) -> str` returning call `i`'s body wrapped in a markdown fence with a preamble; records every prompt in `.prompts`.

**Why:** A fake that emits clean code bypasses `build_prompt()` and `extract_code()` — exactly where a real LLM most often breaks (agy's strongest point). The fake must return dirty strings so the real parse path is walked, and record prompts so the e2e can assert the hypothesis description reached the LLM.

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_foundry_runner.py (create)
def test_scripted_llm_wraps_bodies_in_dirty_markdown_and_records_prompts():
    from research.tests.hermes_support import ScriptedLLM
    from research.hermes.forge import extract_code
    llm = ScriptedLLM(["def compute(df):\n    return df['close']"])
    out = llm.complete("PROMPT-A")
    assert "```" in out and "Certainly" in out            # dirty: fence + preamble
    assert extract_code(out) == "def compute(df):\n    return df['close']"
    assert llm.prompts == ["PROMPT-A"]
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py::test_scripted_llm_wraps_bodies_in_dirty_markdown_and_records_prompts -v`
Expected: FAIL (`ScriptedLLM` does not exist).

- [ ] **Step 3: Implement**

```python
# research/tests/hermes_support.py (append)
class ScriptedLLM:
    """A fake LLMCoder that returns pre-written bodies in call order, wrapped in
    a markdown fence with a chatty preamble so forge's build_prompt/extract_code
    parse path is actually exercised (a clean string would bypass it). Records
    every prompt it is handed so a test can assert the hypothesis description
    reached the LLM."""

    def __init__(self, bodies: list):
        self._bodies = list(bodies)
        self._i = 0
        self.prompts: list = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        body = self._bodies[min(self._i, len(self._bodies) - 1)]
        self._i += 1
        return f"Certainly! Here is the factor you asked for:\n\n```python\n{body}\n```\nHope this helps."
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research/tests/hermes_support.py research/tests/test_hermes_foundry_runner.py
git commit -m "test(hermes): ScriptedLLM fake emits dirty markdown to exercise the parse path

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Characterization tests for the untested candidate/graveyard merge

**Files:**
- Test: `research/tests/test_hermes_orchestrator.py` (append)

**Interfaces:**
- Consumes: `_merge_into_candidates(symbol, manifests_dir, factor_id, series)`, `_merge_into_graveyard(...)`, `_merge_column(existing, name, series)` (all existing in `orchestrator.py:84-112`).
- Produces: nothing (test-only).

**Why:** agy verified the candidate write path was 0-coverage: `test_hermes_candidate_store.py` only exercises the low-level `write_candidate`, never the orchestrator's outer-join merge. A wrong merge (duplicate index, clobbered column) would pass every existing test and corrupt the store on a real run. These are gate-independent — no factor needs to pass, so no manufactured alpha.

> **Note:** the merge code already exists, so these tests pass on first run. That is the intended TDD exception for adding coverage to existing untested code — write the test, run it, confirm it green. The value is locking current behavior against future regressions.

- [ ] **Step 1: Write the tests**

```python
# research/tests/test_hermes_orchestrator.py (append)
def test_merge_into_candidates_outer_joins_across_calls(tmp_path):
    import pandas as pd, numpy as np
    from research.hermes.orchestrator import _merge_into_candidates
    from research.hermes.candidate_store import _candidate_path
    idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    _merge_into_candidates("eth", tmp_path, "f_a", pd.Series(np.arange(5.0), index=idx))
    _merge_into_candidates("eth", tmp_path, "f_b", pd.Series(np.arange(5.0) * 2, index=idx))
    got = pd.read_parquet(_candidate_path("eth", tmp_path))
    assert list(got.columns) == ["f_a", "f_b"]              # both kept, not clobbered
    assert got["f_b"].tolist() == [0.0, 2, 4, 6, 8]

def test_merge_column_replaces_same_name_and_unions_index():
    import pandas as pd, numpy as np
    from research.hermes.orchestrator import _merge_column
    idx1 = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    idx2 = pd.date_range("2024-01-01 02:00", periods=3, freq="1h", tz="UTC")
    base = _merge_column(None, "f_a", pd.Series([1.0, 2, 3], index=idx1))
    out = _merge_column(base, "f_a", pd.Series([9.0, 9, 9], index=idx2))   # same name
    assert list(out.columns) == ["f_a"]                     # replaced, not duplicated
    assert len(out) == 4                                    # union of indexes (2 overlap)

def test_merge_into_graveyard_persists_dead_values(tmp_path):
    import pandas as pd, numpy as np
    from research.hermes.orchestrator import _merge_into_graveyard, _graveyard_path
    idx = pd.date_range("2024-01-01", periods=4, freq="1h", tz="UTC")
    _merge_into_graveyard("eth", tmp_path, "dead_1", pd.Series(np.arange(4.0), index=idx))
    got = pd.read_parquet(_graveyard_path("eth", tmp_path))
    assert "dead_1" in got.columns and len(got) == 4
```

- [ ] **Step 2: Run — confirm green (existing code)**

Run: `python -m pytest research/tests/test_hermes_orchestrator.py -k "merge" -v`
Expected: PASS (characterization of existing behavior). If any FAIL, that is a real pre-existing merge bug — stop and report it before continuing.

- [ ] **Step 3: Commit**

```bash
git add research/tests/test_hermes_orchestrator.py
git commit -m "test(hermes): cover the candidate/graveyard outer-join merge (was 0-coverage)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `resolve_image_id` and `load_ohlcv`

**Files:**
- Create: `research/hermes/foundry_runner.py`
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `SandboxError` from `research.hermes.sandbox`.
- Produces:
  - `resolve_image_id(tag: str) -> str` — returns `"sha256:<64hex>"` or raises `SandboxError`.
  - `load_ohlcv(path) -> pd.DataFrame` — reads a parquet, validates a `close` column and a tz-aware UTC `DatetimeIndex`, or raises `ValueError`. Never networks.

**Why:** The runner resolves a human tag to an immutable image ID (a tag can be swapped under you; an ID cannot) and loads price data from a file only. Both are pure, docker-CLI-only or IO-only, unit-testable without a container.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_runner.py (append)
def test_resolve_image_id_returns_immutable_id(monkeypatch):
    import subprocess
    from research.hermes.foundry_runner import resolve_image_id
    def fake_run(cmd, **k):
        assert cmd[:4] == ["docker", "image", "inspect", "talos-sandbox:test"]
        return subprocess.CompletedProcess(cmd, 0, stdout="sha256:" + "e" * 64 + "\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_image_id("talos-sandbox:test") == "sha256:" + "e" * 64

def test_resolve_image_id_raises_on_unresolvable(monkeypatch):
    import subprocess
    from research.hermes.foundry_runner import resolve_image_id
    from research.hermes.sandbox import SandboxError
    monkeypatch.setattr(subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such image"))
    with pytest.raises(SandboxError, match="does not resolve|No such image"):
        resolve_image_id("nope:zzz")

def test_load_ohlcv_reads_close_and_utc_index(tmp_path):
    import pandas as pd
    from research.hermes.foundry_runner import load_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    p = tmp_path / "o.parquet"
    pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(p)
    got = load_ohlcv(p)
    assert "close" in got.columns and got.index.tz is not None

def test_load_ohlcv_rejects_missing_close(tmp_path):
    import pandas as pd
    from research.hermes.foundry_runner import load_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    p = tmp_path / "o.parquet"
    pd.DataFrame({"volume": [1.0, 2, 3]}, index=idx).to_parquet(p)
    with pytest.raises(ValueError, match="close"):
        load_ohlcv(p)
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "resolve or load_ohlcv" -v`
Expected: FAIL (`research.hermes.foundry_runner` does not exist).

- [ ] **Step 3: Implement**

```python
# research/hermes/foundry_runner.py (create)
"""Talos Foundry runner CLI (design law 2: Talos writes decision files, a
separate runner picks them up; it never inline-runs a stage itself).

`enqueue` writes a job.json; `run` reconciles the queued jobs. The runner never
touches the network — it reads an OHLCV parquet, it does not fetch candles."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

import pandas as pd

from research.hermes.sandbox import DockerSandbox, SandboxError, SandboxRunFailed

log = logging.getLogger(__name__)


def resolve_image_id(tag: str) -> str:
    """Resolve a human-readable tag to its immutable content-addressed image id.
    A tag can be re-pointed under you between the run that vets a factor and the
    run that reproduces it; the image id cannot. Raises SandboxError (infra) if
    the tag does not resolve on this host, so a config typo aborts before the
    first container starts rather than burning every hypothesis's retries."""
    try:
        proc = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                              capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SandboxError(f"could not inspect image {tag!r}: {exc}") from exc
    if proc.returncode != 0:
        raise SandboxError(
            f"sandbox image {tag!r} does not resolve on this host: "
            f"{(proc.stderr or '').strip()[:200]}")
    return proc.stdout.strip()


def load_ohlcv(path) -> pd.DataFrame:
    """Read an OHLCV parquet. Never networks. Validates a `close` column and a
    tz-aware index so a misaligned/naive table fails loudly here rather than
    producing NaN forward returns deep inside the gate."""
    df = pd.read_parquet(path)
    if "close" not in df.columns:
        raise ValueError(f"ohlcv parquet {path} has no 'close' column")
    if getattr(df.index, "tz", None) is None:
        raise ValueError(f"ohlcv parquet {path} index is not tz-aware (need UTC)")
    return df
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "resolve or load_ohlcv" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): foundry runner resolve_image_id + load_ohlcv (no network)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `reconcile_foundry_jobs` with the Layer-A/Layer-C discriminator

**Files:**
- Modify: `research/hermes/foundry_runner.py`
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `enqueue_foundry_job(symbol, runs_dir, params)`, `run_foundry_job(job_path, manifests_dir, llm, sandbox, zoo_dir, ohlcv, budget)` (both in `orchestrator.py`); `load_ohlcv` (Task 6); `SandboxError`/`SandboxRunFailed`.
- Produces: `reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir, budget=None) -> list[dict]` — runs every `status=="queued"` job in `created_at` order, loading each job's ohlcv from `params["ohlcv_path"]`. A bare `SandboxError` (not `SandboxRunFailed`) aborts the batch; any other exception leaves that job `status=failed` (already written by `run_foundry_job`) and continues.

**Why:** One entry point that walks the real production reconcile path. The discriminator makes a config/infra fault stop the whole batch (the next job hits the same wall) while a single bad-code job does not poison its neighbors.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_runner.py (append)
def _queue_job(tmp_path, symbol, ohlcv_path, created_at):
    from research.hermes.orchestrator import enqueue_foundry_job
    p = enqueue_foundry_job(symbol, tmp_path, {
        "oos_start": "2025-01-01", "interval": "1H", "horizon_h": 24,
        "ohlcv_path": str(ohlcv_path)})
    import json
    job = json.loads(Path(p).read_text()); job["created_at"] = created_at
    Path(p).write_text(json.dumps(job)); return p

def test_jobs_run_in_created_at_order(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-02T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-01T00:00:00+00:00")
    seen = []
    def fake_run_job(job_path, **k):
        import json; seen.append(json.loads(Path(job_path).read_text())["symbol"])
        return {"candidate": 0}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert seen == ["btc", "eth"]                      # earlier created_at first

def test_infra_error_aborts_whole_queue(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    from research.hermes.sandbox import SandboxError
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        if sym == "eth":
            raise SandboxError("docker image does not resolve")     # infra
        return {"candidate": 0}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    with pytest.raises(SandboxError):
        fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["eth"]                              # btc never ran

def test_job_level_failure_continues_queue(tmp_path, monkeypatch):
    import pandas as pd
    from research.hermes import foundry_runner as fr
    idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
    op = tmp_path / "o.parquet"; pd.DataFrame({"close": [1.0, 2, 3]}, index=idx).to_parquet(op)
    _queue_job(tmp_path, "eth", op, "2026-01-01T00:00:00+00:00")
    _queue_job(tmp_path, "btc", op, "2026-01-02T00:00:00+00:00")
    ran = []
    def fake_run_job(job_path, **k):
        import json; sym = json.loads(Path(job_path).read_text())["symbol"]; ran.append(sym)
        if sym == "eth":
            raise ValueError("bad factor")             # NOT infra
        return {"candidate": 1}
    monkeypatch.setattr(fr, "run_foundry_job", fake_run_job)
    fr.reconcile_foundry_jobs(tmp_path, tmp_path, llm=object(), sandbox=object(), zoo_dir=tmp_path)
    assert ran == ["eth", "btc"]                       # btc still ran despite eth failing
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "order or aborts or continues" -v`
Expected: FAIL (`reconcile_foundry_jobs` does not exist).

- [ ] **Step 3: Implement**

```python
# research/hermes/foundry_runner.py (append)
from research.hermes.orchestrator import run_foundry_job     # top-of-file import


def _queued_jobs(runs_dir) -> list:
    jobs_root = Path(runs_dir) / "foundry_jobs"
    out = []
    if not jobs_root.exists():
        return out
    for job_path in jobs_root.rglob("job.json"):
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue                                   # a half-written peer; next pass gets it
        if job.get("status") == "queued":
            out.append((job.get("created_at", ""), job_path, job))
    out.sort(key=lambda t: t[0])                       # deterministic: created_at order
    return out


def reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir, budget=None) -> list:
    """Run every queued foundry job in created_at order (write-file->reconcile).

    A bare SandboxError (docker/image infra, NOT a SandboxRunFailed subclass)
    aborts the whole batch — the next job would hit the same wall. Any other
    failure leaves that job status=failed (already written by run_foundry_job)
    and the batch continues, so one bad-code job does not poison its neighbours."""
    summaries = []
    for _created, job_path, job in _queued_jobs(runs_dir):
        ohlcv = load_ohlcv(job["params"]["ohlcv_path"])
        try:
            summaries.append(run_foundry_job(
                job_path, manifests_dir=manifests_dir, llm=llm, sandbox=sandbox,
                zoo_dir=zoo_dir, ohlcv=ohlcv, budget=budget))
        except SandboxError as exc:
            if not isinstance(exc, SandboxRunFailed):
                log.error("infra fault on %s; aborting the batch: %s", job_path, exc)
                raise                                  # Layer A: stop the whole queue
            log.warning("job %s failed (repairable, buried): %s", job_path, exc)
        except Exception as exc:                       # noqa: BLE001 job-level, keep going
            log.warning("job %s failed: %s; continuing", job_path, exc)
    return summaries
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -v`
Expected: PASS (all runner tests).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): reconcile_foundry_jobs with infra-aborts-batch vs job-continues

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `build_llm` stub + argparse `main()`

**Files:**
- Modify: `research/hermes/foundry_runner.py`
- Test: `research/tests/test_hermes_foundry_runner.py`

**Interfaces:**
- Consumes: `enqueue_foundry_job`, `resolve_image_id`, `DockerSandbox`, `reconcile_foundry_jobs`.
- Produces:
  - `build_llm(spec: str)` — raises `NotImplementedError` for `"openrouter"` (the real client is the next spec).
  - `main(argv=None)` — argparse with `enqueue` and `run` subcommands.

**Why:** A thin CLI seam. `build_llm` is a named boundary (documented gap), not a silent stub — it fails loudly rather than pretending to have an LLM.

- [ ] **Step 1: Write the failing tests**

```python
# research/tests/test_hermes_foundry_runner.py (append)
def test_build_llm_openrouter_is_not_yet_implemented():
    from research.hermes.foundry_runner import build_llm
    with pytest.raises(NotImplementedError, match="openrouter|next spec"):
        build_llm("openrouter")

def test_main_enqueue_writes_a_queued_job(tmp_path):
    import json
    from research.hermes.foundry_runner import main
    rc = main(["enqueue", "--symbol", "eth", "--runs-dir", str(tmp_path),
               "--oos-start", "2025-01-01", "--ohlcv-path", str(tmp_path / "o.parquet")])
    assert rc == 0
    jobs = list((tmp_path / "foundry_jobs").rglob("job.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text())
    assert job["status"] == "queued" and job["symbol"] == "eth"
    assert job["params"]["oos_start"] == "2025-01-01"
```

- [ ] **Step 2: Run — verify fail**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -k "build_llm or main_enqueue" -v`
Expected: FAIL (`build_llm` / `main` do not exist).

- [ ] **Step 3: Implement**

```python
# research/hermes/foundry_runner.py (append)
from research.hermes.orchestrator import enqueue_foundry_job    # top-of-file import


def build_llm(spec: str):
    """Construct the LLM client named by `spec`. The real OpenRouter client is
    the next spec; this seam exists so `run` has one place to wire it. Fails
    loudly rather than returning a silent stub."""
    if spec == "openrouter":
        raise NotImplementedError(
            "the real openrouter LLM client is the next spec; "
            "the dry-run uses ScriptedLLM injected directly by the test")
    raise ValueError(f"unknown llm spec {spec!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry", description="Talos Factor Foundry runner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("enqueue", help="write a queued foundry job.json")
    q.add_argument("--symbol", required=True)
    q.add_argument("--runs-dir", required=True)
    q.add_argument("--oos-start", required=True)
    q.add_argument("--ohlcv-path", required=True)
    q.add_argument("--interval", default="1H")
    q.add_argument("--horizon-h", type=int, default=24)

    r = sub.add_parser("run", help="reconcile queued foundry jobs")
    r.add_argument("--runs-dir", required=True)
    r.add_argument("--manifests-dir", required=True)
    r.add_argument("--zoo-dir", required=True)
    r.add_argument("--image", required=True, help="sandbox image tag; resolved to an immutable id")
    r.add_argument("--llm", default="openrouter")
    r.add_argument("--timeout-s", type=int, default=120)

    args = ap.parse_args(argv)
    if args.cmd == "enqueue":
        enqueue_foundry_job(args.symbol, args.runs_dir, {
            "oos_start": args.oos_start, "interval": args.interval,
            "horizon_h": args.horizon_h, "ohlcv_path": args.ohlcv_path})
        return 0

    image_id = resolve_image_id(args.image)          # infra pre-check; raises to abort
    sandbox = DockerSandbox(image=image_id, timeout_s=args.timeout_s, allow_unpinned=False)
    llm = build_llm(args.llm)                         # NotImplementedError until the next spec
    reconcile_foundry_jobs(args.runs_dir, args.manifests_dir, llm, sandbox, args.zoo_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run — verify pass, full runner suite**

Run: `python -m pytest research/tests/test_hermes_foundry_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_runner.py research/tests/test_hermes_foundry_runner.py
git commit -m "feat(hermes): foundry CLI enqueue/run with build_llm seam (openrouter deferred)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: OHLCV e2e fixture (one-time OKX fetch, committed)

**Files:**
- Create: `research/tests/fixtures/ohlcv_eth_1h.parquet`
- Create: `research/tests/fixtures/_fetch_ohlcv_fixture.py` (the one-time generator, kept for reproducibility)

**Interfaces:**
- Consumes: `research.lib.okx_data.fetch_candles(symbol, days, bar)`.
- Produces: a committed parquet, `open/high/low/close/volume`, UTC index, covering a pre-OOS window that overlaps the real `features_eth.parquet` index.

**Why:** No cached OHLCV exists anywhere in the repo, and `run_foundry` needs a price table it will not fetch itself. `features_eth.parquet` runs 2022-06-26 → 2026-06-25; `oos_start` is 2025-01-01. The fixture must cover a pre-OOS span (≥ 6 months, ~4300+ 1H bars) that overlaps the feature index so `_align_ohlcv`'s 95%-coverage check passes.

> **Network note:** this is the one approved one-time network access (OKX public market data, read-only, no auth). It runs once to produce a committed artifact; the test never networks.

- [ ] **Step 1: Write the generator**

```python
# research/tests/fixtures/_fetch_ohlcv_fixture.py (create)
"""One-time generator for ohlcv_eth_1h.parquet. Run manually once; the parquet is
committed and the e2e reads it offline. Kept for reproducibility."""
from pathlib import Path
from research.lib.okx_data import fetch_candles

def main() -> None:
    # ~2 years of ETH 1H, trimmed to a pre-OOS window overlapping features_eth.
    df = fetch_candles("ETH-USDT-SWAP", days=730, bar="1H")
    df = df.loc["2024-06-01":"2024-12-31"]          # pre-oos (oos_start=2025-01-01)
    out = Path(__file__).with_name("ohlcv_eth_1h.parquet")
    df.to_parquet(out)
    print(f"wrote {out} rows={len(df)} {df.index.min()}..{df.index.max()}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the generator once (approved network access)**

Run: `python -m research.tests.fixtures._fetch_ohlcv_fixture`
Expected: prints `wrote .../ohlcv_eth_1h.parquet rows=~5100 2024-06-01..2024-12-31`. Confirm the file exists and the row count is in the thousands.

- [ ] **Step 3: Sanity-check the fixture offline**

Run:
```bash
python -c "import pandas as pd; d=pd.read_parquet('research/tests/fixtures/ohlcv_eth_1h.parquet'); print(d.shape, d.index.tz, list(d.columns)); assert 'close' in d.columns and d.index.tz is not None"
```
Expected: shape `(~5100, 5)`, tz `UTC`, columns include `close`.

- [ ] **Step 4: Commit**

```bash
git add research/tests/fixtures/ohlcv_eth_1h.parquet research/tests/fixtures/_fetch_ohlcv_fixture.py
git commit -m "test(hermes): committed ETH 1H OHLCV fixture for the foundry e2e (one-time OKX fetch)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: docker-gated full-chain e2e via the real reconcile path

**Files:**
- Create: `research/tests/test_hermes_foundry_e2e.py`
- Test: itself (it IS the test)

**Interfaces:**
- Consumes: `reconcile_foundry_jobs`, `resolve_image_id`, `DockerSandbox`, `enqueue_foundry_job`, `ScriptedLLM`, `SandboxContainerGuard`, `call_with_deadline`, `load_features`, the committed ohlcv fixture.
- Produces: nothing (test-only). This is the "the production path actually ran" proof.

**Why:** Every prior task tested a unit. This walks entry → reconcile → run_foundry → forge → sandbox → gatekeeper → evidence card once, against a real container — the link the session's bugs kept missing. The scripted LLM emits dirty markdown (real parse path) and we assert the hypothesis description reached it.

- [ ] **Step 1: Write the e2e (three scripted paths)**

```python
# research/tests/test_hermes_foundry_e2e.py (create)
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from research.hermes.foundry_runner import reconcile_foundry_jobs, resolve_image_id
from research.hermes.orchestrator import enqueue_foundry_job
from research.hermes.sandbox import DockerSandbox, is_docker_available
from research.lib.factor_io import load_features
from research.tests.hermes_support import (
    HUNT_DEADLINE_S, ScriptedLLM, SandboxContainerGuard, call_with_deadline,
)

SANDBOX_TEST_IMAGE = os.environ.get("TALOS_SANDBOX_TEST_IMAGE")
FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv_eth_1h.parquet"
_needs_docker = pytest.mark.skipif(
    not is_docker_available() or not SANDBOX_TEST_IMAGE or not FIXTURE.exists(),
    reason="needs docker daemon + TALOS_SANDBOX_TEST_IMAGE + the ohlcv fixture")


def _stage(tmp_path):
    """Copy a pre-OOS features_eth slice + the ohlcv fixture into a fresh
    manifests dir, aligned so _align_ohlcv's 95% coverage check passes."""
    ohlcv = pd.read_parquet(FIXTURE)
    feats = load_features("eth").loc[ohlcv.index.min():ohlcv.index.max()]
    man = tmp_path / "manifests"; (man / "candidate_features").mkdir(parents=True)
    feats.to_parquet(man / "features_eth.parquet")
    op = tmp_path / "ohlcv_eth.parquet"; ohlcv.reindex(feats.index).to_parquet(op)
    return man, op


def _sandbox(timeout_s=90):
    return DockerSandbox(image=resolve_image_id(SANDBOX_TEST_IMAGE),
                         memory="512m", timeout_s=timeout_s, allow_unpinned=False)


def _enqueue(runs_dir, ohlcv_path):
    return enqueue_foundry_job("eth", runs_dir, {
        "oos_start": "2025-01-01", "interval": "1H", "horizon_h": 24,
        "ohlcv_path": str(ohlcv_path)})


@_needs_docker
def test_healthy_factor_runs_full_pipeline_and_marks_job_done(tmp_path):
    man, op = _stage(tmp_path)
    job_path = _enqueue(tmp_path, op)
    # every hypothesis gets the same legal factor; description is ignored by the
    # fake but MUST still reach it (asserted below).
    llm = ScriptedLLM(["def compute(df):\n    return df['close'].pct_change(20)"])
    summaries = reconcile_foundry_jobs(tmp_path, man, llm, _sandbox(), zoo_dir=_ZOO)
    assert summaries, "no job ran"
    s = summaries[0]
    # engineering facts only -- we do NOT assert a factor passed the gate
    assert s["candidate"] + s["rejected"] + s["forge_failed"] >= 1
    assert "queue_composition" in s and "ohlcv_range" in s
    job = json.loads(Path(job_path).read_text())
    assert job["status"] == "done"
    assert llm.prompts and "compute" in llm.prompts[0]      # the real prompt was built


@_needs_docker
def test_bad_code_is_repaired_from_container_stderr(tmp_path):
    man, op = _stage(tmp_path)
    _enqueue(tmp_path, op)
    llm = ScriptedLLM([
        "def compute(df):\n    return df['nonexistent_col']",     # KeyError in-container
        "def compute(df):\n    return df['close'].pct_change(5)",  # repaired
    ])
    reconcile_foundry_jobs(tmp_path, man, llm, _sandbox(), zoo_dir=_ZOO)
    assert len(llm.prompts) >= 2
    assert "KeyError" in llm.prompts[1] or "nonexistent" in llm.prompts[1]  # stderr fed back


@_needs_docker
def test_infinite_loop_times_out_then_short_circuits(tmp_path):
    man, op = _stage(tmp_path)
    _enqueue(tmp_path, op)
    llm = ScriptedLLM(["def compute(df):\n    while True:\n        pass"])   # same code every retry
    with SandboxContainerGuard() as guard:
        thread, box = call_with_deadline(
            lambda: reconcile_foundry_jobs(tmp_path, man, llm, _sandbox(timeout_s=5), zoo_dir=_ZOO))
        assert not thread.is_alive(), f"reconcile hung past {HUNT_DEADLINE_S}s"
        assert box.get("exc") is None, f"reconcile raised unexpectedly: {box!r}"
        assert guard.new() == [], "container survived the timeout"
```

Add near the top (after imports): `_ZOO = Path("agent/src/factors/zoo")` — the real zoo dir, so `build_queue` finds hypotheses. (The scripted LLM ignores the hypothesis body but the queue must be non-empty.)

- [ ] **Step 2: Build the test image (if not already built)**

Run:
```bash
docker build -f Dockerfile.sandbox-example -t talos-sandbox:test .
```
Expected: image `talos-sandbox:test` exists (`docker images talos-sandbox`).

- [ ] **Step 3: Run the e2e**

Run: `TALOS_SANDBOX_TEST_IMAGE=talos-sandbox:test python -m pytest research/tests/test_hermes_foundry_e2e.py -v`
Expected: 3 PASS. Without docker/image/fixture: 3 SKIP.

If the healthy test fails inside the gate on too-few samples, the features slice is too small — widen the fixture window in Task 9 and re-fetch. If it fails on `queue_composition`/`ohlcv_range` missing, those summary keys are added in the next step.

- [ ] **Step 4: Add the summary keys the e2e asserts (N4)**

If `test_healthy...` fails on `assert "queue_composition" in s`, add to `run_foundry`'s summary construction in `research/hermes/orchestrator.py` (right before `return summary`):

```python
    summary["queue_composition"] = dict(Counter(h.source for h in queue))
    summary["ohlcv_range"] = [str(features.index.min()), str(features.index.max())]
```

Then add a unit test in `test_hermes_orchestrator.py` asserting these keys appear in a `run_foundry` summary (use the existing orchestrator test's fixtures/fakes), run it, and re-run the e2e.

- [ ] **Step 5: Full research suite + leak check**

Run:
```bash
TALOS_SANDBOX_TEST_IMAGE=talos-sandbox:test python -m pytest research/tests/ -q
docker ps -a --filter name=talos_sbx_ --format '{{.Names}}'
```
Expected: all pass/skip; the second command prints nothing (no leaked containers).

- [ ] **Step 6: Commit**

```bash
git add research/tests/test_hermes_foundry_e2e.py research/hermes/orchestrator.py research/tests/test_hermes_orchestrator.py
git commit -m "test(hermes): docker-gated foundry e2e walks the real reconcile path

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §2 sandbox: pin accepts bare id (Task 1), image-existence check + cache (Task 2), exit-125/daemon-down promotion (Task 3). ✅
- §2 foundry_runner: resolve_image_id + load_ohlcv (Task 6), reconcile (Task 7), build_llm + main (Task 8). ✅
- §2 ScriptedLLM in hermes_support (Task 4). ✅
- §2 fixtures: ohlcv (Task 9), features slice (Task 10 `_stage`). ✅
- §3 error layers: A aborts / C continues (Task 7 tests); B untouched (existing). ✅
- §4 test matrix: every row mapped to a task's test. The two merge rows (Task 5), the candidate-merge gap (Task 5), N3 dirty parser (Task 4 + Task 10 prompt assertion), N4 summary keys (Task 10 Step 4). ✅
- Non-goals respected: no OpenRouter client (build_llm raises), no paid run, no fetch in the runner, no sampling strategy. ✅

**Placeholder scan:** no TBD/TODO. `build_llm` raising `NotImplementedError` is a named boundary tested in Task 8, not a silent stub. Task 10 Step 4 is conditional ("if it fails on missing keys") but shows the exact code — not a placeholder.

**Type consistency:** `reconcile_foundry_jobs(runs_dir, manifests_dir, llm, sandbox, zoo_dir, budget=None)` — same signature in Task 7 definition, Task 8 call, Task 10 calls. `resolve_image_id(tag) -> "sha256:..."` consistent across Tasks 6/8/10. `ScriptedLLM(bodies)` / `.prompts` / `.complete` consistent across Tasks 4/10. `load_ohlcv` used by reconcile (Task 7) matches its Task 6 definition. `_looks_like_infra` / `_assert_image_exists` / `_ensure_image` names consistent within sandbox tasks.

**Known deferred (documented in spec, not a plan gap):** `build_llm("openrouter")` and the paid run are the next spec; batch-level LLM cost ceiling is flagged there too.
