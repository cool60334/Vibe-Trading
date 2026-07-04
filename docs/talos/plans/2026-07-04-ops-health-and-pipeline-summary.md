# 感知層：pipeline_summary + ops_health + 版本 beacon（Part A D1+D3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 兩個 machine-readable、全 UTC 的彙總產物：① `pipeline_summary.json` —— stage5 收尾時每策略一行 verdict/gate/OOS 摘要；② `GET /api/ops/health` —— 部署 git 版本 + factor 新鮮度 + job 佇列 + trader 心跳。這是自主決策層（Talos）與人類的共同「眼睛」，也堵住「伺服器落後 18 commits 沒人知道」的 drift 盲區。

**Architecture:** 純讀既有 artifacts 再彙總，**不改任何現有 artifact 格式**。summary 由 stage5 在 emit manifests 後順手寫（失敗不 fail stage）；ops health 為 FastAPI endpoint 即時計算（無背景工作、無快取，YAGNI）。版本 beacon 直接讀 `.git/HEAD`（部署 = git pull，HEAD 即部署版本，無需 deploy 腳本）。

**Tech Stack:** Python 標準庫、FastAPI（既有）、pytest。

**背景（給零 context 的執行者）：**
- 兩個 pytest scope 都會用到，**分開跑**：research 測試從 repo 根 `python -m pytest research/tests/ -q`；dashboard 測試 `cd dashboard/server && python -m pytest -q`。
- 資料來源格式：
  - `research/manifests/<strategy_id>/manifest.json` —— stage5 emit 的 StrategyManifest。**第一步先開 `research/emit_manifest.py` 確認欄位名**（`selected`、`gate.overall_pass`、`gate.fatal_fail`、`backtest.oos.{sharpe,max_drawdown,trade_count}`、diagnosis 的 `recommended_action`）；下方代碼用防禦性 `.get`，欄位名若有出入以 emit_manifest.py 為準修正代碼與測試。
  - `research/manifests/factor_values_<sym>.meta.json` —— `generated_at`、`index_end`（UTC ISO）。
  - `runs/pipeline_jobs/<job_id>/job.json` —— `status`（queued/running/succeeded/failed/canceled）、`created_at`。
  - `runs/testnet/<id>/control.json`（`desired_state`）與 `testnet_status.json`（`live.status/updated_at/equity/trades`）。
  - `.git/HEAD`：`ref: refs/heads/<branch>`；commit 在 `.git/refs/heads/<branch>` 或 `.git/packed-refs`。
- **檔案所有權**：`research/pipeline/lib/pipeline_summary.py`（新）、`research/pipeline/stage5_select.py`（只加收尾呼叫）、`research/tests/test_pipeline_summary.py`（新）、`dashboard/server/ops_health.py`（新）、`dashboard/server/main.py`（只加一個 route）、`dashboard/server/test_ops_health.py`（新）。不准動其他檔。

---

### Task 1: `pipeline_summary` 模組（research 側）

**Files:**
- Create: `research/pipeline/lib/pipeline_summary.py`
- Create: `research/tests/test_pipeline_summary.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""pipeline_summary — one machine-readable verdict file per pipeline run."""
import json
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from pipeline.lib.pipeline_summary import build_pipeline_summary, write_pipeline_summary  # noqa: E402


def _manifest(selected, action, fatal, sharpe):
    return {
        "selected": selected,
        "diagnosis": {"recommended_action": action},
        "gate": {"overall_pass": not fatal, "fatal_fail": fatal},
        "backtest": {"oos": {"sharpe": sharpe, "max_drawdown": -0.05, "trade_count": 42}},
    }


def _write(manifests, sid, data):
    d = manifests / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


def test_build_summary_one_row_per_strategy(tmp_path):
    _write(tmp_path, "eth_s5", _manifest(True, "proceed", False, 1.02))
    _write(tmp_path, "btc_s9", _manifest(False, "back_to_stage_4", True, 0.3))

    s = build_pipeline_summary(tmp_path)

    assert s["schema_version"] == 1
    assert s["generated_at"].endswith("+00:00") or s["generated_at"].endswith("Z")
    row = s["strategies"]["eth_s5"]
    assert row == {
        "selected": True, "recommended_action": "proceed",
        "gate_overall_pass": True, "gate_fatal_fail": False,
        "oos_sharpe": 1.02, "oos_max_drawdown": -0.05, "oos_trade_count": 42,
    }
    assert s["strategies"]["btc_s9"]["gate_fatal_fail"] is True


def test_build_summary_tolerates_corrupt_and_partial_manifests(tmp_path):
    d = tmp_path / "broken"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, "bare", {})  # every field missing → Nones, no crash

    s = build_pipeline_summary(tmp_path)

    assert "broken" not in s["strategies"]
    assert s["strategies"]["bare"]["oos_sharpe"] is None


def test_write_summary_creates_file(tmp_path):
    _write(tmp_path, "eth_s5", _manifest(True, "proceed", False, 1.0))
    p = write_pipeline_summary(tmp_path)
    assert p == tmp_path / "pipeline_summary.json"
    assert json.loads(p.read_text(encoding="utf-8"))["strategies"]["eth_s5"]["selected"] is True
```

- [ ] **Step 2: 跑測試確認 RED**

Run（repo 根）: `python -m pytest research/tests/test_pipeline_summary.py -q`
Expected: FAIL — `ModuleNotFoundError`。

- [ ] **Step 3: 實作**

`research/pipeline/lib/pipeline_summary.py`：

```python
"""Aggregate per-strategy manifests into one pipeline_summary.json.

Answers "what did this pipeline run conclude?" in one machine-readable,
all-UTC file — the direct input for any automation deciding what to do
next, instead of crawling manifests/selection/diagnosis separately.
Read-only over existing artifacts; never mutates them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _summarise_manifest(data: dict) -> dict:
    gate = data.get("gate") or {}
    oos = (data.get("backtest") or {}).get("oos") or {}
    diagnosis = data.get("diagnosis") or {}
    return {
        "selected": data.get("selected"),
        "recommended_action": diagnosis.get("recommended_action")
                              or data.get("recommended_action"),
        "gate_overall_pass": gate.get("overall_pass"),
        "gate_fatal_fail": gate.get("fatal_fail"),
        "oos_sharpe": oos.get("sharpe"),
        "oos_max_drawdown": oos.get("max_drawdown"),
        "oos_trade_count": oos.get("trade_count"),
    }


def build_pipeline_summary(manifests_dir: "str | Path") -> dict:
    manifests_dir = Path(manifests_dir)
    strategies: dict = {}
    for mf in sorted(manifests_dir.glob("*/manifest.json")):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # a corrupt manifest must not sink the summary
        strategies[mf.parent.name] = _summarise_manifest(data)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "strategies": strategies,
    }


def write_pipeline_summary(manifests_dir: "str | Path") -> Path:
    manifests_dir = Path(manifests_dir)
    out = manifests_dir / "pipeline_summary.json"
    out.write_text(
        json.dumps(build_pipeline_summary(manifests_dir), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `python -m pytest research/tests/test_pipeline_summary.py -q`
Expected: 3 passed。

- [ ] **Step 5: stage5 收尾佈線**

開 `research/pipeline/stage5_select.py`，搜 `emit_manifest_for_strategy`，在該 emit 迴圈**完成後**（同縮排層）插入（`manifests_dir` 用該迴圈既有的目錄變數，變數名以檔內為準）：

```python
    # ── Machine-readable run verdicts (Talos / ops input) ────────────────────
    from pipeline.lib.pipeline_summary import write_pipeline_summary
    try:
        _summary_path = write_pipeline_summary(manifests_dir)
        print(f"[stage5] wrote {_summary_path}")
    except Exception as exc:  # noqa: BLE001 — summary must never fail the stage
        print(f"[stage5] WARN: pipeline_summary failed: {exc}")
```

- [ ] **Step 6: 跑全套 research 測試 + Commit**

Run: `python -m pytest research/tests/ -q` → 全綠。

```bash
git add research/pipeline/lib/pipeline_summary.py research/tests/test_pipeline_summary.py research/pipeline/stage5_select.py
git commit -m "feat(stage5): emit pipeline_summary.json — one-file run verdicts"
```

### Task 2: `ops_health` + 版本 beacon（dashboard 側）

**Files:**
- Create: `dashboard/server/ops_health.py`
- Create: `dashboard/server/test_ops_health.py`
- Modify: `dashboard/server/main.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""ops_health — one all-UTC snapshot: version, freshness, jobs, traders."""
import json
from pathlib import Path

from ops_health import build_ops_health, deployed_version


def _repo(tmp_path):
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/quant-trading-dashboard\n")
    (tmp_path / ".git" / "refs" / "heads" / "quant-trading-dashboard").write_text("abc123\n")
    m = tmp_path / "research" / "manifests"
    m.mkdir(parents=True)
    (m / "factor_values_eth.meta.json").write_text(json.dumps(
        {"generated_at": "2026-07-04T00:00:00+00:00", "index_end": "2026-07-03T23:00:00+00:00"}))
    j = tmp_path / "runs" / "pipeline_jobs" / "20260704T000000Z-aaaaaa"
    j.mkdir(parents=True)
    (j / "job.json").write_text(json.dumps(
        {"status": "succeeded", "created_at": "2026-07-04T00:00:00+00:00"}))
    t = tmp_path / "runs" / "testnet" / "eth_s5_paper"
    t.mkdir(parents=True)
    (t / "control.json").write_text(json.dumps({"desired_state": "running"}))
    (t / "testnet_status.json").write_text(json.dumps(
        {"live": {"status": "running", "updated_at": "2026-07-04T09:00:00+00:00",
                  "equity": 10000.0, "trades": 0}}))
    return tmp_path


def test_deployed_version_reads_head_and_loose_ref(tmp_path):
    v = deployed_version(_repo(tmp_path))
    assert v == {"branch": "quant-trading-dashboard", "commit": "abc123"}


def test_deployed_version_falls_back_to_packed_refs(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git" / "refs" / "heads" / "quant-trading-dashboard").unlink()
    (repo / ".git" / "packed-refs").write_text(
        "def456 refs/heads/quant-trading-dashboard\n")
    assert deployed_version(repo)["commit"] == "def456"


def test_deployed_version_missing_git_is_none_fields(tmp_path):
    assert deployed_version(tmp_path) == {"branch": None, "commit": None}


def test_build_ops_health_aggregates_all_sections(tmp_path):
    h = build_ops_health(_repo(tmp_path))
    assert h["schema_version"] == 1
    assert h["version"]["commit"] == "abc123"
    assert h["factor_freshness"] == [{
        "symbol": "eth",
        "generated_at": "2026-07-04T00:00:00+00:00",
        "index_end": "2026-07-03T23:00:00+00:00",
    }]
    assert h["jobs"] == {"queued": 0, "running": 0, "succeeded": 1,
                         "failed": 0, "canceled": 0}
    assert h["traders"] == [{
        "testnet_id": "eth_s5_paper", "desired_state": "running",
        "status": "running", "updated_at": "2026-07-04T09:00:00+00:00",
        "equity": 10000.0, "trades": 0,
    }]


def test_build_ops_health_empty_repo_does_not_crash(tmp_path):
    h = build_ops_health(tmp_path)
    assert h["factor_freshness"] == [] and h["traders"] == []
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `cd dashboard/server && python -m pytest test_ops_health.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops_health'`。

- [ ] **Step 3: 實作 `dashboard/server/ops_health.py`**

```python
"""One all-UTC operational snapshot: deploy version, factor freshness,
job-queue counts, trader heartbeats.

Every timestamp passes through verbatim from artifacts that are already
UTC ISO; nothing here reads server-local time except generated_at (UTC).
Read-only — safe to call from a request handler.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_META_RE = re.compile(r"^factor_values_([a-z0-9]+)\.meta\.json$")
_JOB_STATUSES = ("queued", "running", "succeeded", "failed", "canceled")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def deployed_version(repo_root: "str | Path") -> dict:
    """{branch, commit} from .git files directly (no git binary needed).
    Deploys are `git pull` on the server, so HEAD == deployed version."""
    repo_root = Path(repo_root)
    try:
        head = (repo_root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {"branch": None, "commit": None}
    if not head.startswith("ref: "):
        return {"branch": None, "commit": head or None}  # detached HEAD
    ref = head[5:].strip()
    branch = ref.rsplit("/", 1)[-1]
    loose = repo_root / ".git" / ref
    if loose.exists():
        try:
            return {"branch": branch, "commit": loose.read_text(encoding="utf-8").strip()}
        except OSError:
            pass
    packed = repo_root / ".git" / "packed-refs"
    if packed.exists():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1] == ref:
                    return {"branch": branch, "commit": parts[0]}
        except OSError:
            pass
    return {"branch": branch, "commit": None}


def build_ops_health(repo_root: "str | Path") -> dict:
    repo_root = Path(repo_root)

    freshness = []
    manifests = repo_root / "research" / "manifests"
    if manifests.is_dir():
        for p in sorted(manifests.iterdir()):
            m = _META_RE.match(p.name)
            if not m:
                continue
            meta = _read_json(p) or {}
            freshness.append({
                "symbol": m.group(1),
                "generated_at": meta.get("generated_at"),
                "index_end": meta.get("index_end"),
            })

    jobs = {s: 0 for s in _JOB_STATUSES}
    jobs_dir = repo_root / "runs" / "pipeline_jobs"
    if jobs_dir.is_dir():
        for jp in jobs_dir.glob("*/job.json"):
            status = (_read_json(jp) or {}).get("status")
            if status in jobs:
                jobs[status] += 1

    traders = []
    testnet = repo_root / "runs" / "testnet"
    if testnet.is_dir():
        for d in sorted(p for p in testnet.iterdir() if p.is_dir()):
            ctrl = _read_json(d / "control.json") or {}
            live = (_read_json(d / "testnet_status.json") or {}).get("live") or {}
            traders.append({
                "testnet_id": d.name,
                "desired_state": ctrl.get("desired_state"),
                "status": live.get("status"),
                "updated_at": live.get("updated_at"),
                "equity": live.get("equity"),
                "trades": live.get("trades"),
            })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "version": deployed_version(repo_root),
        "factor_freshness": freshness,
        "jobs": jobs,
        "traders": traders,
    }
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `cd dashboard/server && python -m pytest test_ops_health.py -q`
Expected: 5 passed。

- [ ] **Step 5: main.py 加 route**

開 `dashboard/server/main.py`：找到 FastAPI `app` 與既有 endpoints 解析 repo root 的方式（搜 `REPO_ROOT` 或 `repo_root`，其他 route 讀 runs/ 用的那個變數/函式），沿用同一來源加：

```python
from ops_health import build_ops_health


@app.get("/api/ops/health")
def api_ops_health():
    return build_ops_health(<沿用檔內既有的 repo-root Path>)
```

若 main.py 沒有現成 module-level repo root，用檔內其他 route 的解析函式；兩者皆無才 fallback `Path(os.environ.get("REPO_ROOT", "/repo"))`。

- [ ] **Step 6: 跑全套 server scope + Commit**

Run: `cd dashboard/server && python -m pytest -q` → 全綠。

```bash
git add dashboard/server/ops_health.py dashboard/server/test_ops_health.py dashboard/server/main.py
git commit -m "feat(server): /api/ops/health — version beacon + freshness + jobs + traders"
```

---

## Self-check

- [ ] Task 1 Step 3 前已對照 `research/emit_manifest.py` 確認 manifest 欄位名，出入處已同步修測試與代碼。
- [ ] 兩個 scope 分開跑、都綠。
- [ ] 所有輸出時間戳 UTC；沒有讀 server 本地時間。
- [ ] 全部唯讀彙總，沒改任何既有 artifact 的格式或寫入者。
