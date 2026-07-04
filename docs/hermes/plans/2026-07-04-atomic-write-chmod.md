# Atomic write 權限修正（Part A C3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `factor_io` 的 atomic write（mkstemp + os.replace）產出的檔案固定是 0600（mkstemp 規格），伺服器上唯讀觀察帳號讀不到新寫的 parquet/meta —— 在 replace 前 chmod 0o644。

**Architecture:** 兩個 helper（`_atomic_to_parquet`、`_atomic_write_text`）各加一行 `os.chmod(tmp, 0o644)`；用 monkeypatch 驗證呼叫順序（chmod 在 replace 前），POSIX 上另驗實際 mode bits（Windows 的 chmod 語義不同，mode 斷言跳過）。

**Tech Stack:** Python 標準庫、pytest、pandas（寫 parquet 需 pyarrow，環境已有）。

**背景（給零 context 的執行者）：**
- `research/lib/factor_io.py` 約 97–121 行有兩個 atomic helper：`_atomic_to_parquet(df, path)` 與 `_atomic_write_text(path, text)`，皆 `tempfile.mkstemp` → 寫入 → `os.replace`。mkstemp 建檔**固定 0600、無視 umask** —— 這是伺服器上新 parquet 變 `-rw-------` 的根因。
- 測試 scope：**從 repo 根**跑 `python -m pytest research/tests/ -q`。
- **檔案所有權**：只准動 `research/lib/factor_io.py` 與新建 `research/tests/test_factor_io_chmod.py`。

---

### Task 1: chmod 0o644 before replace

**Files:**
- Create: `research/tests/test_factor_io_chmod.py`
- Modify: `research/lib/factor_io.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""Atomic writes must yield group/other-readable files.

tempfile.mkstemp creates 0600 regardless of umask and os.replace preserves
it — read-only observers (fable-ro ACL) lose access to every freshly
rewritten parquet/meta. The helpers must chmod 0o644 BEFORE the replace.
"""
import os
import stat
import sys
from pathlib import Path

import pandas as pd
import pytest

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from lib.factor_io import _atomic_to_parquet, _atomic_write_text  # noqa: E402


def test_atomic_text_chmods_before_replace(tmp_path, monkeypatch):
    calls = []
    real_chmod, real_replace = os.chmod, os.replace
    monkeypatch.setattr(os, "chmod", lambda p, m: (calls.append(("chmod", m)), real_chmod(p, m)))
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(("replace", None)), real_replace(a, b)))

    _atomic_write_text(tmp_path / "x.json", "{}")

    ops = [c[0] for c in calls]
    assert "chmod" in ops, "no chmod call — file stays mkstemp-0600"
    assert ops.index("chmod") < ops.index("replace"), "chmod must precede replace"
    assert ("chmod", 0o644) in calls


def test_atomic_parquet_chmods_before_replace(tmp_path, monkeypatch):
    calls = []
    real_chmod, real_replace = os.chmod, os.replace
    monkeypatch.setattr(os, "chmod", lambda p, m: (calls.append(("chmod", m)), real_chmod(p, m)))
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(("replace", None)), real_replace(a, b)))

    df = pd.DataFrame({"a": [1.0, 2.0]},
                      index=pd.date_range("2026-01-01", periods=2, freq="h"))
    _atomic_to_parquet(df, tmp_path / "x.parquet")

    ops = [c[0] for c in calls]
    assert ops.index("chmod") < ops.index("replace")
    assert ("chmod", 0o644) in calls


@pytest.mark.skipif(os.name != "posix", reason="mode bits only meaningful on POSIX")
def test_atomic_text_final_mode_is_group_readable(tmp_path):
    _atomic_write_text(tmp_path / "y.json", "{}")
    mode = stat.S_IMODE(os.stat(tmp_path / "y.json").st_mode)
    assert mode & stat.S_IRGRP and mode & stat.S_IROTH
```

- [ ] **Step 2: 跑測試確認 RED**

Run（repo 根）: `python -m pytest research/tests/test_factor_io_chmod.py -q`
Expected: 前兩個測試 FAIL（`"chmod" in ops` 不成立）。POSIX 專屬測試在 Windows skip。

- [ ] **Step 3: 實作 —— `factor_io.py` 兩個 helper 各加一行**

`_atomic_to_parquet` 的 try 區塊改為：

```python
    try:
        df.to_parquet(tmp_path, engine="pyarrow", compression="snappy")
        os.chmod(tmp_path, 0o644)  # mkstemp is 0600 regardless of umask; readers need group read
        os.replace(tmp_path, path)
```

`_atomic_write_text` 的 try 區塊改為：

```python
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o644)  # mkstemp is 0600 regardless of umask; readers need group read
        os.replace(tmp_path, path)
```

- [ ] **Step 4: 跑測試確認 GREEN + 全套 research scope**

Run: `python -m pytest research/tests/ -q`
Expected: 全 passed（本檔 2 passed + 1 skipped on Windows）。

- [ ] **Step 5: Commit**

```bash
git add research/lib/factor_io.py research/tests/test_factor_io_chmod.py
git commit -m "fix(factor_io): chmod 0644 before atomic replace (mkstemp is 0600)"
```

---

## Self-check

- [ ] chmod 在 replace **之前**（replace 後 chmod 會有一瞬 0600 檔案暴露給讀者？不 —— 重點是舊檔在 replace 前仍在，任何時刻讀者看到的不是 0600 檔）。
- [ ] 只動兩個 helper，各一行；沒動呼叫端。
- [ ] Windows 上測試不假斷言 mode bits。
