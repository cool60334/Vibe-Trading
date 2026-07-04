# Trader 預期 vs 實際交易監控（Part A D2-監控）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** trader 能區分「沉默＝正常低頻」vs「沉默＝壞掉」：status 輸出預期/實際成交數與 regime 檔年齡，異常沉默與 regime 過期發 warning alert。（背景：eth_s5 上線一個月 0 筆交易無人察覺 —— 根因後來確診為 lookback bug，但暴露了監控缺此維度。）

**Architecture:** 新純函式模組 `trader/monitor.py`（預期成交率取自策略 YAML 的 `trades_per_year_estimate`、實際取自 trades.csv、regime 年齡取自 regime_<sym>.json），`loop.py` 每 tick 組一個 `monitor` block 寫進 `testnet_status.json`，兩種一次性 warning alert（silent / regime stale）。**只告警不暫停**（無法從外部斷定策略是否用 regime；誤停比誤報貴）。

**Tech Stack:** Python 標準庫（**不可**引入 pyyaml —— trader 容器映像未必有，YAML 欄位用 regex 抽）、pytest、pandas 已有。

**背景（給零 context 的執行者）：**
- trader 程式在 `dashboard/trader/`：`loop.py`（主迴圈，每 bar 一 tick）、`signal.py`、`freshness.py`、`lookback.py`（現成範例：用 regex 從策略 YAML 抽資訊、經 `research/strategy_runs.json` 找 YAML 路徑 —— **照抄它的讀檔模式**）。
- 測試 scope：**`cd dashboard && python -m pytest trader -q`**。
- 執行期路徑：repo 以 `/repo` 唯讀掛載給 trader；`repo_root`、`manifests_dir`、`out_dir`（= `runs/testnet/<id>/`）在 `loop.run()` 內都已有變數。
- trades.csv 格式（`loop._append_trade` 寫）：header `timestamp,symbol,side,qty,price`，一列 = 一次成交（fill），開/平倉各一列 → 一筆完整交易 ≈ 2 fills。timestamp 為 ISO UTC。
- regime 檔：`research/manifests/regime_<short>.json`，`{"breakdown": [{"date": "YYYY-MM-DD", "regime": "bull|bear|neutral"}, ...]}`，每日由 pipeline 更新。
- 策略 YAML 有 `expected_behavior: trades_per_year_estimate: <int>`（eth_s5=35、sol_s1=80）。缺此欄 → 監控 fail-open（不告警）。
- **檔案所有權**：`dashboard/trader/monitor.py`（新）、`dashboard/trader/test_monitor.py`（新）、`dashboard/trader/loop.py`、`dashboard/trader/test_loop_status.py`。不准動其他檔。

---

### Task 1: `trader/monitor.py` 純函式

**Files:**
- Create: `dashboard/trader/monitor.py`
- Create: `dashboard/trader/test_monitor.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""Tests for trader.monitor — expected-vs-actual trade telemetry."""
import csv
import json
from datetime import datetime, timedelta, timezone

from trader.monitor import (
    actual_fills_last_30d,
    expected_fills_per_30d,
    regime_age_days,
    silence_alert_needed,
)

_NOW = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)


def _make_repo(tmp_path, yaml_text):
    (tmp_path / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "research" / "strategy_runs.json").write_text(json.dumps({
        "eth_s5_half_size": {"spec_yaml": "research/strategies/s.yaml"}
    }), encoding="utf-8")
    y = tmp_path / "research" / "strategies" / "s.yaml"
    y.parent.mkdir(parents=True, exist_ok=True)
    y.write_text(yaml_text, encoding="utf-8")
    return tmp_path


def test_expected_fills_from_yaml_estimate(tmp_path):
    repo = _make_repo(tmp_path, "expected_behavior:\n  trades_per_year_estimate: 36\n")
    # 36 trades/yr ≈ 2.958 trades/30d ≈ 5.916 fills/30d
    val = expected_fills_per_30d(repo, "eth_s5_half_size")
    assert val is not None and abs(val - 36 / 365.25 * 30 * 2) < 1e-9


def test_expected_fills_missing_estimate_is_none(tmp_path):
    repo = _make_repo(tmp_path, "name: x\n")
    assert expected_fills_per_30d(repo, "eth_s5_half_size") is None


def test_expected_fills_missing_strategy_is_none(tmp_path):
    repo = _make_repo(tmp_path, "expected_behavior:\n  trades_per_year_estimate: 36\n")
    assert expected_fills_per_30d(repo, "nope") is None


def _write_trades(out_dir, ts_list):
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "side", "qty", "price"])
        w.writeheader()
        for ts in ts_list:
            w.writerow({"timestamp": ts, "symbol": "E", "side": "buy", "qty": 1, "price": 1})


def test_actual_fills_counts_only_last_30d(tmp_path):
    _write_trades(tmp_path, [
        (_NOW - timedelta(days=40)).isoformat(),
        (_NOW - timedelta(days=10)).isoformat(),
        (_NOW - timedelta(days=1)).isoformat(),
    ])
    assert actual_fills_last_30d(tmp_path, _NOW) == 2


def test_actual_fills_missing_file_is_zero(tmp_path):
    assert actual_fills_last_30d(tmp_path, _NOW) == 0


def test_silence_alert_thresholds():
    assert silence_alert_needed(6.0, 0) is True     # >=3 expected trades, none seen
    assert silence_alert_needed(6.0, 1) is False    # any fill clears it
    assert silence_alert_needed(4.0, 0) is False    # too low-freq to judge
    assert silence_alert_needed(None, 0) is False   # unknown expectancy: fail-open


def test_regime_age_days_reads_last_breakdown_date(tmp_path):
    (tmp_path / "regime_eth.json").write_text(json.dumps({
        "breakdown": [{"date": "2026-07-01", "regime": "bear"},
                      {"date": "2026-07-02", "regime": "bear"}]
    }), encoding="utf-8")
    age = regime_age_days(tmp_path, "ETH/USDT:USDT", _NOW)
    assert age is not None and abs(age - 2.5) < 0.01  # 07-02 00:00 → 07-04 12:00


def test_regime_age_missing_file_is_none(tmp_path):
    assert regime_age_days(tmp_path, "ETH/USDT:USDT", _NOW) is None
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `cd dashboard && python -m pytest trader/test_monitor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.monitor'`。

- [ ] **Step 3: 實作 `trader/monitor.py`**

```python
"""Expected-vs-actual trade telemetry — pure functions, no network.

A strategy that goes silent for a month is either legitimately low-frequency
or broken (all-NaN transforms, stale regime mask, drifted factor
distribution). The kill switch only watches losses; these helpers give the
loop a "should have traded by now" dimension so silence becomes visible.
Seed of the Hermes alpha-decay monitor (Part A D2).
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from trader.freshness import _symbol_short, _to_utc

_ESTIMATE_RE = re.compile(r"trades_per_year_estimate:\s*(\d+(?:\.\d+)?)")

#: Minimum expected fills/30d before zero fills is deemed suspicious.
#: 6 fills = ~3 trades → P(0 trades | λ=3) ≈ 5%.
SILENCE_MIN_EXPECTED_FILLS = 6.0


def expected_fills_per_30d(repo_root: "str | Path", strategy_id: str) -> Optional[float]:
    """Fills (order executions) per 30 days implied by the strategy YAML's
    ``trades_per_year_estimate``; one round-trip trade = 2 fills. None when
    the registry/YAML/estimate is missing (fail open — no alert)."""
    repo_root = Path(repo_root)
    try:
        runs = json.loads(
            (repo_root / "research" / "strategy_runs.json").read_text(encoding="utf-8")
        )
        spec_yaml = (runs.get(strategy_id) or {}).get("spec_yaml")
        if not spec_yaml:
            return None
        text = (repo_root / spec_yaml).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    m = _ESTIMATE_RE.search(text)
    if not m:
        return None
    trades_per_year = float(m.group(1))
    return trades_per_year / 365.25 * 30 * 2


def actual_fills_last_30d(out_dir: "str | Path", now: datetime) -> int:
    """Rows of trades.csv (one row = one fill) within the last 30 days."""
    path = Path(out_dir) / "trades.csv"
    if not path.exists():
        return 0
    cutoff = _to_utc(now).timestamp() - 30 * 86400
    n = 0
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ts = _to_utc(datetime.fromisoformat(row["timestamp"]))
                except (KeyError, ValueError):
                    continue
                if ts.timestamp() >= cutoff:
                    n += 1
    except OSError:
        return 0
    return n


def silence_alert_needed(expected_fills_30d: Optional[float], actual_fills_30d: int) -> bool:
    """True when the strategy should statistically have traded but hasn't."""
    if expected_fills_30d is None:
        return False
    return expected_fills_30d >= SILENCE_MIN_EXPECTED_FILLS and actual_fills_30d == 0


def regime_age_days(manifests_dir: "str | Path", symbol: str, now: datetime) -> Optional[float]:
    """Days since the last date in regime_<short>.json breakdown; None if
    the file is missing/unreadable/empty. The regime overlay IS the alpha for
    regime-filtered strategies — a frozen regime silently masks all entries."""
    short = _symbol_short(symbol)
    path = Path(manifests_dir) / f"regime_{short}.json"
    try:
        breakdown = json.loads(path.read_text(encoding="utf-8")).get("breakdown") or []
    except (OSError, json.JSONDecodeError):
        return None
    if not breakdown:
        return None
    last = breakdown[-1].get("date")
    try:
        last_dt = datetime.strptime(str(last), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (_to_utc(now) - last_dt).total_seconds() / 86400
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `cd dashboard && python -m pytest trader/test_monitor.py -q`
Expected: 9 passed。

- [ ] **Step 5: Commit**

```bash
git add dashboard/trader/monitor.py dashboard/trader/test_monitor.py
git commit -m "feat(trader): monitor helpers for expected-vs-actual fills + regime age"
```

### Task 2: loop 佈線 + status 輸出

**Files:**
- Modify: `dashboard/trader/loop.py`
- Modify: `dashboard/trader/test_loop_status.py`

- [ ] **Step 1: 寫失敗測試（status 帶 monitor block）**

`test_loop_status.py` 追加：

```python
def test_write_status_includes_monitor_block(tmp_path):
    monitor = {"expected_fills_30d": 5.9, "actual_fills_30d": 0,
               "silent": False, "regime_age_days": 1.2}
    _write_status(out_dir=tmp_path, mode="paper", monitor=monitor, **_COMMON)
    assert _read_status(tmp_path)["monitor"] == monitor


def test_write_status_monitor_defaults_to_none(tmp_path):
    _write_status(out_dir=tmp_path, mode="paper", **_COMMON)
    assert _read_status(tmp_path)["monitor"] is None
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `cd dashboard && python -m pytest trader/test_loop_status.py -q`
Expected: FAIL — `_write_status() got an unexpected keyword argument 'monitor'`。

- [ ] **Step 3: 實作**

3a. `loop.py` `_write_status` 簽名末尾加參數並寫進 payload：

```python
def _write_status(
    out_dir: Path,
    ...(既有參數不動)...
    mode: str = "paper",
    monitor: "Optional[dict]" = None,
) -> None:
    status = {
        ...(既有欄位不動)...
        "alerts": alerts[-50:],  # keep last 50
        "monitor": monitor,
    }
```

3b. `run()` 內：import 區（`from trader.signal import compute_signal` 附近）加
`from trader.monitor import actual_fills_last_30d, expected_fills_per_30d, regime_age_days, silence_alert_needed`；
狀態變數區（`insufficient_alerted = False` 附近）加：

```python
    expected_fills = expected_fills_per_30d(repo_root, strategy_id)
    silence_alerted = False
    regime_stale_alerted = False
    REGIME_MAX_AGE_DAYS = float(os.environ.get("REGIME_MAX_AGE_DAYS", "3"))
```

3c. `run()` 主迴圈裡「Write status & equity snapshot」段之前組 monitor block 並處理告警：

```python
            # ── Expectancy / regime telemetry (alert-only, never pauses) ──────
            now_dt = datetime.now(tz=timezone.utc)
            actual_fills = actual_fills_last_30d(out_dir, now_dt)
            r_age = regime_age_days(manifests_dir, symbol, now_dt)
            silent = silence_alert_needed(expected_fills, actual_fills)
            monitor = {
                "expected_fills_30d": expected_fills,
                "actual_fills_30d": actual_fills,
                "silent": silent,
                "regime_age_days": r_age,
            }
            if silent and not silence_alerted:
                silence_alerted = True
                alerts.append({
                    "timestamp": _now_iso(), "severity": "warning",
                    "message": (
                        f"no fills in 30d but ~{expected_fills:.1f} expected — "
                        "verify signal path / regime mask / factor drift"
                    ),
                })
                logger.warning("Silent strategy: 0 fills vs %.1f expected/30d", expected_fills)
            elif not silent:
                silence_alerted = False
            if r_age is not None and r_age > REGIME_MAX_AGE_DAYS and not regime_stale_alerted:
                regime_stale_alerted = True
                alerts.append({
                    "timestamp": _now_iso(), "severity": "warning",
                    "message": f"regime file stale: {r_age:.1f}d old (> {REGIME_MAX_AGE_DAYS}d) — overlay may mask entries forever",
                })
                logger.warning("Regime file stale: %.1fd", r_age)
            elif r_age is not None and r_age <= REGIME_MAX_AGE_DAYS:
                regime_stale_alerted = False
```

3d. 兩處 `_write_status(...)` 呼叫（主迴圈那次）加 `monitor=monitor`；terminate 分支與迴圈外收尾那兩次加 `monitor=None`（變數彼時可能未定義）。

- [ ] **Step 4: 跑全套 trader 測試確認 GREEN**

Run: `cd dashboard && python -m pytest trader -q`
Expected: 全 passed（既有 89+ 與新增皆綠）。

- [ ] **Step 5: Commit**

```bash
git add dashboard/trader/loop.py dashboard/trader/test_loop_status.py
git commit -m "feat(trader): expectancy + regime-age telemetry in status with one-shot alerts"
```

---

## Self-check

- [ ] 告警是 warning 且**不改變交易行為**（不 pause、不平倉）。
- [ ] 預期值來源缺失時完全靜默（fail-open），不誤報。
- [ ] 沒有引入 pyyaml 依賴（regex 抽 YAML 欄位）。
- [ ] status JSON 舊欄位一個都沒動（前端相容）。
