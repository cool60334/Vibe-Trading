# Freshness 排程略過已停策略（Part A B2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `freshness_scheduler` 只為「有 trader 正在跑」的幣種排 hourly live_refresh job —— 已停策略（如 xrp_s2，2026-07-01 停）不再每小時白燒 12–22 分鐘算力。

**Architecture:** scheduler `tick()` 前先掃 `runs/testnet/*/control.json`，取出 `desired_state == "running"` 的幣種短名集合，`FRESHNESS_SYMBOLS` 候選清單與其取交集。env `FRESHNESS_IGNORE_CONTROLS=1` 可回舊行為（全刷）。

**Tech Stack:** Python 標準庫、pytest。

**背景（給零 context 的執行者）：**
- `dashboard/server/freshness_scheduler.py`（~70 行）目前行為：對 env `FRESHNESS_SYMBOLS`（如 `sol,xrp`）每小時 `tick()` 一次，若該幣沒有 active（queued/running）的 live_refresh job 就 enqueue 一個。它**只寫 job 檔**，執行由獨立 pipeline runner 負責 —— 本計畫不碰 runner。
- `control.json` 長相：`runs/testnet/<id>/control.json`，欄位 `desired_state`（"running"/"stopped"）、`symbol`（"SOL/USDT:USDT" 或 "SOL-USDT-SWAP" 兩種格式都存在）。幣種短名 = symbol 第一段小寫（`SOL/USDT:USDT` → `sol`）。
- 測試 scope：**`cd dashboard/server && python -m pytest -q`**。已有 `test_freshness_scheduler.py`，沿用其風格（tmp_path 假目錄樹）。
- **檔案所有權**：只准動 `dashboard/server/freshness_scheduler.py` 與 `dashboard/server/test_freshness_scheduler.py`。

---

### Task 1: `running_symbols()` 純函式

**Files:**
- Modify: `dashboard/server/freshness_scheduler.py`
- Test: `dashboard/server/test_freshness_scheduler.py`

- [ ] **Step 1: 寫失敗測試**

在 `test_freshness_scheduler.py` 追加：

```python
import json as _json

from freshness_scheduler import running_symbols


def _write_control(repo_root, testnet_id, desired_state, symbol):
    d = repo_root / "runs" / "testnet" / testnet_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "control.json").write_text(
        _json.dumps({"desired_state": desired_state, "symbol": symbol}),
        encoding="utf-8",
    )


def test_running_symbols_collects_only_running(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    _write_control(tmp_path, "eth_z_paper", "running", "ETH-USDT-SWAP")
    assert running_symbols(tmp_path) == {"sol", "eth"}


def test_running_symbols_empty_tree(tmp_path):
    assert running_symbols(tmp_path) == set()


def test_running_symbols_ignores_corrupt_control(tmp_path):
    d = tmp_path / "runs" / "testnet" / "bad"
    d.mkdir(parents=True)
    (d / "control.json").write_text("{not json", encoding="utf-8")
    assert running_symbols(tmp_path) == set()
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `cd dashboard/server && python -m pytest test_freshness_scheduler.py -q`
Expected: FAIL — `ImportError: cannot import name 'running_symbols'`。

- [ ] **Step 3: 實作**

在 `freshness_scheduler.py` 的 import 區後加：

```python
import json


def _symbol_short(symbol: str) -> str:
    s = str(symbol or "").strip()
    if "/" in s:
        return s.split("/")[0].lower()
    if "-" in s:
        return s.split("-")[0].lower()
    return s.lower()


def running_symbols(repo_root) -> set:
    """Symbols (short form) with at least one control.json desiring 'running'.

    Refreshing factors for a symbol nobody trades burns 12-22 min of compute
    per hour for nothing — the scheduler intersects its candidate list with
    this set. Corrupt/missing controls are skipped (never crash the tick).
    """
    base = Path(repo_root) / "runs" / "testnet"
    out: set = set()
    if base.is_dir():
        for p in base.glob("*/control.json"):
            try:
                ctrl = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if ctrl.get("desired_state") == "running":
                short = _symbol_short(ctrl.get("symbol"))
                if short:
                    out.add(short)
    return out
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `cd dashboard/server && python -m pytest test_freshness_scheduler.py -q`
Expected: 全 passed。

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/freshness_scheduler.py dashboard/server/test_freshness_scheduler.py
git commit -m "feat(freshness): running_symbols() reads control.json states"
```

### Task 2: `tick()` 過濾 + env 逃生門

**Files:**
- Modify: `dashboard/server/freshness_scheduler.py`
- Test: `dashboard/server/test_freshness_scheduler.py`

- [ ] **Step 1: 寫失敗測試**

追加（`tick` 已在檔內被 import 或補 import）：

```python
from datetime import datetime, timezone

from freshness_scheduler import tick


def test_tick_skips_symbols_without_running_trader(tmp_path):
    _write_control(tmp_path, "sol_x_paper", "running", "SOL/USDT:USDT")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    ids = tick(tmp_path, ["sol", "xrp"], now, 3600.0)
    assert len(ids) == 1  # only sol enqueued


def test_tick_ignore_controls_env_refreshes_all(tmp_path, monkeypatch):
    monkeypatch.setenv("FRESHNESS_IGNORE_CONTROLS", "1")
    _write_control(tmp_path, "xrp_y_paper", "stopped", "XRP/USDT:USDT")
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    ids = tick(tmp_path, ["sol", "xrp"], now, 3600.0)
    assert len(ids) == 2


def test_tick_no_controls_at_all_enqueues_nothing(tmp_path):
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    assert tick(tmp_path, ["sol"], now, 3600.0) == []
```

（若既有測試呼叫 `tick()` 且未建 control 檔而預期 enqueue，**更新那些測試**改用 `_write_control` 建 running 控制檔 —— 行為變更是本計畫目的，測試更新屬預期。）

- [ ] **Step 2: 跑測試確認 RED**

Run: `cd dashboard/server && python -m pytest test_freshness_scheduler.py -q`
Expected: 新測試 FAIL（tick 未過濾）。

- [ ] **Step 3: 實作 —— 修改 `tick()`**

把現有 `tick()` 改為：

```python
def tick(repo_root, symbols: list[str], now: datetime,
         interval_sec: float) -> list[str]:
    """One scheduling pass. Returns the job_ids enqueued this pass.

    Symbols without a running trader are skipped (their factors have no
    consumer). FRESHNESS_IGNORE_CONTROLS=1 restores unconditional refresh
    for dev / recovery scenarios.
    """
    if os.environ.get("FRESHNESS_IGNORE_CONTROLS", "").strip() not in ("", "0"):
        active = set(symbols)
    else:
        active = running_symbols(repo_root)

    enqueued: list[str] = []
    for sym in symbols:
        if sym not in active:
            continue
        if _active_live_refresh(repo_root, sym, now, 2 * interval_sec) is None:
            job = pj.create_job(repo_root, kind="live_refresh", symbol=sym,
                                interval="1H")
            enqueued.append(job["job_id"])
    return enqueued
```

- [ ] **Step 4: 跑測試確認 GREEN + 全套 server scope**

Run: `cd dashboard/server && python -m pytest -q`
Expected: 全 passed。

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/freshness_scheduler.py dashboard/server/test_freshness_scheduler.py
git commit -m "feat(freshness): skip hourly refresh for symbols with no running trader"
```

---

## Self-check

- [ ] 已停策略（stopped control）不再被排 job；無任何 control 時不排任何 job。
- [ ] `FRESHNESS_IGNORE_CONTROLS=1` 回舊行為。
- [ ] 沒動 pipeline runner / job 格式 / 其他檔案。
- [ ] 部署備註寫進 commit 或 PR 描述：上線後 xrp 的 hourly job 應消失；重啟 xrp trader 後第一輪 factor 會較舊，trader 自有 stale-pause 保護。
