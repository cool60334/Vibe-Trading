# Talos Phase 1A — Statistical Gatekeeper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 建 Foundry 的統計守門——把沙盒跑出的因子序列判生死，全過才寫候選庫，並填滿 `EvidenceCard`（1E）指標欄。核心確定性防線，防 LLM 垃圾因子污染 `research/`。

**Architecture:** 單模組 `research/hermes/gatekeeper.py`，純確定性、無 LLM/docker。複用 `factor_metrics`（`add_forward_returns`）、`deflated_sharpe`、`regime.compute_regime`、`research_ledger`。輸出 `GatekeeperResult`，欄位對映 1E `EvidenceCard`。

**Tech Stack:** Python 3.11、pandas、numpy、scipy.stats。pytest research scope（repo 根 `python -m pytest research/tests/`）。

> **⚠️ 本計畫為 agy 二審後 v2 重寫**（v1 方法論致命錯已修）。關鍵逆轉：**廢除 `net_ic`**（成本扣 forward return 再 rank-corr 統計無意義）→ 改 **Gross IC**（訊號品質）+ **Net IR/Sharpe**（成本把關，1-period 重平衡淨值序列）。此逆轉推翻 `talos-design.md` 附錄 C-1 前版，該文件同步更新。

---

## 設計原則（agy 二審 v2 定案）

1. **訊號品質 = Gross IC**：`spearman(factor_t, fwd_ret_h)`。IC 衡量訊號對**原始**報酬的秩預測力，不可把成本塞進來扭曲。
2. **成本把關 = Net IR/Sharpe**：從**正確對齊的 1-period 重平衡淨值序列**算（`weights_t × ret1_{t+1} − turnover_t × cost`）。**不用 h-period 重疊報酬算 Sharpe**（重疊→自相關→std 低估→Sharpe 假暴增，經典地雷）。
3. **turnover 來自權重**（P1），且有**絕對上限守門**（200%/bar 級不可交易）。
4. **lag 守門員自持**（C-7）：`evaluate` 內部強制 `factor.shift(entry_lag)`，**不信任**呼叫端（LLM/sandbox）會乖乖對齊。
5. **regime 日級 → ffill**：`compute_regime` 回傳**日級**標籤，reindex 到因子頻率須 `method="ffill"`，否則掉 95% 資料。
6. **DSR trial 同質**：N 用 ledger 累計，但 trial SR 只取**同 interval/family** 同質子集餵 `deflated_sharpe`（混異質 T/family 破壞多重檢驗數學）。
7. **矩陣 Spearman 不全域 dropna**：跨數千異期死因子全域 dropna→整列刪光→空矩陣；靠 pandas pairwise corr。
8. **門檻全 config 化**：不同標的/頻率 IC 天花板天差地遠，寫死無擴展性。

---

## File Structure

| 檔案 | 責任 |
|------|------|
| `research/hermes/gatekeeper.py` | 全部統計檢驗 + `GateConfig` + `GatekeeperResult` + `evaluate()` |
| `research/tests/test_hermes_gatekeeper.py` | 假因子（已知答案）單測 |
| `research/hermes/evidence_card.py`（**改**） | `net_ic` 欄改名 `gross_ic`（Task 7） |

**regime 標籤：** bull/bear/neutral（`compute_regime` 實際輸出）。

---

## Task 1: 因子→target weights + turnover（P1 + agy #5）

**Files:** Create `research/hermes/gatekeeper.py`; Test `research/tests/test_hermes_gatekeeper.py`.

EMA z-score（減緩 window-start 不穩定）→ clip [-1,1]。處理 std=0（離散/常數訊號不可變 NaN）。turnover = `|w.diff()|`。

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_gatekeeper.py
import numpy as np
import pandas as pd
import pytest
from research.hermes.gatekeeper import factor_to_weights, turnover_of


def _s(vals, freq="1h"):
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=len(vals), freq=freq),
                     dtype="float64")


def test_weights_clipped_and_discrete_signal_survives_zero_std():
    # a constant-then-step discrete signal must NOT be wiped to NaN by std==0
    f = _s([1.0] * 100 + [-1.0] * 100)
    w = factor_to_weights(f, span=48)
    assert w.abs().max() <= 1.0 + 1e-9
    assert w.notna().sum() > 0                      # not all NaN despite std==0 runs


def test_turnover_is_weight_change():
    w = pd.Series([0.0, 1.0, -1.0], index=pd.date_range("2024-01-01", periods=3, freq="1h"))
    assert turnover_of(w).fillna(0).tolist() == pytest.approx([0.0, 1.0, 2.0])


def test_turnover_charges_first_entry_from_nan_warmup():
    # agy-3 #1: NaN warmup -> first real position must NOT be a free entry
    w = pd.Series([np.nan, np.nan, 0.8, 0.8],
                  index=pd.date_range("2024-01-01", periods=4, freq="1h"))
    tau = turnover_of(w).fillna(0.0)
    assert tau.iloc[2] == pytest.approx(0.8)          # entry 0 -> 0.8 charged
    assert tau.iloc[3] == pytest.approx(0.0)
```

- [ ] **Step 2: Run — fails (ModuleNotFoundError).**

- [ ] **Step 3: Implement**

```python
# research/hermes/gatekeeper.py
"""Talos Factor Foundry statistical gatekeeper (Phase 1A, agy-v2).

Deterministic pass/fail on a sandbox-computed factor. Signal quality = Gross IC
(spearman vs raw h-horizon return); cost gating = Net IR/Sharpe from a correctly
aligned 1-period rebalanced net-return stream (NOT overlapping h-period returns).
Turnover from position weights with an absolute ceiling; entry lag enforced
INSIDE evaluate (never trust the caller); regime labels ffill'd from daily;
DSR trial distribution restricted to homogeneous same-interval ledger trials.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def factor_to_weights(factor: pd.Series, span: int = 168) -> pd.Series:
    """Map factor -> target weights in [-1,1] via causal EMA z-score.

    EMA (not SMA) softens window-start instability (agy #5). std==0 runs (a
    constant/discrete signal) keep their standardised sign instead of becoming
    NaN: z is set to 0 where std==0 so a bounded signal passes through clip."""
    mu = factor.ewm(span=span, adjust=False, min_periods=span // 4).mean()
    sd = factor.ewm(span=span, adjust=False, min_periods=span // 4).std(bias=True)
    z = (factor - mu) / sd
    # agy-3 #5-1: replace ONLY the exactly-flat (sd==0) runs with 0.0; warmup
    # NaN (sd is NaN there) must stay NaN — .where(sd>0,0) wrongly zeroed warmup,
    # handing the strategy a premature 0 position + a fake turnover spike.
    z = z.mask(sd == 0, 0.0)
    return z.clip(-1.0, 1.0)


def turnover_of(weights: pd.Series) -> pd.Series:
    """Per-bar turnover = |w_t - w_{t-1}|. NaN weights (EMA warmup / flat gaps)
    are treated as flat (0.0) BEFORE diff so the first real entry from a NaN
    warmup is charged turnover instead of being a free position (agy-3 #1)."""
    return weights.fillna(0.0).diff().abs()
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): factor->EMA-zscore weights + turnover (Phase 1A, P1)`

---

## Task 2: Gross IC + 正確對齊的 Net IR/Sharpe（agy #1a/1b/1c 核心修正）

**Files:** Modify `gatekeeper.py` + test.

- **Gross IC**：`spearman(factor, ret_h)`。
- **Net IR/Sharpe**：**1-period** 淨值序列——`strat_ret_t = weights_t × ret1_{t+1} − turnover_t × cost_frac`（進場當根付成本；1-period 報酬無重疊）。Sharpe/IR = `mean/std`（per-bar）。**廢除 net_ic**。

- [ ] **Step 1: Failing test**

```python
def test_gross_ic_matches_spearman():
    from research.hermes.gatekeeper import gross_ic
    from scipy.stats import spearmanr
    rng = np.random.default_rng(0)
    f = _s(rng.normal(size=400)); r = _s(rng.normal(size=400))
    assert gross_ic(f, r) == pytest.approx(float(spearmanr(f, r).statistic), abs=1e-9)


def test_net_ir_penalises_high_turnover():
    from research.hermes.gatekeeper import net_ir, factor_to_weights
    rng = np.random.default_rng(1)
    n = 500
    ret1 = _s(rng.normal(scale=0.01, size=n))               # 1-period returns
    calm = _s(np.sin(np.linspace(0, 6, n)))                 # smooth -> low turnover
    churn = _s(rng.normal(size=n))                          # noisy -> high turnover
    ir_calm = net_ir(factor_to_weights(calm), ret1, cost_frac=0.0006)
    ir_churn = net_ir(factor_to_weights(churn), ret1, cost_frac=0.02)
    assert ir_churn < ir_calm                               # cost drag bites churn


def test_net_ir_uses_one_period_return_not_overlapping():
    # guard against the overlapping-return Sharpe-inflation trap: net_ir must be
    # called with a 1-period return series; a longer overlap would inflate it.
    from research.hermes.gatekeeper import net_ir, factor_to_weights
    n = 300
    ret1 = _s(np.random.default_rng(2).normal(scale=0.01, size=n))
    w = factor_to_weights(_s(np.arange(n, dtype="float64")))
    ir = net_ir(w, ret1, cost_frac=0.0006)
    assert np.isfinite(ir)
```

- [ ] **Step 2: Run — fails (ImportError).**

- [ ] **Step 3: Implement**

```python
from scipy.stats import spearmanr


def gross_ic(factor: pd.Series, fwd_ret: pd.Series) -> float:
    """Spearman IC of the factor vs RAW forward return (drops NaN pairs)."""
    paired = pd.concat([factor, fwd_ret], axis=1).dropna()
    if len(paired) < 20:
        return float("nan")
    return float(spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]).statistic)


def net_ir(weights: pd.Series, ret_1period: pd.Series, cost_frac: float) -> float:
    """Per-bar IR/Sharpe of the net return of a 1-period rebalanced position.

    strat_ret_t = weights_t * ret1_{t+1} - turnover_t * cost_frac.
    ret_1period MUST be a single-bar forward return (agy #1c: h-period overlap
    would autocorrelate and inflate Sharpe). Cost is paid when the position is
    set at t; the position earns the next bar's return."""
    fwd1 = ret_1period.shift(-1)                       # weights_t earn ret_{t+1}
    tau = turnover_of(weights)
    strat = (weights * fwd1) - tau.fillna(0.0) * cost_frac
    strat = strat.dropna()
    sd = strat.std(ddof=0)
    if len(strat) < 20 or sd <= 0:
        return float("nan")
    return float(strat.mean() / sd)
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): gross IC + 1-period net IR, drop net_ic (Phase 1A, agy #1)`

---

## Task 3: 非重疊 IC（C-4）

**Files:** Modify `gatekeeper.py` + test.

- [ ] **Step 1: Failing test**

```python
def test_nonoverlap_ic_strides_and_monotone_is_one():
    from research.hermes.gatekeeper import nonoverlap_ic
    n = 300
    f = _s(np.arange(n, dtype="float64")); r = _s(np.arange(n, dtype="float64"))
    assert nonoverlap_ic(f, r, horizon_bars=24) == pytest.approx(1.0, abs=1e-6)


def test_nonoverlap_ic_nan_when_too_few():
    from research.hermes.gatekeeper import nonoverlap_ic
    f = _s(np.arange(30, dtype="float64"))
    assert np.isnan(nonoverlap_ic(f, f, horizon_bars=24))
```

- [ ] **Step 2-4:** as above pattern.

```python
def nonoverlap_ic(factor: pd.Series, fwd_ret: pd.Series, horizon_bars: int) -> float:
    """IC on non-overlapping subsample (every horizon_bars-th row) so a long
    horizon's overlapping windows don't inflate significance (agy C-4)."""
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be >= 1")
    paired = pd.concat([factor, fwd_ret], axis=1).dropna().iloc[::horizon_bars]
    if len(paired) < 20:
        return float("nan")
    return float(spearmanr(paired.iloc[:, 0], paired.iloc[:, 1]).statistic)
```

- [ ] **Step 5: Commit** `feat(hermes): non-overlapping IC subsample (Phase 1A, C-4)`

---

## Task 4: regime（日級 ffill）+ 分年 IC（agy #2）

**Files:** Modify `gatekeeper.py` + test. Reference: `research/lib/regime.py` (`compute_regime(daily_close, ...) -> DataFrame[regime]`, **daily**), `research/lib/regime.py` (`daily_close_from_hourly`).

- [ ] **Step 1: Failing test**

```python
def test_regime_ic_ffills_daily_labels_to_factor_freq():
    from research.hermes.gatekeeper import regime_ic
    # hourly factor, DAILY regime labels — must ffill, not drop 23/24 rows
    hidx = pd.date_range("2024-01-01", periods=240, freq="1h")   # 10 days
    factor = pd.Series(np.arange(240, dtype="float64"), index=hidx)
    fwd = pd.Series(np.arange(240, dtype="float64"), index=hidx)
    didx = pd.date_range("2024-01-01", periods=10, freq="1D")
    daily_labels = pd.Series((["bull"] * 5) + (["bear"] * 5), index=didx)
    r = regime_ic(factor, fwd, daily_labels)
    assert set(r) <= {"bull", "bear", "neutral"}
    # bull covers ~5 days * 24h = 120 hourly rows (ffill worked), IC computable
    assert "bull" in r and np.isfinite(r["bull"])


def test_yearly_ic_splits_by_year():
    from research.hermes.gatekeeper import yearly_ic
    idx = pd.date_range("2022-06-01", periods=500, freq="1D")
    f = pd.Series(np.arange(500, dtype="float64"), index=idx)
    y = yearly_ic(f, f)
    assert "2022" in y and "2023" in y
```

- [ ] **Step 2-4:**

```python
def regime_ic(factor: pd.Series, fwd_ret: pd.Series, daily_regime: pd.Series) -> dict:
    """IC within each regime. daily_regime is DAILY (compute_regime output); it is
    ffill'd onto the factor index so hourly factors keep all rows (agy #2)."""
    labels = daily_regime.reindex(factor.index, method="ffill")
    out: dict = {}
    for label in ("bull", "bear", "neutral"):
        mask = labels == label
        if mask.sum() >= 20:
            out[label] = gross_ic(factor[mask], fwd_ret[mask])
    return out


def yearly_ic(factor: pd.Series, fwd_ret: pd.Series) -> dict:
    out: dict = {}
    for year, idx in factor.groupby(factor.index.year).groups.items():
        if len(idx) >= 20:
            out[str(year)] = gross_ic(factor.loc[idx], fwd_ret.reindex(idx))
    return out
```

- [ ] **Step 5: Commit** `feat(hermes): regime (daily ffill) + yearly IC breakdown (Phase 1A, agy #2)`

---

## Task 5: 矩陣化 pairwise abs-Spearman 去重（agy #4 / P3 / C-2 / C-6）

**Files:** Modify `gatekeeper.py` + test.

**不全域 dropna**（跨數千異期死因子→整列刪光→空矩陣）。整表 `.rank()`（逐欄忽略 NaN）→ `.corr()`（pandas 內建 pairwise NaN-aware）。abs 防反向（C-2），數值序列（C-6）。

- [ ] **Step 1: Failing test**

```python
def test_nearest_correlate_pairwise_survives_disjoint_lifespans():
    from research.hermes.gatekeeper import nearest_correlate
    n = 300
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    base = pd.Series(np.arange(n, dtype="float64"), index=idx)
    others = pd.DataFrame({
        "dead_early": np.r_[np.arange(150, dtype="float64"), [np.nan] * 150],  # dies mid
        "dead_late": np.r_[[np.nan] * 150, -np.arange(150, dtype="float64")],  # born mid, inverse
    }, index=idx)
    # global dropna() would empty this (no row has BOTH non-NaN); pairwise must not.
    name, absrho = nearest_correlate(base, others)
    assert name in {"dead_early", "dead_late"}
    assert absrho == pytest.approx(1.0, abs=1e-6)          # abs catches inverse


def test_nearest_correlate_empty_matrix():
    from research.hermes.gatekeeper import nearest_correlate
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    base = pd.Series(np.arange(50, dtype="float64"), index=idx)
    assert nearest_correlate(base, pd.DataFrame(index=idx)) == (None, 0.0)
```

- [ ] **Step 2-4:**

```python
def nearest_correlate(factor: pd.Series, others: pd.DataFrame) -> tuple:
    """(column_name, max_abs_spearman) of the most-correlated existing/dead factor.
    O(N*K) via corrwith — NOT .corr() (agy-3 #4: .corr() builds the full
    (K+1)x(K+1) all-to-all matrix; at K=5000 that is ~25M pairs and an OOM bomb
    when we only need base-vs-each). Rank per column (NaN-preserving) then
    corrwith aligns pairwise. abs() catches an inverse factor (agy C-2). Returns
    (None, 0.0) when nothing to compare / nothing overlaps."""
    if others.shape[1] == 0:
        return (None, 0.0)
    ranked_factor = factor.rank()
    ranked_others = others.rank()                       # per-column, keeps NaN
    corr = ranked_others.corrwith(ranked_factor).dropna()   # O(N*K), pairwise
    if corr.empty:
        return (None, 0.0)
    abs_corr = corr.abs()
    top = abs_corr.idxmax()
    return (str(top), float(abs_corr.loc[top]))
```

> `corrwith` 預設 pearson，對 rank 值做 pearson == spearman。實作前確認該 pandas 版本 `corrwith` 對常數欄回傳 NaN（由 `.dropna()` 吸收）。

- [ ] **Step 5: Commit** `feat(hermes): matrixed pairwise abs-Spearman dedup, no global dropna (Phase 1A, agy #4)`

---

## Task 6: DSR from ledger（同質 trial 子集，agy #3 / P2 / C-3）

**Files:** Modify `gatekeeper.py` + test. Reference: `research/lib/deflated_sharpe.py`, `research/lib/research_ledger.py` (`read_events`).

N 用 ledger 累計，但 **trial SR 只取同 `symbol` + 同 `interval`** 同質子集（混異質 T/family 破壞 DSR 數學，agy #3）。event detail 須含 `sr_per_bar` + `interval`；1D orchestrator 負責寫，1A 只讀。

- [ ] **Step 1: Failing test**

```python
def test_dsr_uses_only_same_interval_homogeneous_trials(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    events = (
        [{"kind": "factor_trial", "symbol": "eth",
          "detail": {"sr_per_bar": s, "interval": "1H"}} for s in [0.001, 0.002, 0.0015, 0.003]]
        + [{"kind": "factor_trial", "symbol": "eth",       # WRONG interval — must be excluded
            "detail": {"sr_per_bar": 9.9, "interval": "1D"}}]
    )
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: events)
    dsr = gatekeeper.foundry_dsr(0.004, "eth", "1H", manifests_dir=tmp_path, T=8760)
    assert 0.0 <= dsr <= 1.0
    # the 1D outlier (9.9) would blow up variance if wrongly included; exclude it
    monkeypatch.setattr(gatekeeper, "read_events",
                        lambda md: [e for e in events if e["detail"]["interval"] == "1H"])
    assert gatekeeper.foundry_dsr(0.004, "eth", "1H", tmp_path, 8760) == pytest.approx(dsr, abs=1e-12)


def test_dsr_safe_default_when_too_few(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    assert gatekeeper.foundry_dsr(0.004, "eth", "1H", tmp_path, 8760) == 1.0
```

- [ ] **Step 2-4:**

```python
from research.lib.deflated_sharpe import deflated_sharpe
from research.lib.research_ledger import read_events


def foundry_dsr(best_sr_per_bar: float, symbol: str, interval: str,
                manifests_dir, T: int) -> float:
    """Deflated Sharpe using the symbol's HISTORICAL trials at the SAME interval
    (agy #3/P2): count comes from the ledger (multiple-testing debt persists
    across nights), but the trial SR distribution is kept homogeneous — mixing
    different-T / different-interval trials breaks the DSR variance math."""
    trials = [
        e["detail"]["sr_per_bar"]
        for e in read_events(manifests_dir)
        if e.get("kind") == "factor_trial" and e.get("symbol") == symbol
        and isinstance(e.get("detail"), dict)
        and e["detail"].get("interval") == interval
        and "sr_per_bar" in e["detail"]
    ]
    # agy-3 #3: the current factor is NOT yet in the ledger; include it so the
    # trial population N and its variance are complete for the multiple-testing
    # correction (otherwise N is short by 1 and the current sample is missing).
    trials.append(best_sr_per_bar)
    return deflated_sharpe(best_sr_per_bar, trials, T=T)
```

- [ ] **Step 5: Commit** `feat(hermes): Foundry DSR from same-interval ledger trials (Phase 1A, agy #3)`

---

## Task 7: `evaluate()` + `GateConfig` + 內建 lag + MAX_TURNOVER + 1E schema 改名

**Files:** Modify `gatekeeper.py`, `research/hermes/evidence_card.py`（`net_ic`→`gross_ic`）, `research/hermes/__init__.py`; tests（`gatekeeper` + `evidence_card`）.

- **內建 lag（C-7/agy #6c）**：`evaluate` 先 `factor = factor.shift(cfg.entry_lag)`，不信呼叫端。
- **MAX_TURNOVER 守門（agy #6b）**：均 turnover 超 `cfg.max_turnover` → reject。
- **config 門檻（agy #6a）**：`GateConfig` dataclass 傳入。
- **1E schema**：`EvidenceCard.net_ic` → `gross_ic`（含 `_CORE_METRICS`、既有 3 個測試檔 helper、匯出）。

- [ ] **Step 1: Failing test**

```python
def test_evaluate_enforces_lag_internally(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    # factor == next-bar return (look-ahead if NOT lagged). Internal shift must
    # break this perfect same-bar coupling so gross_ic is not a spurious ~1.
    close = pd.Series(100 + np.cumsum(np.random.default_rng(0).normal(size=n)), index=idx)
    ret1 = close.pct_change().shift(-1)
    factor = ret1.copy()                                   # peeks unless lagged
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=24))
    assert abs(res.metrics["gross_ic"]) < 0.99            # lag broke the peek


def test_evaluate_rejects_high_turnover(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    close = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(size=n)), index=idx)
    factor = pd.Series(np.random.default_rng(2).normal(size=n), index=idx)   # noisy -> churn
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path,
                   cfg=GateConfig(interval="1D", horizon_h=24, max_turnover=0.01))
    assert res.passed is False and "turnover" in res.rejection_reason.lower()


def test_evaluate_metrics_match_card_fields(tmp_path, monkeypatch):
    from research.hermes import gatekeeper
    from research.hermes.gatekeeper import evaluate, GateConfig
    n = 400
    idx = pd.date_range("2022-01-01", periods=n, freq="1D")
    close = pd.Series(100 + np.cumsum(np.random.default_rng(3).normal(size=n)), index=idx)
    factor = pd.Series(np.random.default_rng(4).normal(size=n), index=idx)
    ohlcv = pd.DataFrame({"close": close}, index=idx)
    monkeypatch.setattr(gatekeeper, "read_events", lambda md: [])
    res = evaluate(factor, ohlcv, daily_regime=pd.Series("bull", index=idx),
                   existing_and_dead=pd.DataFrame(index=idx), symbol="eth",
                   manifests_dir=tmp_path, cfg=GateConfig(interval="1D", horizon_h=24))
    for k in ("gross_ic", "ic_nonoverlap", "ir", "dsr", "pbo", "turnover",
              "n_samples", "regime_ic", "yearly_ic", "nearest_factor",
              "nearest_abs_spearman"):
        assert k in res.metrics
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
from dataclasses import dataclass

from research.lib.factor_metrics import add_forward_returns
from research.lib.deflated_sharpe import bars_per_year
from research.lib.timeframe import bars_per_hour


@dataclass(frozen=True)
class GateConfig:
    interval: str                       # "1H" / "1D"
    horizon_h: int                      # forward-return horizon (hours)
    entry_lag: int = 1                  # bars to shift the factor before eval (C-7)
    cost_frac: float = 0.0006           # per-unit-turnover cost (taker+slippage)
    gross_ic_min: float = 0.03
    dsr_min: float = 0.5
    redundant_abs_spearman: float = 0.7
    max_turnover: float = 0.5           # mean per-bar turnover ceiling (untradeable above)


@dataclass(frozen=True)
class GatekeeperResult:
    passed: bool
    metrics: dict
    rejection_reason: str = ""


def evaluate(factor, ohlcv, daily_regime, existing_and_dead, symbol,
             manifests_dir, cfg: GateConfig) -> GatekeeperResult:
    factor = factor.shift(cfg.entry_lag)               # agy #6c: gate self-enforces lag
    ret_col = f"ret_{cfg.horizon_h}h"
    fwd = add_forward_returns(ohlcv[["close"]], "close", [cfg.horizon_h],
                              interval=cfg.interval)[ret_col]
    ret1 = ohlcv["close"].pct_change()
    weights = factor_to_weights(factor)
    mean_turnover = float(turnover_of(weights).fillna(0.0).mean())
    sr_bar = net_ir(weights, ret1, cfg.cost_frac)
    nearest, absrho = nearest_correlate(factor, existing_and_dead)

    metrics = {
        "gross_ic": gross_ic(factor, fwd),
        "ic_nonoverlap": nonoverlap_ic(  # agy-3 #5-2: horizon_h is HOURS -> bars
            factor, fwd, horizon_bars=max(1, cfg.horizon_h * bars_per_hour(cfg.interval))),
        "ir": sr_bar,
        "dsr": foundry_dsr(sr_bar if np.isfinite(sr_bar) else 0.0, symbol,
                           cfg.interval, manifests_dir, T=bars_per_year(cfg.interval)),
        "pbo": None,                    # reserved; CPCV-based PBO is a later task
        "turnover": mean_turnover,
        "n_samples": int(pd.concat([factor, fwd], axis=1).dropna().shape[0]),
        "regime_ic": regime_ic(factor, fwd, daily_regime),
        "yearly_ic": yearly_ic(factor, fwd),
        "nearest_factor": nearest,
        "nearest_abs_spearman": absrho,
    }
    gic = metrics["gross_ic"]
    if absrho >= cfg.redundant_abs_spearman:
        return GatekeeperResult(False, metrics, f"redundant: abs_spearman {absrho:.2f} vs {nearest}")
    if mean_turnover > cfg.max_turnover:
        return GatekeeperResult(False, metrics, f"turnover {mean_turnover:.2f} > {cfg.max_turnover}")
    if np.isnan(gic) or abs(gic) < cfg.gross_ic_min:
        return GatekeeperResult(False, metrics, f"weak gross_ic {gic:.4f} < {cfg.gross_ic_min}")
    if metrics["dsr"] < cfg.dsr_min:
        return GatekeeperResult(False, metrics, f"DSR {metrics['dsr']:.2f} < {cfg.dsr_min}")
    return GatekeeperResult(True, metrics, "")
```

**1E schema 改名（同一 commit）**：`research/hermes/evidence_card.py` 把欄位 `net_ic` → `gross_ic`，`_CORE_METRICS` 內 `"net_ic"`→`"gross_ic"`；更新 `test_hermes_evidence_card.py` / `test_hermes_evidence_store.py` / `test_hermes_promote.py` 三處 helper 的 `net_ic=` → `gross_ic=`。`research/hermes/__init__.py` 追加匯出 `evaluate, GateConfig, GatekeeperResult`。

- [ ] **Step 4: Run gatekeeper suite + 全 hermes 套件（含改名的 1E 測試）確認無回歸。**

- [ ] **Step 5: Commit** `feat(hermes): gatekeeper evaluate()+GateConfig, lag+turnover gates, card net_ic->gross_ic (Phase 1A)`

---

## Self-Review

**Spec coverage（agy 二審 v2）：** Gross IC + Net IR（agy #1，取代 net_ic）→ Task 2；1-period 對齊防 Sharpe 灌水（#1c）→ Task 2；regime 日級 ffill（#2）→ Task 4；DSR 同質 trial（#3）→ Task 6；矩陣 pairwise 不 dropna（#4）→ Task 5；std=0+EMA（#5）→ Task 1；config 門檻（#6a）+ MAX_TURNOVER（#6b）+ 內建 lag（#6c）→ Task 7；非重疊 IC（C-4）→ Task 3；矩陣 abs 防反向（C-2/C-6）→ Task 5。

**已知界線：**
- **PBO 暫 None**（schema slot 留）——需 CPCV（`research/lib/cpcv.py`），接線較重，拆後續增量，不阻塞 evaluate。
- **P2 依賴 1D 契約**：`foundry_dsr` 只讀；每 trial 記 `factor_trial` 事件（含 `sr_per_bar`+`interval`）由 1D orchestrator 寫。測試用 monkeypatch。
- **Net IR 為 per-bar**，未年化；DSR 內部亦 per-bar，一致。

**Placeholder scan：** Task 5/6/7 明示「實作前 Read 既有簽名對齊」（`deflated_sharpe`/`research_ledger`/`factor_metrics`/`compute_regime` + pandas `corr()` 版本行為）。PBO=None 為明示保留欄。

**Type consistency：** `factor_to_weights`/`turnover_of`/`gross_ic`/`net_ir`/`nonoverlap_ic`/`regime_ic`/`yearly_ic`/`nearest_correlate`/`foundry_dsr`/`evaluate` 跨 task 一致；`GateConfig` 欄位在 Task 7 定義並被 evaluate 消費；`GatekeeperResult.metrics` 鍵集 == 1E `EvidenceCard` 指標欄（含改名後的 `gross_ic`），Task 7 測試強制。

**跨計畫影響（務必記）：** Task 7 改 1E `EvidenceCard.net_ic`→`gross_ic`（1E 已 committed，此為後續修正）。`talos-design.md` 附錄 C-1 同步更新（net_ic 逆轉）。

---

## 附錄：agy 三審（v2 確認 + 4 個 v2 新引入 bug）

agy 三審確認 v2 架構**正確吸收**二審 6 大修正——`net_ir` 對齊無 look-ahead、`gross_ic` 時間基準一致、`GateConfig` 門檻預設合理（`gross_ic_min=0.03` 初階及格、`dsr_min=0.5` 沙盒垃圾過濾定位、`max_turnover=0.5` 物理上限），**皆查核無誤**。但抓到 4 個 v2 實作細節新洞，全折入：

- **#1 免費建倉 bug**（Task 1 `turnover_of`）——`weights.diff()` 遇 NaN warmup→首次實質建倉 diff 仍 NaN→`fillna(0)` 抹零首筆手續費。改 `weights.fillna(0.0).diff().abs()` + 回歸測試。
- **#3 DSR 漏當前 trial**（Task 6）——ledger 撈的歷史 trial 不含當前因子，N 少 1、變異數缺當前樣本。`deflated_sharpe` 前 `trials.append(best_sr_per_bar)`。
- **#4 OOM 炸彈**（Task 5 `nearest_correlate`）——`ranked.corr()` 算全對全 (K+1)² 矩陣，K=5000→2500 萬對→Worker OOM。改 `ranked_others.corrwith(ranked_factor)` 降到 O(N·K)。
- **#5-1 EMA warmup NaN 抹除**（Task 1 `factor_to_weights`）——`z.where(sd>0,0.0)` 把 warmup 的 `NaN>0=False` 也換成 0.0→提早 0 倉位+假 turnover。改 `z.mask(sd==0, 0.0)`（只換 sd 恰為 0，NaN 留 NaN）。
- **#5-2 ic_nonoverlap 單位錯**（Task 7）——`horizon_h`（小時）當 bars 傳；15m interval 步長錯 4 倍破壞非重疊。改 `horizon_h × bars_per_hour(interval)`（`research.lib.timeframe`）。

---

## Execution Handoff

計畫 v2 存 `docs/talos/plans/2026-07-08-talos-phase1a-gatekeeper.md`。已納 agy 二審全致命修正 + 三審 4 個 v2 新洞。選執行：Subagent-Driven（推薦）或 Inline，每 Task 停等核准。
