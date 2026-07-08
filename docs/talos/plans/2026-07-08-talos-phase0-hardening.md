# Talos Phase 0 — 基礎設施硬化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建 Talos Factor Foundry 的五道基礎設施硬化防線，**在寫任何自動產因子邏輯之前**——否則產出全是海市蜃樓且會污染 production `research/`。

**Architecture:** 五個純 Python/Docker 模組落於新目錄 `research/hermes/`（目錄名沿用舊代號，文件內稱 Talos）。全部確定性、無 LLM。複用既有 `factor_io._atomic_to_parquet`、`factor_metrics`、`research_config.yaml` 的 `oos_start`。沙盒採兩層：AST 白名單靜態閘（layer-0，純 Python）+ Docker OS 級隔離（layer-1，`--network=none --memory --read-only`），兩者以 `SandboxExecutor` 介面抽象，dev/prod 一致。

**Tech Stack:** Python 3.11、pandas、pyarrow、Python `ast`、Docker CLI（`subprocess` 呼叫）、pytest（research scope，repo 根跑 `python -m pytest research/tests/`）。

**設計來源：** `docs/talos/talos-design.md` §3.6 Phase 0（五項）+ 附錄 C（agy 二審 11 點）。agy 沙盒策略諮詢採「路④ Docker-backed 介面 + AST layer-0」。

---

## File Structure

| 檔案 | 責任 | 依賴 |
|------|------|------|
| `research/hermes/__init__.py` | 套件標記 + 公開匯出 | — |
| `research/hermes/pit.py` | Point-in-time no-lookahead 驗證 harness（perturbation-based，抓 bfill/負 shift/任何未來洩漏） | 純 py |
| `research/hermes/split.py` | 嚴格 foundry 資料切分（僅 pre-`oos_start`，train/val，鎖死 walk-forward OOS） | 純 py + config |
| `research/hermes/candidate_store.py` | 隔離候選特徵庫 atomic write + **拒寫 production path** 硬 guard | 複用 `factor_io` |
| `research/hermes/sandbox_ast.py` | Layer-0 AST 白名單靜態閘（禁危險 import/call + 封 `bfill`/`backfill`） | 純 py `ast` |
| `research/hermes/sandbox.py` | Layer-1 `SandboxExecutor` 介面 + `DockerSandbox` OS 級隔離執行 | Docker CLI |
| `research/tests/test_hermes_*.py` | 每模組假資料單測 | pytest |

**隔離不變式（貫穿全 Phase）**：`research/hermes/` 的任何寫入路徑**絕不**碰 production `features_<sym>.parquet` / `factor_values_<sym>.parquet`（實盤 trader + freshness scheduler 讀）。只寫 `candidate_features/`。

---

## Task 1: PIT no-lookahead 驗證 harness

**Files:**
- Create: `research/hermes/__init__.py`
- Create: `research/hermes/pit.py`
- Test: `research/tests/test_hermes_pit.py`

概念：給一個 `compute_fn(df) -> pd.Series`（在 OHLCV df 上算特徵），harness 把 `perturb_from` 之後的 row 全部汙染（NaN + `1e10`），重算，斷言 `perturb_from` 之前的特徵值**不變**。任何未來洩漏（負 shift、`bfill`、`.iloc[t+1]`）都會讓早期值飄移而被抓。此為 runtime 防線，比 AST 更強。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_pit.py
import numpy as np
import pandas as pd
import pytest
from research.hermes.pit import assert_no_lookahead, LookaheadError


def _panel(n=300, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    close = 100.0 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame({"close": close, "volume": rng.random(n) * 1000}, index=idx)


def test_causal_feature_passes():
    df = _panel()
    # 5-bar momentum: only past+current → causal, must pass
    assert_no_lookahead(lambda d: d["close"].pct_change(5), df) is None


def test_bfill_leak_is_caught():
    df = _panel()
    # bfill pulls FUTURE close into present NaN → must raise
    def leaky(d):
        s = d["close"].copy()
        s.iloc[100] = np.nan
        return s.bfill()
    with pytest.raises(LookaheadError):
        assert_no_lookahead(leaky, df)


def test_negative_shift_leak_is_caught():
    df = _panel()
    with pytest.raises(LookaheadError):
        assert_no_lookahead(lambda d: d["close"].shift(-1), df)  # tomorrow's close
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_pit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.hermes'`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/__init__.py
"""Talos Factor Foundry infrastructure (Phase 0 hardening)."""
```

```python
# research/hermes/pit.py
"""Point-in-time guard: a feature value at row t must not depend on rows > t.

Perturbation harness (stronger than AST): corrupt all rows >= perturb_from,
recompute, and assert every value BEFORE perturb_from is unchanged. Any
future leak — negative shift, bfill/backfill, .iloc[t+1] — shifts an earlier
value and is caught. Reused idea from agent/tests/factors/test_lookahead.py,
adapted to single-symbol OHLCV research frames.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PROBE_FROM_DEFAULT = 200   # rows [0, perturb_from) must be invariant
PERTURB_GAP = 10           # first corrupted row = probe_end + gap


class LookaheadError(AssertionError):
    """Raised when a feature's past values change after the future is corrupted."""


def assert_no_lookahead(
    compute_fn,
    df: pd.DataFrame,
    perturb_from: int | None = None,
    atol: float = 1e-9,
    rtol: float = 1e-9,
) -> None:
    """Return None if causal; raise LookaheadError if the feature peeks ahead.

    compute_fn(df) -> pd.Series aligned to df.index.
    """
    n = len(df)
    if perturb_from is None:
        perturb_from = min(PROBE_FROM_DEFAULT, n - PERTURB_GAP - 1)
    if perturb_from <= 0 or perturb_from >= n:
        raise ValueError(f"perturb_from {perturb_from} out of range for n={n}")

    base = np.asarray(compute_fn(df), dtype="float64")[:perturb_from]

    corrupt = df.copy()
    corrupt.iloc[perturb_from:] = 1e10
    # alternate NaN into half the corrupted block so both sentinels are exercised
    corrupt.iloc[perturb_from + PERTURB_GAP:] = np.nan
    after = np.asarray(compute_fn(corrupt), dtype="float64")[:perturb_from]

    if not np.allclose(base, after, atol=atol, rtol=rtol, equal_nan=True):
        drift = np.nanmax(np.abs(base - after))
        raise LookaheadError(
            f"feature peeks into the future: max drift {drift:.3e} in rows "
            f"[0,{perturb_from}) after corrupting rows >= {perturb_from}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_pit.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/__init__.py research/hermes/pit.py research/tests/test_hermes_pit.py
git commit -m "feat(hermes): PIT no-lookahead perturbation harness (Phase 0)"
```

---

## Task 2: 嚴格 foundry 資料切分

**Files:**
- Create: `research/hermes/split.py`
- Test: `research/tests/test_hermes_split.py`

概念：Foundry **只准**在 `pre-oos_start` 資料內切 train/val；pipeline 的 walk-forward OOS（>= `oos_start`）**鎖死**、Foundry 不得觸碰，保留給晉級前一次性 `final_holdout`。函式若收到任何 >= oos_start 的 row 會回傳只含 pre-oos 的切分，且提供一個嚴格模式在洩漏時 raise。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_split.py
import numpy as np
import pandas as pd
import pytest
from research.hermes.split import foundry_split, OOSLeakError


def _df(start, n, freq="1D"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"x": np.arange(n)}, index=idx)


def test_split_drops_oos_and_orders_train_before_val():
    df = _df("2024-06-01", 400)  # spans past 2025-01-01 oos_start
    train, val = foundry_split(df, oos_start="2025-01-01", val_frac=0.25)
    assert train.index.max() < pd.Timestamp("2025-01-01")
    assert val.index.max() < pd.Timestamp("2025-01-01")
    assert train.index.max() <= val.index.min()          # chronological, no shuffle
    assert len(val) == round(0.25 * (len(train) + len(val)))


def test_strict_mode_raises_when_caller_preselected_oos_rows():
    df = _df("2025-02-01", 30)  # entirely inside locked OOS
    with pytest.raises(OOSLeakError):
        foundry_split(df, oos_start="2025-01-01", val_frac=0.2, strict=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_split.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/split.py
"""Foundry train/val split — locked to pre-oos_start data only.

The pipeline's walk-forward OOS window (index >= oos_start) is reserved and
must never be seen during factor discovery; final_holdout.py is the only
path allowed to touch it. Foundry carves train/val ONLY from pre-oos rows,
chronologically (no shuffle — crypto is autocorrelated).
"""
from __future__ import annotations

import pandas as pd


class OOSLeakError(AssertionError):
    """Raised in strict mode when the input already contains OOS rows."""


def foundry_split(
    df: pd.DataFrame,
    oos_start: str,
    val_frac: float = 0.2,
    strict: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (train, val), both strictly before oos_start, val chronologically last."""
    if not 0 < val_frac < 1:
        raise ValueError(f"val_frac must be in (0,1), got {val_frac}")
    cutoff = pd.Timestamp(oos_start)
    pre = df[df.index < cutoff]
    if strict and len(pre) != len(df):
        raise OOSLeakError(
            f"{len(df) - len(pre)} rows >= oos_start {oos_start} leaked into split input"
        )
    if pre.empty:
        raise ValueError(f"no rows before oos_start {oos_start}")
    n_val = round(val_frac * len(pre))
    split_at = len(pre) - n_val
    return pre.iloc[:split_at], pre.iloc[split_at:]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_split.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/split.py research/tests/test_hermes_split.py
git commit -m "feat(hermes): foundry train/val split locked to pre-oos_start (Phase 0)"
```

---

## Task 3: 隔離候選特徵庫（atomic write + 拒 production path）

**Files:**
- Create: `research/hermes/candidate_store.py`
- Test: `research/tests/test_hermes_candidate_store.py`
- Reference: `research/lib/factor_io.py:97` (`_atomic_to_parquet`)

概念：Foundry 產出寫 `manifests/candidate_features/<sym>.parquet`，用既有 atomic write（parquet 無 native append，nightly 併發直寫會毀檔）。硬 guard：目標路徑若不在 `candidate_features/` 下（例如意外指向 production `features_`/`factor_values_`）直接 raise。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_candidate_store.py
import pandas as pd
import pytest
from research.hermes.candidate_store import write_candidate, ProductionWriteError, CANDIDATE_SUBDIR


def _df():
    idx = pd.date_range("2024-01-01", periods=5, freq="1h")
    return pd.DataFrame({"cand_feat": [1.0, 2, 3, 4, 5]}, index=idx)


def test_writes_under_candidate_dir_atomically(tmp_path):
    path = write_candidate(_df(), "eth", manifests_dir=tmp_path)
    assert CANDIDATE_SUBDIR in path.parts
    assert path.exists()
    assert not list(path.parent.glob("*.tmp"))          # no temp leftover
    pd.testing.assert_frame_equal(pd.read_parquet(path), _df())


def test_refuses_production_feature_path(tmp_path, monkeypatch):
    # simulate a caller trying to redirect output at the production store
    from research.hermes import candidate_store
    monkeypatch.setattr(candidate_store, "CANDIDATE_SUBDIR", "features")  # production dir name
    with pytest.raises(ProductionWriteError):
        write_candidate(_df(), "eth", manifests_dir=tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_candidate_store.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/candidate_store.py
"""Isolated candidate-feature store — NEVER writes production parquet.

Foundry output lands in manifests/candidate_features/<sym>.parquet. Production
features_<sym>.parquet / factor_values_<sym>.parquet (read by the live trader +
freshness scheduler) are off-limits; a human promote step merges candidates in.
Atomic write reuses factor_io so concurrent nightly iterations can't corrupt.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from research.lib.factor_io import _atomic_to_parquet, _symbol_short

CANDIDATE_SUBDIR = "candidate_features"
# production basenames Foundry must never target
_PRODUCTION_PREFIXES = ("features_", "factor_values_")


class ProductionWriteError(RuntimeError):
    """Raised when a write would land outside the isolated candidate store."""


def _candidate_path(symbol: str, manifests_dir: Path) -> Path:
    return Path(manifests_dir) / CANDIDATE_SUBDIR / f"cand_{_symbol_short(symbol)}.parquet"


def write_candidate(df: pd.DataFrame, symbol: str, manifests_dir) -> Path:
    """Atomically write a candidate feature frame; refuse any production path."""
    path = _candidate_path(symbol, Path(manifests_dir))
    if CANDIDATE_SUBDIR not in path.parts or path.name.startswith(_PRODUCTION_PREFIXES):
        raise ProductionWriteError(
            f"candidate write must stay under {CANDIDATE_SUBDIR}/, got {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_to_parquet(df, path)
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_candidate_store.py -v`
Expected: PASS (2 tests)

> 注意：若 `_symbol_short` 或 `_atomic_to_parquet` 簽名與此不符，先 `Read research/lib/factor_io.py` 對齊真實簽名再實作——勿臆測。

- [ ] **Step 5: Commit**

```bash
git add research/hermes/candidate_store.py research/tests/test_hermes_candidate_store.py
git commit -m "feat(hermes): isolated candidate store, atomic write + production guard (Phase 0)"
```

---

## Task 4: Layer-0 AST 白名單靜態閘

**Files:**
- Create: `research/hermes/sandbox_ast.py`
- Test: `research/tests/test_hermes_sandbox_ast.py`

概念：執行 LLM 產碼**前**先 parse 成 AST，白名單制擋掉危險 import 與呼叫。成本低、防禦高（agy ③）。同時封殺 `bfill`/`backfill`（agy C-5：無聲拿未來值補現在，perturbation harness 已能抓，但靜態閘讓失敗更早更明確）。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_sandbox_ast.py
import pytest
from research.hermes.sandbox_ast import check_source, UnsafeCodeError

SAFE = "import pandas as pd\nimport numpy as np\n\ndef compute(df):\n    return df['close'].pct_change(5)\n"


def test_safe_source_passes():
    assert check_source(SAFE) is None


@pytest.mark.parametrize("bad", [
    "import os\n",
    "import socket\n",
    "from subprocess import run\n",
    "import requests\n",
    "open('/etc/passwd')\n",
    "eval('1+1')\n",
    "exec('x=1')\n",
    "__import__('os')\n",
    "df['close'].bfill()\n",
    "df['close'].fillna(method='bfill')\n",
    "df['close'].backfill()\n",
])
def test_unsafe_source_raises(bad):
    with pytest.raises(UnsafeCodeError):
        check_source("import pandas as pd\n" + bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_sandbox_ast.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/sandbox_ast.py
"""Layer-0 static gate: reject dangerous LLM-generated ETL before execution.

Allowlist imports; ban filesystem/network/eval primitives; ban backward-fill
(bfill/backfill/fillna(method='bfill')) which silently pulls future values into
the present. Defence-in-depth on top of the Docker sandbox (layer-1) and the
PIT perturbation harness.
"""
from __future__ import annotations

import ast

ALLOWED_IMPORTS = {"pandas", "numpy", "scipy", "ta", "math", "statistics"}
BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "getattr", "setattr"}
BANNED_ATTRS = {"bfill", "backfill"}


class UnsafeCodeError(ValueError):
    """Raised when source violates the allowlist / bans."""


def check_source(src: str) -> None:
    """Return None if the source is safe; raise UnsafeCodeError otherwise."""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise UnsafeCodeError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise UnsafeCodeError(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise UnsafeCodeError(f"import not allowed: from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                raise UnsafeCodeError(f"banned call: {fn.id}")
            if isinstance(fn, ast.Attribute) and fn.attr in BANNED_ATTRS:
                raise UnsafeCodeError(f"banned method: .{fn.attr}()")
            # fillna(method='bfill'/'backfill')
            if isinstance(fn, ast.Attribute) and fn.attr == "fillna":
                for kw in node.keywords:
                    if (kw.arg == "method" and isinstance(kw.value, ast.Constant)
                            and kw.value.value in ("bfill", "backfill")):
                        raise UnsafeCodeError("banned: fillna(method='bfill')")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            raise UnsafeCodeError(f"banned attribute: .{node.attr}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_sandbox_ast.py -v`
Expected: PASS (1 + 11 parametrized)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/sandbox_ast.py research/tests/test_hermes_sandbox_ast.py
git commit -m "feat(hermes): layer-0 AST allowlist gate + bfill ban (Phase 0)"
```

---

## Task 5: Layer-1 Docker 沙盒執行

**Files:**
- Create: `research/hermes/sandbox.py`
- Test: `research/tests/test_hermes_sandbox.py`

概念：`SandboxExecutor` 介面 + `DockerSandbox` 實作。執行前先過 layer-0 AST 閘，再丟進 `docker run --rm --network=none --memory=<cap> --cpus=1 --read-only -v <in>:/in:ro -v <out>:/out:rw` 執行 runner。斷網/OOM/唯讀/timeout 全 OS 級。無 daemon 時 docker 測試 skip；AST 整合測試不需 docker。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_sandbox.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_sandbox.py -v`
Expected: FAIL — `ModuleNotFoundError` (docker test collected but the import fails first)

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/sandbox.py
"""Layer-1 sandbox: OS-level isolation for LLM-generated feature ETL.

SandboxExecutor is the stable interface; DockerSandbox is the only impl
(agy path ④ — dev/prod parity, no hand-rolled leaky subprocess sandbox).
Every run passes the layer-0 AST gate first, then executes inside a locked
container: --network=none (offline), --memory (OOM cap), --read-only rootfs
with a single writable /out mount, hard timeout via subprocess.
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from research.hermes.sandbox_ast import check_source

DEFAULT_IMAGE = "python:3.11-slim"


def is_docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


class SandboxExecutor(ABC):
    @abstractmethod
    def run(self, source: str, input_parquet, output_dir) -> str:
        ...


class DockerSandbox(SandboxExecutor):
    def __init__(self, image: str = DEFAULT_IMAGE, memory: str = "1g",
                 cpus: str = "1", timeout_s: int = 120):
        self.image, self.memory, self.cpus, self.timeout_s = image, memory, cpus, timeout_s

    def _build_command(self, in_container_path: str, output_dir: str, runner: str) -> list:
        return [
            "docker", "run", "--rm",
            "--network=none",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            "--read-only",
            "--pids-limit=128",
            "-v", f"{output_dir}:/out:rw",
            "-v", f"{runner}:/app/runner.py:ro",
            self.image,
            "python", "/app/runner.py", in_container_path, "/out/candidate.parquet",
        ]

    def run(self, source: str, input_parquet, output_dir) -> str:
        check_source(source)                       # layer-0 gate FIRST
        if not is_docker_available():
            raise RuntimeError("docker daemon unavailable; cannot run sandboxed ETL")
        # NOTE: writing `source` + input mount + runner materialisation is completed
        # in Task 6 wiring; this method establishes the hardened command surface.
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        cmd = self._build_command(str(input_parquet), str(output_dir), runner="/app/runner.py")
        proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"sandbox run failed: {proc.stderr[-500:]}")
        return str(Path(output_dir) / "candidate.parquet")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_sandbox.py -v`
Expected: PASS for AST-gate + flags tests; container test SKIPPED if no daemon (start Docker Desktop to run it green).

- [ ] **Step 5: Commit**

```bash
git add research/hermes/sandbox.py research/tests/test_hermes_sandbox.py
git commit -m "feat(hermes): layer-1 DockerSandbox hardened execution surface (Phase 0)"
```

---

## Task 6: Sandbox runner materialisation + Phase 0 整合驗證

**Files:**
- Create: `research/hermes/_runner_template.py` (在容器內執行 LLM `compute(df)` 的固定 runner)
- Modify: `research/hermes/sandbox.py` (把 source + runner 實體化進 container，補完 Task 5 的 NOTE)
- Modify: `research/hermes/__init__.py` (公開匯出)
- Test: `research/tests/test_hermes_phase0_integration.py`

概念：容器內需要一個固定 runner：讀 `/in` parquet → `exec` 受檢過的 `compute` → 寫 `/out/candidate.parquet`。runner 本身唯讀掛入，LLM 碰不到。整合測試把 PIT + split + candidate_store 串起來跑一條 happy path（不需 docker）。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_phase0_integration.py
import numpy as np
import pandas as pd
from research.hermes.pit import assert_no_lookahead
from research.hermes.split import foundry_split
from research.hermes.candidate_store import write_candidate


def test_end_to_end_causal_factor_pipeline(tmp_path):
    idx = pd.date_range("2024-06-01", periods=400, freq="1D")
    df = pd.DataFrame({"close": 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 400))}, index=idx)

    compute = lambda d: d["close"].pct_change(5)     # causal
    assert_no_lookahead(compute, df) is None          # PIT gate

    train, val = foundry_split(df, oos_start="2025-01-01", val_frac=0.2)
    assert train.index.max() < pd.Timestamp("2025-01-01")

    feat = pd.DataFrame({"mom5": compute(train)})
    path = write_candidate(feat, "eth", manifests_dir=tmp_path)
    assert path.exists() and "candidate_features" in path.parts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_phase0_integration.py -v`
Expected: FAIL — `_runner_template` import / `__init__` export mismatch (or passes if exports already fine — then just proceed to strengthen runner)

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/_runner_template.py
"""Runs inside the offline container. Reads /in parquet, execs the (already
AST-gated) user compute(df), writes /out/candidate.parquet. Mounted read-only."""
import sys
import pandas as pd

if __name__ == "__main__":
    in_path, out_path = sys.argv[1], sys.argv[2]
    df = pd.read_parquet(in_path)
    ns: dict = {}
    exec(open("/app/user_source.py").read(), {"pd": pd, "__builtins__": __builtins__}, ns)
    result = ns["compute"](df)
    pd.DataFrame({"candidate": result}).to_parquet(out_path)
```

```python
# research/hermes/__init__.py  (replace stub)
"""Talos Factor Foundry infrastructure (Phase 0 hardening)."""
from research.hermes.pit import assert_no_lookahead, LookaheadError
from research.hermes.split import foundry_split, OOSLeakError
from research.hermes.candidate_store import write_candidate, ProductionWriteError
from research.hermes.sandbox_ast import check_source, UnsafeCodeError
from research.hermes.sandbox import DockerSandbox, SandboxExecutor, is_docker_available

__all__ = [
    "assert_no_lookahead", "LookaheadError",
    "foundry_split", "OOSLeakError",
    "write_candidate", "ProductionWriteError",
    "check_source", "UnsafeCodeError",
    "DockerSandbox", "SandboxExecutor", "is_docker_available",
]
```

在 `sandbox.py` 的 `run()` 補完 source/runner 實體化（取代 Task 5 的 NOTE）：把 `source` 寫入一個暫存 `user_source.py`、把它與 `_runner_template.py` 都以 `:ro` 掛入容器 `/app/`，input parquet 掛 `/in:ro`。實作時 `Read` 現況 `run()` 再改，勿臆測既有內容。

- [ ] **Step 4: Run test + full Phase 0 suite**

Run: `python -m pytest research/tests/test_hermes_phase0_integration.py research/tests/test_hermes_pit.py research/tests/test_hermes_split.py research/tests/test_hermes_candidate_store.py research/tests/test_hermes_sandbox_ast.py research/tests/test_hermes_sandbox.py -v`
Expected: ALL PASS（docker 容器測試 SKIP 若無 daemon）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/_runner_template.py research/hermes/__init__.py research/hermes/sandbox.py research/tests/test_hermes_phase0_integration.py
git commit -m "feat(hermes): sandbox runner + Phase 0 integration (PIT+split+store) (Phase 0)"
```

---

## Self-Review

**Spec coverage（對 talos-design.md §3.6 Phase 0 五項 + agy C 點）：**
- ① PIT 防 look-ahead → Task 1（perturbation harness，抓 bfill/負 shift，C-5）✅
- ② 隔離候選庫 → Task 3（拒 production path，C-8 atomic write）✅
- ③ 產碼沙盒 → Task 4（AST layer-0，C-5 bfill）+ Task 5（Docker layer-1，C-10 root RO+output mount，C-11 斷網）✅
- ④ 嚴格資料切分 → Task 2（pre-oos_start，walk-forward 鎖死）✅
- ⑤ 假資料單測 → 每 Task Step 1 + Task 6 整合 ✅

**尚未涵蓋（刻意留給 Phase 1，非本計畫）：** Net-return IC / DSR-from-ledger / 非重疊 IC / regime 分解 / 墓地數值比對（C-1/C-3/C-4/C-6）——皆屬 Foundry 統計守門，Phase 1 才實作。C-2 abs()、C-7 lag 亦 Phase 1。C-9 paper v1 不做。**Phase 0 只建地基，不建守門。**

**Placeholder scan：** Task 5 `run()` 有一個明示 NOTE 由 Task 6 補完（非隱藏 placeholder，已標交接點）；Task 3/6 明示「實作前先 Read 既有簽名對齊」以防臆測 `_atomic_to_parquet`/`_symbol_short`/`run()` 真實介面。

**Type consistency：** 例外類名跨檔一致（LookaheadError/OOSLeakError/ProductionWriteError/UnsafeCodeError）；`check_source` 在 sandbox_ast 定義、sandbox.py + __init__ 引用一致；`is_docker_available` 定義於 sandbox.py、測試引用一致。

**已知風險（誠實記錄）：**
- `_runner_template.py` 用 `exec` 跑 user source——**安全靠容器隔離（--network=none/--read-only/--memory）而非 exec 本身**；AST 閘為 defence-in-depth。這是刻意架構（agy ②：Python 層 monkeypatch 易繞，靠 OS 隔離）。
- Windows dev：Docker 容器測試需 Docker Desktop daemon 開著；純 Python 測試（Task 1-4、6 整合）無此依賴，隨時可跑。

---

## Execution Handoff

計畫存 `docs/talos/plans/2026-07-08-talos-phase0-hardening.md`。兩執行選項：

1. **Subagent-Driven（推薦）** — 每 Task 派新 subagent，Task 間審查，快迭代。
2. **Inline Execution** — 本 session 逐 Task 跑 executing-plans，批次+檢查點。

依使用者鐵律「檢查點制、每 Task 停等核准」，兩者都會在每 Task 完成後停。挑哪個？
