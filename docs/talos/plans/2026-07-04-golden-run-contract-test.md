# Golden-Run 引擎合約測試（Part A C2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一個凍結快照測試：固定合成 OHLCV + 固定 signal engine 丟進 `agent/backtest` 引擎，斷言 metrics 永遠相同 —— 上游 `agent/` 更新若默默改變引擎行為（費率預設、對齊、成交時序），此測試立刻紅。

**Architecture:** 純本地、零網路。測試內以固定 seed 產生合成 K 線 → stub loader → `CryptoEngine.run_backtest()` → 斷言 metrics 關鍵欄位等於凍結常數。首次執行時先跑一步「捕捉腳本」把實際 metrics 印出、貼進測試常數（golden 測試的標準建立流程）。

**Tech Stack:** pytest、pandas、numpy、`agent/backtest`（只 import，不修改）。

**背景（給零 context 的執行者）：**
- 本 repo 兩套系統：`agent/`（上游開源，**絕不修改**）與 `research/` + `dashboard/`（私有）。本計畫只新增 `research/tests/` 下的檔案。
- research 測試 scope：**從 repo 根**跑 `python -m pytest research/tests/ -q`。不可與 agent/dashboard 測試混跑。
- `import backtest` 需要 `agent/` 在 sys.path（repo 沒把它裝進所有環境）——測試檔內自行 bootstrap（下方代碼已含）。
- **檔案所有權**：只准建 `research/tests/test_golden_run.py`。不准動任何其他檔案。

---

### Task 1: 測試骨架 + 捕捉當前引擎行為

**Files:**
- Create: `research/tests/test_golden_run.py`

- [ ] **Step 1: 寫測試檔（快照常數先填 None，故意失敗）**

```python
"""Golden-run contract test for the upstream agent/backtest engine.

research/ depends on agent/backtest as its simulator. Upstream pulls can
silently change engine behaviour (fee defaults, signal alignment,
execution timing) and every research conclusion with it. This test runs a
fixed synthetic market through CryptoEngine and pins the metrics.

If this test goes red after an upstream update: the ENGINE changed, not
your strategy. Investigate the diff in agent/backtest before re-freezing.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_DIR = _REPO_ROOT / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.crypto import CryptoEngine  # noqa: E402

SYMBOL = "BTC-USDT-SWAP"
N_BARS = 24 * 120  # 120 days of 1H bars


def _synthetic_ohlcv() -> pd.DataFrame:
    """Deterministic random-walk OHLCV. Seeded → identical on every run."""
    rng = np.random.default_rng(20260704)
    idx = pd.date_range("2024-01-01", periods=N_BARS, freq="h")
    ret = rng.normal(0, 0.004, N_BARS)
    close = 30000 * np.exp(np.cumsum(ret))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.002, N_BARS))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.002, N_BARS))
    vol = rng.uniform(100, 1000, N_BARS)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


class _StubLoader:
    name = "golden-stub"

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def fetch(self, codes, start_date, end_date, fields=None, interval=None):
        return {c: self._df.copy() for c in codes}


class _FlipEngine:
    """Deterministic long/short flipper: sign of a slow sine → ~dozens of trades."""

    def generate(self, data_map):
        df = data_map[SYMBOL]
        phase = np.arange(len(df)) / (24 * 10) * 2 * np.pi  # 10-day cycle
        sig = pd.Series(np.sign(np.sin(phase)), index=df.index, dtype=float)
        return {SYMBOL: sig}


# Frozen snapshot — populated in Task 1 Step 3 from the capture run.
# None = not yet frozen (test fails loudly instead of vacuously passing).
GOLDEN: "dict | None" = None


def _run() -> dict:
    import tempfile

    df = _synthetic_ohlcv()
    config = {"codes": [SYMBOL], "interval": "1H", "initial_cash": 100_000}
    engine = CryptoEngine(config)
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        (run_dir / "code").mkdir()
        (run_dir / "code" / "signal_engine.py").write_text("# golden stub\n")
        metrics = engine.run_backtest(
            config, _StubLoader(df), _FlipEngine(), run_dir, bars_per_year=8760,
        )
    return metrics


def test_engine_behaviour_is_frozen():
    assert GOLDEN is not None, (
        "GOLDEN snapshot not frozen yet — run the capture step "
        "(see plan Task 1 Step 3) and paste the printed dict here."
    )
    m = _run()
    for key, expected in GOLDEN.items():
        actual = float(m[key])
        assert actual == pytest.approx(expected, abs=1e-9), (
            f"engine contract drift on {key!r}: frozen={expected} now={actual} — "
            "upstream agent/backtest behaviour changed"
        )
```

- [ ] **Step 2: 跑測試，確認因快照未凍結而失敗（RED）**

Run（repo 根）: `python -m pytest research/tests/test_golden_run.py -q`
Expected: 1 failed — `GOLDEN snapshot not frozen yet`。
若失敗原因是 import error / 引擎跑不起來，先修 bootstrap 或 stub 的相容問題（**不可**修 agent/ 本身），直到失敗原因正確。

- [ ] **Step 3: 捕捉當前 metrics 並凍結**

Run（repo 根）:
```bash
python -c "
import sys; sys.path.insert(0, 'research/tests')
from test_golden_run import _run
m = _run()
keys = ['sharpe', 'total_return', 'max_drawdown', 'trade_count']
print({k: float(m[k]) for k in keys if k in m})
print('ALL KEYS:', sorted(m.keys()))
"
```
把印出的 dict 貼進 `GOLDEN = {...}`（若某 key 不存在，從 ALL KEYS 挑等義欄位並在常數旁註記）。**至少凍結 4 個欄位**：sharpe、total_return、max_drawdown、trade_count。

- [ ] **Step 4: 跑測試確認 GREEN + 連跑兩次確認確定性**

Run: `python -m pytest research/tests/test_golden_run.py -q && python -m pytest research/tests/test_golden_run.py -q`
Expected: 兩次都 1 passed（若第二次數值不同 = 合成資料或引擎不確定性，回頭檢查 seed / TemporaryDirectory 路徑是否洩進 metrics）。

- [ ] **Step 5: 跑全套 research 測試確認無迴歸**

Run: `python -m pytest research/tests/ -q`
Expected: 全綠。

- [ ] **Step 6: Commit**

```bash
git add research/tests/test_golden_run.py
git commit -m "test(research): golden-run contract test pinning agent/backtest behaviour"
```

---

## Self-check（執行者完成前過一遍）

- [ ] 沒動 `agent/` 任何檔案。
- [ ] 測試零網路（stub loader）。
- [ ] GOLDEN 已凍結成字面常數（不是執行期計算）。
- [ ] 兩次執行結果 byte-identical。
