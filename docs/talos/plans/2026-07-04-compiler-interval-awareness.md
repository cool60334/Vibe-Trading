# Signal compiler interval 感知 + stage4 manifests dir（Part A A4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拆兩顆 sub-hour 地雷：① `signal_compiler` 把「N 天窗」寫死成 `N*24` 根 K、把 `max_hold_hours` 直接當根數 —— 30m 級別下 90 天窗變 45 天、持有 24h 變 12h，**默默算錯**；② `stage4_optimize` / `stage4_cpcv` 用寫死的 `research/manifests`，30m 跑會交叉污染 1H 產物。**硬約束：1H 輸出必須 byte-identical**（現行策略零重驗）。

**Architecture:** `compile_strategy()` 增 `interval="1H"` 參數，經 `research/lib/timeframe.py` 的 `bars_per_day`/`bars_per_hour` 換算，渲染字串保持 `N*<bpd>` 形式（1H 時 `<bpd>`=24 → 輸出與現行完全相同）。呼叫端（stage2b / stage3 lag-stress / stage4 / stage4_cpcv）傳 `cfg.interval`。stage4 兩支的 manifests dir 改 `active_manifests_dir()`（與其他 stage 一致）。

**Tech Stack:** Python、Jinja2（模板本身不用改 —— 數字在 renderer 產生）、pytest。

**背景（給零 context 的執行者）：**
- `research/lib/signal_compiler.py`（~460 行）：`_render_condition()` 產 `rolling({n}*24, min_periods={n}*24//2)`（percentile 與 zscore 兩處）；`_render_exit_state_machine()` 的 signal_invalidation hoist 同樣 `*24`，time_based 產 `bars_held >= {max_hold_hours}`。
- `research/lib/timeframe.py`：`bars_per_hour(interval)`（{15m:4, 30m:2, 1H:1}）、`bars_per_day(interval)`、`active_manifests_dir()`（RESEARCH_INTERVAL env → `research/manifests[/<interval>]`）。
- 呼叫端：`stage2b_compile_signal.py`、`stage3_backtest.py`（僅 `_run_lag_stress_for_strategy` 內一處 `compile_strategy(spec, lag_bars=lag)`）、`stage4_optimize.py`（`_compile_signal_code(spec_dict)`，被本檔與 `stage4_cpcv.py` 使用）。每個呼叫端都有 `cfg = load_config()`，`cfg.interval` 現成。
- 測試 scope：**從 repo 根** `python -m pytest research/tests/ -q`。既有 `test_signal_compiler.py` 斷言 1H 輸出（含 `*24` 字串）—— 它們必須**原封不動**繼續綠，這就是 byte-identity 的迴歸鎖。
- **檔案所有權**：`research/lib/signal_compiler.py`、`research/pipeline/stage2b_compile_signal.py`、`research/pipeline/stage3_backtest.py`（僅 lag-stress 一行）、`research/pipeline/stage4_optimize.py`、`research/pipeline/stage4_cpcv.py`、`research/tests/test_signal_compiler_interval.py`（新）。
- **先讀** `research/tests/test_signal_compiler.py` 開頭，抄它建 `StrategySpec` 測試 fixture 的方式（schemas import 路徑等）。

---

### Task 1: compiler 接 interval（renderer 層）

**Files:**
- Create: `research/tests/test_signal_compiler_interval.py`
- Modify: `research/lib/signal_compiler.py`

- [ ] **Step 1: 寫失敗測試**

（`_make_spec()` 的具體寫法參照既有 `test_signal_compiler.py` 的 fixture —— 用同一套 import bootstrap 與 StrategySpec 建構；下方骨架的 spec 欄位齊全可直接用）

```python
"""Interval-awareness of the signal compiler.

The DSL says `percentile_90d` = 90 DAYS; the compiler used to hard-code
90*24 BARS, silently shrinking every window at sub-hour intervals. At 1H
the output must stay byte-identical to the legacy renderer.
"""
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
_REPO = _RESEARCH.parent
for p in (str(_RESEARCH), str(_REPO / "dashboard" / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas import StrategySpec  # noqa: E402
from lib.signal_compiler import compile_strategy  # noqa: E402


def _make_spec() -> StrategySpec:
    return StrategySpec.model_validate({
        "name": "iv_test",
        "archetype": "single_factor",
        "hypothesis": "interval test",
        "symbol": "ETH-USDT-SWAP",
        "timeframe_signal": "8h",
        "hold_period": {"min_hours": 24, "max_hours": 168},
        "indicators": {
            "funding_z": {"source": "stage1:funding_z", "smoothing": "sma_3"},
        },
        "entry_long": {
            "description": "d", "logic": "all",
            "conditions": ["funding_z_percentile_90d <= 15 persist 2/3"],
        },
        "entry_short": {
            "description": "d", "logic": "all",
            "conditions": ["funding_z_percentile_90d >= 80 persist 2/3"],
        },
        "exit_rules": [
            {"condition": "time_based", "max_hold_hours": 168},
            {"condition": "signal_invalidation",
             "expression": "funding_z_percentile_90d between 40,60"},
        ],
        "position_sizing": {"method": "fixed_risk",
                            "risk_per_trade_pct": 1.5, "leverage": 1.0},
        "parameter_search_ranges": {},
    })


def test_default_interval_matches_explicit_1h_byte_identical():
    spec = _make_spec()
    assert compile_strategy(spec) == compile_strategy(spec, interval="1H")


def test_1h_renders_legacy_24_bars_per_day():
    src = compile_strategy(_make_spec(), interval="1H")
    assert "rolling(90*24, min_periods=90*24//2)" in src
    assert "bars_held >= 168" in src


def test_30m_scales_windows_and_hold():
    src = compile_strategy(_make_spec(), interval="30m")
    assert "rolling(90*48, min_periods=90*48//2)" in src   # 48 bars/day
    assert "90*24" not in src
    assert "bars_held >= 336" in src                        # 168h * 2 bars/h


def test_15m_scales_windows_and_hold():
    src = compile_strategy(_make_spec(), interval="15m")
    assert "rolling(90*96, min_periods=90*96//2)" in src
    assert "bars_held >= 672" in src


def test_invalidation_hoist_scales_too():
    src = compile_strategy(_make_spec(), interval="30m")
    assert "_inv_pct_1 = funding_z.rolling(90*48" in src or "rolling(90*48" in src
```

- [ ] **Step 2: 跑測試確認 RED**

Run（repo 根）: `python -m pytest research/tests/test_signal_compiler_interval.py -q`
Expected: `compile_strategy() got an unexpected keyword argument 'interval'` 或 30m 斷言 FAIL。
（若 `_make_spec` 因 schema 必填欄位報錯，比照既有 `test_signal_compiler.py` 的 spec dict 補齊欄位 —— 這是 fixture 修正，不是行為修正。）

- [ ] **Step 3: 實作 renderer 層**

`signal_compiler.py`：

3a. import 區加：

```python
from lib.timeframe import bars_per_day, bars_per_hour  # noqa: E402
```

3b. `_render_condition(cond_str, indicator_var_map=None, bpd: int = 24)`：兩處字串改用 `bpd`：

```python
        base_cond = (
            f"({base_name}.rolling({n_days}*{bpd}, min_periods={n_days}*{bpd}//2).rank(pct=True)*100 {op} {value})"
        )
```

（zscore 分支同樣把三個 `24` 換成 `{bpd}`。）

3c. `_render_entry_block(side, block, indicator_var_map=None, bpd: int = 24)`：把 `_render_condition(c, indicator_var_map)` 改 `_render_condition(c, indicator_var_map, bpd=bpd)`。

3d. `_render_exit_rule_check(rule, rule_idx=0, indent=..., bph: int = 1)`：time_based 分支改：

```python
    if condition == "time_based":
        max_hold_bars = int(rule.max_hold_hours) * bph
        return f"{indent}if bars_held >= {max_hold_bars}:\n{indent}    exit_flag = True"
```

3e. `_render_exit_state_machine(exit_rules, size_mult=1.0, lag_bars=0, bpd: int = 24, bph: int = 1)`：
- invalidation hoist 的 `rolling({n_days}*24, min_periods={n_days}*24//2)` 改 `{bpd}`；
- `rule_checks` 產生行改傳 `bph=bph`。

3f. `compile_strategy(spec, yaml_hash="", lag_bars=0, interval: str = "1H")`：

```python
    bpd = bars_per_day(interval)
    bph = bars_per_hour(interval)
```

把 `_render_entry_block(..., bpd=bpd)`、`_render_exit_state_machine(spec.exit_rules, size_mult=spec.size_mult, lag_bars=lag_bars, bpd=bpd, bph=bph)` 接上。

- [ ] **Step 4: 跑測試確認 GREEN + 既有 compiler 測試不動全綠（byte-identity 鎖）**

Run: `python -m pytest research/tests/test_signal_compiler_interval.py research/tests/test_signal_compiler.py -q`
Expected: 全 passed。**不准改 `test_signal_compiler.py` 任何斷言** —— 若它紅了，是你把 1H 輸出改壞了。

- [ ] **Step 5: Commit**

```bash
git add research/lib/signal_compiler.py research/tests/test_signal_compiler_interval.py
git commit -m "feat(compiler): interval-aware rolling windows and hold bars (1H byte-identical)"
```

### Task 2: 呼叫端傳 interval + stage4 manifests dir

**Files:**
- Modify: `research/pipeline/stage2b_compile_signal.py`
- Modify: `research/pipeline/stage3_backtest.py`
- Modify: `research/pipeline/stage4_optimize.py`
- Modify: `research/pipeline/stage4_cpcv.py`
- Test: `research/tests/test_signal_compiler_interval.py`（追加）

- [ ] **Step 1: 寫失敗測試（stage4 manifests dir 委派）**

`test_signal_compiler_interval.py` 追加：

```python
def test_stage4_resolves_manifests_via_active_dir(monkeypatch):
    from pipeline.stage4_optimize import _resolve_manifests_dir

    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    assert _resolve_manifests_dir().name == "manifests"

    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    assert _resolve_manifests_dir().name == "30m"


def test_stage4_cpcv_uses_same_resolver(monkeypatch):
    import pipeline.stage4_cpcv as cpcv
    from pipeline.stage4_optimize import _resolve_manifests_dir

    assert cpcv._resolve_manifests_dir is _resolve_manifests_dir
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `python -m pytest research/tests/test_signal_compiler_interval.py -q`
Expected: `ImportError: cannot import name '_resolve_manifests_dir'`。

- [ ] **Step 3: 實作**

3a. `stage4_optimize.py`：加

```python
def _resolve_manifests_dir():
    """Interval-namespaced manifests dir — keeps 30m runs from clobbering 1H
    artifacts. Same delegation as stage 1/2.5/3/3diag/emit_manifest."""
    from lib.timeframe import active_manifests_dir
    return active_manifests_dir()
```

`main()` 內 `manifests_dir = _REPO_ROOT / "research" / "manifests"` 改 `manifests_dir = _resolve_manifests_dir()`。

3b. `stage4_optimize.py` 編譯呼叫接 interval：`_compile_signal_code(spec_dict)` 簽名改 `_compile_signal_code(spec_dict, interval="1H")` → `compile_strategy(spec_model, interval=interval)`；`_optimize_strategy` 與 walk-forward holdout 兩處呼叫改 `_compile_signal_code(spec_dict, cfg.interval)`（`cfg` 兩處作用域皆有，確認後接上）。

3c. `stage4_cpcv.py`：`from pipeline.stage4_optimize import ..., _resolve_manifests_dir`；`run()` 內 `manifests_dir = _REPO_ROOT / "research" / "manifests"` 改 `_resolve_manifests_dir()`；`_compile_signal_code(spec_dict)` 呼叫加 `cfg.interval`。

3d. `stage2b_compile_signal.py`：找 `compile_strategy(` 呼叫，加 `interval=cfg.interval`（檔內已有 config 載入；若函式作用域沒有 cfg，往上找呼叫鏈把 cfg.interval 傳進來，保持最小改動）。

3e. `stage3_backtest.py`：`_run_lag_stress_for_strategy` 內 `compile_strategy(spec, lag_bars=lag)` 改 `compile_strategy(spec, lag_bars=lag, interval=cfg.interval)`（`cfg` 是該函式參數，現成）。

- [ ] **Step 4: 跑全套 research 測試確認 GREEN**

Run: `python -m pytest research/tests/ -q`
Expected: 全 passed（含既有 stage2b/stage3/stage4 相關測試）。

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage2b_compile_signal.py research/pipeline/stage3_backtest.py research/pipeline/stage4_optimize.py research/pipeline/stage4_cpcv.py research/tests/test_signal_compiler_interval.py
git commit -m "fix(stage4): interval-namespaced manifests dir; thread cfg.interval into compiles"
```

---

## Self-check

- [ ] `compile_strategy(spec)`（不帶 interval）輸出與改動前 **byte-identical**（`test_signal_compiler.py` 未改一字且全綠 = 證據）。
- [ ] 30m/15m 的窗與 hold 換算有測試釘住（48/96 bars/day、hold ×2/×4）。
- [ ] stage4 與 stage4_cpcv 都走 `active_manifests_dir()`，且共用同一 resolver（測試釘住）。
- [ ] 沒動 Jinja 模板檔（數字全在 renderer 產生）。
- [ ] 不需要重編譯任何已部署策略（1H 輸出不變）。
