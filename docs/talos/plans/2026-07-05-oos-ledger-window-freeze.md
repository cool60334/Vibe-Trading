# OOS 研究記帳 + 窗口凍結 + 終極 holdout（Part A A2+A3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 三件互相咬合的效度防線：① `window_end` 凍結回測窗口（A/B 對比同窗，消滅「今天跑和下週跑窗口不同」的噪音）；② `final_holdout_start` 之後的資料任何 pipeline 窗口都碰不到，只有一支手動 CLI 在 promote 前跑**一次**；③ append-only 研究記帳 ledger — 從 stage0a 篩了幾個因子、stage4 掃了幾組、OOS 被評估第幾次，全部留痕，並在 manifest/gate 上透明標示（**標示不懲罰** — sequential peeking 無法用 DSR 數學救，只有真沒看過的資料救得了）。

**Architecture:** 單一錨定函式 `resolve_anchor_date(cfg, today)` 取代散落的 `date.today()` —— `window_end` 與 `final_holdout_start` 上限都在這一個點生效，所有窗口 helper（build_run_config / train_window / oos_window / CPCV hist_end）自動繼承。Ledger 為 JSONL append-only、fail-soft（記帳失敗絕不弄掛 stage）。兩個新 config 欄位**皆可選、預設 None = 行為與現行完全相同**（伺服器不改 config 零影響）。

**Tech Stack:** Python 標準庫、pytest、Pydantic（schemas 加一個 optional 欄位）。

**背景（給零 context 的執行者）：**
- 本 repo 兩套系統：`agent/`（上游，**不碰**）與 `research/` + `dashboard/`（本計畫範圍）。
- 測試 scope 兩個都會用到，**分開跑**：research 從 repo 根 `python -m pytest research/tests/ -q`；dashboard `cd dashboard/server && python -m pytest -q`。
- 關鍵現況（先讀這些檔再動手）：
  - `research/pipeline/stage3_backtest.py`：`build_run_config()`（`start = today - period`，`end = oos_start or today`）、`train_window()`、`oos_window()` —— 三處都有 `if today is None: today = date.today()` 型邏輯，stage4/stress/lag/CPCV 全靠它們。
  - `research/pipeline/config.py`：`ResearchConfig` dataclass + `load_config()`；`oos_start` 是現成的 optional str 欄位 —— 新欄位**照抄它的宣告與解析方式**。
  - `research/pipeline/stage4_optimize.py`：`_optimize_strategy()` 寫 `optimization.json` 後有 walk-forward holdout 區塊（搜 `oos_holdout`）；`base_spec` 是已載入的 YAML dict（`indicators` keys 可取因子清單）。
  - `research/pipeline/stage4_cpcv.py`：`hist_end = date.today().isoformat() if cfg.oos_start else ...`（搜 `date.today`）。
  - `research/pipeline/stage0a_features.py`：`_process_symbol()` 末段 `dump_evidence(...)` 之後是掛記帳 hook 的位置。
  - `research/emit_manifest.py`：manifest 組裝 + `compute_gate()`（回傳含 `red_flags` list 的 GateBlock）。
  - `dashboard/server/schemas.py`：`StrategyManifest` Pydantic model。
  - 測試建 `ResearchConfig` 的現成範式：在 `research/tests/` 裡搜 `_make_research_config`，照抄。
- **檔案所有權**：`research/lib/research_ledger.py`（新）、`research/pipeline/final_holdout.py`（新）、`research/pipeline/config.py`、`research/pipeline/stage3_backtest.py`、`research/pipeline/stage4_optimize.py`、`research/pipeline/stage4_cpcv.py`、`research/pipeline/stage0a_features.py`、`research/emit_manifest.py`、`dashboard/server/schemas.py`（僅加 optional 欄位）、新測試 `research/tests/test_window_freeze.py`、`research/tests/test_research_ledger.py`、`research/tests/test_final_holdout.py`。其他檔不准動。
- **鐵律**：ledger 記帳一律 fail-soft；`final_holdout.py` 只能手動跑，**不准**接進任何自動 chain；兩個新 config 欄位不設時所有既有測試必須原樣全綠。

---

### Task 1: `resolve_anchor_date()` — 窗口凍結 + holdout 上限（A3 + A2 的地基）

**Files:**
- Modify: `research/pipeline/config.py`（加 `window_end`、`final_holdout_start` 兩個 optional 欄位，照 `oos_start` 的樣式宣告+解析）
- Modify: `research/pipeline/stage3_backtest.py`
- Create: `research/tests/test_window_freeze.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""Window freeze + final-holdout cap.

start = today - period drifts with the run date, so cross-run comparisons
mix signal with window noise; and nothing stops a window from touching the
final holdout. resolve_anchor_date() is the single point fixing both.
"""
import sys
from datetime import date
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from pipeline.stage3_backtest import (  # noqa: E402
    build_run_config,
    oos_window,
    resolve_anchor_date,
    train_window,
)

# 建 cfg 的方式：照抄 research/tests/ 既有的 _make_research_config helper（搜尋即得），
# 多帶 window_end / final_holdout_start 兩個 kwargs。下面以 _cfg(...) 代稱。
from tests_helpers_or_local_copy import _cfg  # ← 執行時替換成實際 helper 的 import/複製


def test_anchor_defaults_to_today_when_nothing_set():
    cfg = _cfg(window_end=None, final_holdout_start=None)
    assert resolve_anchor_date(cfg) == date.today()


def test_anchor_uses_frozen_window_end():
    cfg = _cfg(window_end="2026-06-01")
    assert resolve_anchor_date(cfg) == date(2026, 6, 1)


def test_explicit_today_arg_beats_window_end():
    cfg = _cfg(window_end="2026-06-01")
    assert resolve_anchor_date(cfg, today=date(2026, 3, 1)) == date(2026, 3, 1)


def test_final_holdout_caps_anchor_always():
    cfg = _cfg(window_end="2026-06-01", final_holdout_start="2026-04-01")
    # frozen anchor 在 holdout 之後 → 被壓回 holdout 前一天
    assert resolve_anchor_date(cfg) == date(2026, 3, 31)
    # 顯式 today 也一樣被壓（holdout 是鐵律，沒有 override）
    assert resolve_anchor_date(cfg, today=date(2026, 7, 1)) == date(2026, 3, 31)
    # 早於 holdout 的顯式 today 不受影響
    assert resolve_anchor_date(cfg, today=date(2026, 1, 1)) == date(2026, 1, 1)


def test_build_run_config_is_date_stable_under_freeze():
    cfg = _cfg(window_end="2026-06-01", oos_start="2025-01-01", period=1460)
    a = build_run_config("ETH-USDT-SWAP", cfg)
    b = build_run_config("ETH-USDT-SWAP", cfg)  # 「隔天再跑」也一樣
    assert a == b
    assert a["end_date"] == "2025-01-01"          # oos_start 截斷 train（不變的舊行為）
    assert a["start_date"] == "2022-06-02"        # 2026-06-01 - 1460d


def test_oos_window_ends_at_frozen_anchor():
    cfg = _cfg(window_end="2026-06-01", oos_start="2025-01-01")
    assert oos_window(cfg) == ("2025-01-01", "2026-06-01")


def test_oos_window_never_touches_final_holdout():
    cfg = _cfg(oos_start="2025-01-01", final_holdout_start="2026-04-01")
    assert oos_window(cfg) == ("2025-01-01", "2026-03-31")


def test_legacy_behaviour_unchanged_when_fields_unset():
    cfg = _cfg(oos_start="2025-01-01")
    t = date(2026, 7, 5)
    assert train_window(cfg, today=t) == ((t.fromordinal(t.toordinal() - cfg.period)).isoformat(), "2025-01-01")
    assert oos_window(cfg, today=t) == ("2025-01-01", "2026-07-05")
```

（`_cfg` 匯入那行是**建構待辦**：先搜 `research/tests/` 的 `_make_research_config`，把它 import 或複製到本檔改名 `_cfg` 並支援新欄位。這是 fixture 接線，不是行為設計。）

- [ ] **Step 2: 跑測試確認 RED**

Run（repo 根）: `python -m pytest research/tests/test_window_freeze.py -q`
Expected: `ImportError: cannot import name 'resolve_anchor_date'`（fixture 接好後）。

- [ ] **Step 3: 實作**

3a. `config.py`：`ResearchConfig` 加兩個欄位（宣告位置、YAML 解析、預設值全部照 `oos_start` 現有寫法複製改名）：

```python
    window_end: "str | None" = None          # 凍結窗口錨定日；None = 用 date.today()（legacy）
    final_holdout_start: "str | None" = None # 終極 holdout 起日；設定後任何 pipeline 窗口都切在它之前
```

3b. `stage3_backtest.py` 加（放在 `train_window` 上方）：

```python
def resolve_anchor_date(cfg: ResearchConfig, today: "date | None" = None) -> date:
    """Effective 'today' for ALL window math — the single freeze point.

    Priority: explicit `today` (tests/backfills) > cfg.window_end (frozen
    anchor so cross-run comparisons share one window) > date.today().
    cfg.final_holdout_start, when set, caps the result at holdout_start - 1
    day UNCONDITIONALLY: no pipeline window may ever touch the final
    holdout — that data is spent only once, by final_holdout.py, right
    before a promote decision.
    """
    if today is None:
        today = date.fromisoformat(cfg.window_end) if cfg.window_end else date.today()
    if cfg.final_holdout_start:
        cap = date.fromisoformat(cfg.final_holdout_start) - timedelta(days=1)
        if today > cap:
            today = cap
    return today
```

3c. `build_run_config` / `train_window` / `oos_window` 三個函式裡的
`if today is None: today = date.today()` 全部改成一行 `today = resolve_anchor_date(cfg, today)`。

3d. `stage4_cpcv.py`：`hist_end = date.today().isoformat() if cfg.oos_start else base_cfg["end_date"]` 改為

```python
    from pipeline.stage3_backtest import resolve_anchor_date
    hist_end = resolve_anchor_date(cfg).isoformat() if cfg.oos_start else base_cfg["end_date"]
```

- [ ] **Step 4: 跑測試確認 GREEN + 全套 research 迴歸**

Run: `python -m pytest research/tests/test_window_freeze.py -q` → 全 passed。
Run: `python -m pytest research/tests/ -q` → 除既知的 `test_eth_s5_dsl_equiv`（若尚未修復）外全綠 —— 新欄位未設時行為必須零變化。

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/config.py research/pipeline/stage3_backtest.py research/pipeline/stage4_cpcv.py research/tests/test_window_freeze.py
git commit -m "feat(windows): resolve_anchor_date — window_end freeze + final-holdout cap"
```

### Task 2: `research_ledger` — 全漏斗記帳（append-only JSONL）

**Files:**
- Create: `research/lib/research_ledger.py`
- Create: `research/tests/test_research_ledger.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""Append-only research ledger — makes multiple testing & OOS peeking countable."""
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from lib.research_ledger import (  # noqa: E402
    append_event,
    count_events,
    read_events,
    research_accounting_block,
)


def test_append_and_read_roundtrip(tmp_path):
    append_event(tmp_path, kind="sweep", symbol="eth",
                 strategy_id="eth_s5", detail={"n_trials": 60})
    append_event(tmp_path, kind="oos_eval", symbol="eth", strategy_id="eth_s5")
    events = read_events(tmp_path)
    assert len(events) == 2
    assert events[0]["kind"] == "sweep"
    assert events[0]["detail"]["n_trials"] == 60
    assert events[1]["ts"].endswith("+00:00")  # all UTC


def test_count_filters_by_kind_and_symbol(tmp_path):
    for _ in range(3):
        append_event(tmp_path, kind="oos_eval", symbol="eth")
    append_event(tmp_path, kind="oos_eval", symbol="sol")
    append_event(tmp_path, kind="sweep", symbol="eth")
    assert count_events(tmp_path, kind="oos_eval", symbol="eth") == 3
    assert count_events(tmp_path, kind="oos_eval") == 4
    assert count_events(tmp_path) == 5


def test_read_tolerates_corrupt_lines(tmp_path):
    append_event(tmp_path, kind="sweep", symbol="eth")
    with (tmp_path / "research_ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json}\n")
    append_event(tmp_path, kind="sweep", symbol="eth")
    assert len(read_events(tmp_path)) == 2  # corrupt line skipped, not fatal


def test_append_never_raises_on_unwritable_dir(tmp_path):
    append_event(tmp_path / "no_such_subdir_without_parents" / "x", kind="sweep", symbol="eth")
    # fail-soft：不噴例外即通過


def test_accounting_block_shape(tmp_path):
    append_event(tmp_path, kind="factor_screen", symbol="eth", detail={"n_features": 27})
    append_event(tmp_path, kind="sweep", symbol="eth")
    append_event(tmp_path, kind="oos_eval", symbol="eth")
    append_event(tmp_path, kind="oos_eval", symbol="eth")
    blk = research_accounting_block(tmp_path, "eth")
    assert blk == {
        "factor_screens_symbol": 1,
        "sweeps_symbol": 1,
        "oos_evals_symbol": 2,
        "final_holdout_evals_symbol": 0,
    }
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `python -m pytest research/tests/test_research_ledger.py -q`
Expected: `ModuleNotFoundError: No module named 'lib.research_ledger'`。

- [ ] **Step 3: 實作 `research/lib/research_ledger.py`**

```python
"""Append-only research ledger (JSONL) — the funnel's flight recorder.

DSR only penalises trials INSIDE one stage-4 sweep. Everything upstream —
how many factors stage 0a screened, how many sweeps ran, how many times the
same OOS window has been evaluated for a symbol — went uncounted, which is
exactly how a holdout degrades into a second train set. This module makes
those counts durable so gates and humans can SEE the multiple-testing debt.

Transparency, not punishment: counts are surfaced on manifests/gates as
red flags; they never hard-block by themselves. Writes are fail-soft — a
ledger hiccup must never fail a pipeline stage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LEDGER_NAME = "research_ledger.jsonl"


def _ledger_path(manifests_dir: "str | Path") -> Path:
    return Path(manifests_dir) / LEDGER_NAME


def append_event(
    manifests_dir: "str | Path",
    *,
    kind: str,
    symbol: str,
    strategy_id: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    """Append one event. Fail-soft: any OSError is printed, never raised."""
    event = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "kind": kind,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "detail": detail or {},
    }
    try:
        with _ledger_path(manifests_dir).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:  # ledger must never sink a stage
        print(f"[ledger] WARN: append failed: {exc}")


def read_events(manifests_dir: "str | Path") -> list:
    """All parseable events, oldest first. Corrupt lines are skipped."""
    path = _ledger_path(manifests_dir)
    if not path.exists():
        return []
    out: list = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def count_events(
    manifests_dir: "str | Path",
    kind: Optional[str] = None,
    symbol: Optional[str] = None,
) -> int:
    return sum(
        1
        for e in read_events(manifests_dir)
        if (kind is None or e.get("kind") == kind)
        and (symbol is None or e.get("symbol") == symbol)
    )


def research_accounting_block(manifests_dir: "str | Path", symbol: str) -> dict:
    """Per-symbol funnel counters for embedding into a strategy manifest."""
    return {
        "factor_screens_symbol": count_events(manifests_dir, "factor_screen", symbol),
        "sweeps_symbol": count_events(manifests_dir, "sweep", symbol),
        "oos_evals_symbol": count_events(manifests_dir, "oos_eval", symbol),
        "final_holdout_evals_symbol": count_events(manifests_dir, "final_holdout", symbol),
    }
```

- [ ] **Step 4: GREEN + Commit**

Run: `python -m pytest research/tests/test_research_ledger.py -q` → 全 passed。

```bash
git add research/lib/research_ledger.py research/tests/test_research_ledger.py
git commit -m "feat(ledger): append-only research ledger — funnel-wide trial accounting"
```

### Task 3: 記帳 hooks（stage0a / stage4）+ manifest 透明標示

**Files:**
- Modify: `research/pipeline/stage0a_features.py`
- Modify: `research/pipeline/stage4_optimize.py`
- Modify: `research/emit_manifest.py`
- Modify: `dashboard/server/schemas.py`

- [ ] **Step 1: hooks（行為由 Task 2 測試保障，接線以現有 stage 測試迴歸把關）**

3a. `stage0a_features.py` `_process_symbol()`：`dump_evidence(...)` 成功後加：

```python
        # ── Research ledger: record screening breadth (fail-soft) ────────────
        from lib.research_ledger import append_event
        append_event(manifests_dir, kind="factor_screen", symbol=sym,
                     detail={"n_features": len(feature_dict)})
```

3b. `stage4_optimize.py` `_optimize_strategy()`：
- 寫完 `optimization.json`（`print(f"  [OK] wrote {out_path}")` 之後）加：

```python
    from lib.research_ledger import append_event
    append_event(manifests_dir, kind="sweep", symbol=symbol_to_short(entry.symbol),
                 strategy_id=strategy_id,
                 detail={"n_trials": n_trials,
                         "factors": sorted((base_spec.get("indicators") or {}).keys()),
                         "best_sharpe": best.sharpe if best is not None else None})
```

- walk-forward holdout 成功（`update_walk_forward_runs(...)` 那行之後）加：

```python
                    append_event(manifests_dir, kind="oos_eval",
                                 symbol=symbol_to_short(entry.symbol),
                                 strategy_id=strategy_id,
                                 detail={"window": list(oos_win),
                                         "sharpe": float(m.get("sharpe", 0) or 0)})
```

3c. `dashboard/server/schemas.py`：`StrategyManifest` 加 optional 欄位（放其他 optional 欄位旁）：

```python
    research_accounting: Optional[dict] = None  # per-symbol funnel counters (ledger)
```

3d. `research/emit_manifest.py`：manifest dict/model 組裝處（找到填 `gate` 的位置）加：

```python
    from lib.research_ledger import research_accounting_block
    accounting = research_accounting_block(manifests_dir, short)
    manifest_kwargs["research_accounting"] = accounting  # ← 依實際組裝方式接（dict 或 model kwargs）
```

且 gate 建好後、寫出前，加透明 red flag（**informational，不設 fatal**）：

```python
    _oos_n = accounting.get("oos_evals_symbol", 0)
    if _oos_n >= 4:
        gate.red_flags.append(
            f"oos_window_evaluated_{_oos_n}x_for_{short} — holdout losing freshness; "
            "run final_holdout.py before promote"
        )
```

（`manifests_dir`、`short`、gate 物件的實際變數名以 emit_manifest.py 檔內為準 —— 先讀該檔的 manifest 組裝函式再接線。）

- [ ] **Step 2: 迴歸 + 雙 scope 測試**

Run: `python -m pytest research/tests/ -q` → 全綠（hooks 全 fail-soft，不得改變任何既有測試結果）。
Run: `cd dashboard/server && python -m pytest -q` → 全綠（optional 欄位向後相容）。

- [ ] **Step 3: Commit**

```bash
git add research/pipeline/stage0a_features.py research/pipeline/stage4_optimize.py research/emit_manifest.py dashboard/server/schemas.py
git commit -m "feat(ledger): stage0a/stage4 hooks + manifest accounting + oos-peek red flag"
```

### Task 4: `final_holdout.py` — 一次性終極驗證 CLI（手動、絕不進自動 chain）

**Files:**
- Create: `research/pipeline/final_holdout.py`
- Create: `research/tests/test_final_holdout.py`

- [ ] **Step 1: 寫失敗測試（純邏輯部分）**

```python
"""final_holdout — one-shot validation on data no pipeline window ever saw."""
import sys
from datetime import date
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from lib.research_ledger import append_event  # noqa: E402
from pipeline.final_holdout import holdout_window, prior_holdout_evals  # noqa: E402


def test_holdout_window_spans_holdout_start_to_today():
    win = holdout_window("2026-04-01", today=date(2026, 7, 5))
    assert win == ("2026-04-01", "2026-07-05")


def test_prior_evals_counts_only_this_strategy(tmp_path):
    append_event(tmp_path, kind="final_holdout", symbol="eth", strategy_id="eth_s5")
    append_event(tmp_path, kind="final_holdout", symbol="eth", strategy_id="eth_s9")
    append_event(tmp_path, kind="oos_eval", symbol="eth", strategy_id="eth_s5")
    assert prior_holdout_evals(tmp_path, "eth_s5") == 1
    assert prior_holdout_evals(tmp_path, "nope") == 0
```

- [ ] **Step 2: RED**

Run: `python -m pytest research/tests/test_final_holdout.py -q`
Expected: `ModuleNotFoundError: No module named 'pipeline.final_holdout'`。

- [ ] **Step 3: 實作 `research/pipeline/final_holdout.py`**

```python
"""One-shot FINAL-HOLDOUT validation — run manually, once, before a promote.

The final holdout ([cfg.final_holdout_start, today]) is data no pipeline
window is allowed to touch (resolve_anchor_date caps every window before
it). This CLI spends that data — deliberately, loudly, and countably:

    python -m research.pipeline.final_holdout --strategy <id>

It backtests the strategy's stage-4 best params on the holdout window,
writes manifests/<id>/final_holdout.json, and records a ledger event.
Running it AGAIN for the same strategy prints a loud re-peek warning:
a second look at the holdout is already selection bias.

NEVER wire this into the automatic pipeline chain.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_THIS = Path(__file__).resolve()
_RESEARCH = _THIS.parent.parent
for _p in (str(_RESEARCH), str(_RESEARCH.parent / "dashboard" / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml as _yaml  # noqa: E402

from pipeline.config import _REPO_ROOT, load_config  # noqa: E402
from pipeline.stage3_backtest import build_run_config, symbol_to_short  # noqa: E402
from pipeline.stage4_optimize import (  # noqa: E402
    _compile_signal_code,
    _invoke_backtest,
    _read_metrics,
    _scaffold_combo_run,
    apply_overrides_to_spec,
)
from pipeline.strategy_runs import load_strategy_runs  # noqa: E402
from lib.research_ledger import append_event, read_events  # noqa: E402


def holdout_window(final_holdout_start: str, today: "date | None" = None) -> tuple:
    if today is None:
        today = date.today()
    return (final_holdout_start, today.isoformat())


def prior_holdout_evals(manifests_dir, strategy_id: str) -> int:
    return sum(
        1 for e in read_events(manifests_dir)
        if e.get("kind") == "final_holdout" and e.get("strategy_id") == strategy_id
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="One-shot final-holdout validation")
    ap.add_argument("--strategy", required=True)
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.final_holdout_start:
        print("[final-holdout] cfg.final_holdout_start is not set — nothing reserved. Abort.")
        sys.exit(2)

    from lib.timeframe import active_manifests_dir
    manifests_dir = active_manifests_dir()
    runs_map = load_strategy_runs()
    if args.strategy not in runs_map.entries:
        print(f"[final-holdout] unknown strategy {args.strategy!r}")
        sys.exit(1)
    entry = runs_map.entries[args.strategy]

    n_prior = prior_holdout_evals(manifests_dir, args.strategy)
    if n_prior > 0:
        print("=" * 60)
        print(f"[final-holdout] ⚠️  RE-PEEK WARNING: this strategy already evaluated "
              f"the final holdout {n_prior}x. A repeat look is selection bias — "
              "the result below is NOT a fresh out-of-sample verdict.")
        print("=" * 60)

    opt_path = manifests_dir / args.strategy / "optimization.json"
    best_params: dict = {}
    if opt_path.exists():
        try:
            best_params = json.loads(opt_path.read_text(encoding="utf-8")).get("best_params") or {}
        except (OSError, json.JSONDecodeError):
            pass

    spec_path = _REPO_ROOT / entry.spec_yaml
    base_spec = _yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec_dict = apply_overrides_to_spec(base_spec, best_params) if best_params else base_spec
    code = _compile_signal_code(spec_dict, cfg.interval)

    win = holdout_window(cfg.final_holdout_start)
    run_cfg = build_run_config(entry.symbol, cfg)
    run_cfg["start_date"], run_cfg["end_date"] = win

    run_name = f"{args.strategy}_final_holdout"
    run_dir = _REPO_ROOT / "runs" / run_name
    _scaffold_combo_run(run_dir, run_cfg, code)
    proc = _invoke_backtest(run_dir)
    if proc.returncode != 0:
        print(f"[final-holdout] backtest failed (exit {proc.returncode}): "
              f"{(proc.stderr or '')[:300]}")
        sys.exit(1)
    m = _read_metrics(run_dir) or {}

    payload = {
        "strategy_id": args.strategy,
        "window": list(win),
        "sharpe": float(m.get("sharpe", 0) or 0),
        "max_drawdown": float(m.get("max_drawdown", 0) or 0),
        "trade_count": int(float(m.get("trade_count", 0) or 0)),
        "best_params": best_params,
        "prior_evals": n_prior,
    }
    out_dir = manifests_dir / args.strategy
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "final_holdout.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    append_event(manifests_dir, kind="final_holdout",
                 symbol=symbol_to_short(entry.symbol), strategy_id=args.strategy,
                 detail={"window": list(win), "sharpe": payload["sharpe"]})

    print(f"[final-holdout] {args.strategy}: sharpe={payload['sharpe']:+.3f} "
          f"dd={payload['max_drawdown']:.3f} trades={payload['trade_count']} "
          f"({win[0]}..{win[1]}) -> final_holdout.json")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

（注意：`_compile_signal_code(spec_dict, cfg.interval)` 的簽名以 A4 merge 後的 `stage4_optimize.py` 為準 —— 先確認再呼叫。）

- [ ] **Step 4: GREEN + 全套雙 scope**

Run: `python -m pytest research/tests/test_final_holdout.py -q` → passed。
Run: `python -m pytest research/tests/ -q` 與 `cd dashboard/server && python -m pytest -q` → 全綠。

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/final_holdout.py research/tests/test_final_holdout.py
git commit -m "feat(holdout): one-shot final-holdout CLI with re-peek warning"
```

### Task 5: 文件補一段（PIPELINE.md 不在所有權清單 → 不動；改 config 註解）

- [ ] `research/research_config.yaml` 加註解區塊（不啟用，只示範）：

```yaml
# ─── 窗口凍結與終極 holdout（A2+A3，可選） ─────────────────────────────────
# window_end: "2026-06-01"          # 凍結回測窗口錨定日：一個迭代週期內所有 run 同窗，
#                                   # 週期結束人工滾動。未設 = 用執行當日（legacy）。
# final_holdout_start: "2026-04-01" # 此日之後的資料任何 pipeline 窗口都碰不到；
#                                   # 只能由 python -m research.pipeline.final_holdout
#                                   # 在 promote 前手動花費一次。
```

- [ ] Commit：`git add research/research_config.yaml && git commit -m "docs(config): document window_end / final_holdout_start knobs"`

---

## Self-check（執行者完成前過一遍）

- [ ] 兩個新 config 欄位未設時，全部既有測試原樣全綠（零行為變化）。
- [ ] `resolve_anchor_date` 的 holdout 上限對**顯式 today 也生效**（鐵律無 override，有測試釘住）。
- [ ] ledger 所有寫入 fail-soft；stage0a/stage4 在 ledger 目錄不可寫時照常成功。
- [ ] `final_holdout.py` 沒被任何自動 chain（pipeline_jobs、stage 模組）import 或呼叫。
- [ ] red flag 為 informational（不設 fatal、不擋 promote —— 擋的是人看到後的判斷）。
- [ ] manifest 的 `research_accounting` 為 optional，舊 manifest 讀取不炸。
