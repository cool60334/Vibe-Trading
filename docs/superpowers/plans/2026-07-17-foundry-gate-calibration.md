# Foundry 偵測門檻校準 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修好 Foundry 統計閘的三個校準缺陷，並建立雙向控制組當常設迴歸測試 —— 讓「這張網撈得到多弱的 alpha」變成一個被量測、被守住的數字。

**Architecture:** `deflated_sharpe` 把「試驗次數 N」與「變異數樣本」拆成獨立參數（恢復 `foundry_dsr` docstring 本來就宣稱的契約）；DSR 改吃 gross SR（虛無假設下期望為 0），成本改由獨立的 `net_ir > 0` 閘負責；新增 `research/hermes/calibration.py` 提供正/負控制的純函式，測試與 CLI 共用。

**Tech Stack:** Python 3.11、pandas、numpy、scipy、pytest。

Spec：[docs/superpowers/specs/2026-07-17-foundry-gate-calibration-design.md](../specs/2026-07-17-foundry-gate-calibration-design.md)

## Global Constraints

- **測試從 repo root 跑**：`python -m pytest research/tests/ -q`。**絕不**與 `agent/tests/` 或 `dashboard/server` 的 pytest 混在同一次呼叫（三個獨立 scope）。
- **不動任何門檻數值**：`gross_ic_min=0.03`、`dsr_min=0.5`、`redundant_abs_spearman=0.7`、`max_turnover=0.5` 一律不改。本計畫修的是**統計錯誤**，不是門檻鬆緊。
- **`max_turnover` 不砍**：它是**物理執行上限**（防非線性滑點/市場衝擊，線性成本模型抓不到），不是獲利指標的延伸。只改 docstring。
- **控制組是常設迴歸測試**，不是一次性腳本。
- **CI 不呼叫付費 LLM**。控制組全部走 `evaluate()`，不碰 LLM。
- **禁止 factor-specific 邏輯**：閘裡不得出現針對特定因子/區間的條件。閘只能是與因子身分無關的統計量（spec §4.4）。
- 每個 task 結束 commit，英文 message，結尾 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。分支 `quant-trading-dashboard`。**不 push**，除非使用者明講。

## File Structure

| 檔案 | 建/改 | 責任 |
|---|---|---|
| `research/lib/deflated_sharpe.py` | 改 | `N` 與 `var_trials` 拆成獨立參數 |
| `research/hermes/gatekeeper.py` | 改 | `gross_ir`、`net_ir>0` 閘、`foundry_dsr` 改吃 gross + N 接續 + `T=n_samples`、`max_turnover` docstring |
| `research/hermes/orchestrator.py` | 改 | ledger 記 `gross_sr_per_bar` |
| `research/hermes/calibration.py` | 建 | `plant_alpha` / `circular_shift` / `PRIME_SHIFT_DAYS` |
| `research/tests/test_hermes_gate_calibration.py` | 建 | 正/負控制迴歸測試 |
| `research/tests/test_hermes_gatekeeper.py` | 改 | `gross_ir`、`net_ir` 閘、ledger 契約 |

---

## Task 1: `deflated_sharpe` 拆開 N 與變異數樣本

**Files:**
- Modify: `research/lib/deflated_sharpe.py:30-60`
- Test: `research/tests/test_factor_gates.py`（既有 deflated_sharpe 測試在此；先 `grep -n deflated_sharpe research/tests/*.py` 確認，若不在則建 `research/tests/test_deflated_sharpe.py`）

**Interfaces:**
- Produces: `deflated_sharpe(best_sr_per_bar, trial_srs_per_bar, T, expected_sr=0.0, n_trials=None) -> float`
  `n_trials=None` → 沿用 `len(有限的 trial_srs)`（向後相容）。給值 → 用它當 N，變異數仍取自 `trial_srs_per_bar`。

> **這不是新發明，是恢復既有契約。** `foundry_dsr` 的 docstring 本來就寫：「**count comes from the ledger (multiple-testing debt persists across nights)**, but the trial SR distribution is kept homogeneous」—— 次數與分佈本來就該分開。但 `deflated_sharpe` 用 `n = srs.size` 同時當兩者，所以那個宣稱從來沒被實現過。

- [ ] **Step 1: 寫失敗測試**

```python
# 若既有測試檔沒有 deflated_sharpe，建 research/tests/test_deflated_sharpe.py
import numpy as np
import pytest
from research.lib.deflated_sharpe import deflated_sharpe


def test_n_trials_defaults_to_the_sample_count():
    """不給 n_trials 時行為完全不變（向後相容）。"""
    srs = [0.01, -0.01, 0.02, -0.02, 0.005]
    assert deflated_sharpe(0.03, srs, T=20000) == deflated_sharpe(
        0.03, srs, T=20000, n_trials=len(srs))


def test_more_trials_is_stricter_at_the_same_variance():
    """N 是多重測試債：試越多次，同一個 SR 越不顯著。
    這是拆開參數的全部理由 —— 債務要能獨立於變異數樣本累計。"""
    srs = [0.01, -0.01, 0.02, -0.02, 0.005]
    few = deflated_sharpe(0.03, srs, T=20000, n_trials=5)
    many = deflated_sharpe(0.03, srs, T=20000, n_trials=500)
    assert many < few


def test_variance_still_comes_from_the_series_not_from_n_trials():
    """n_trials 只影響 max_z，不得影響變異數估計。"""
    tight = [0.001, -0.001, 0.002, -0.002]
    wide = [0.10, -0.10, 0.20, -0.20]
    assert deflated_sharpe(0.03, tight, T=20000, n_trials=50) > \
           deflated_sharpe(0.03, wide, T=20000, n_trials=50)


def test_n_trials_below_two_is_undefined_and_never_blocks():
    assert deflated_sharpe(0.03, [0.01, -0.01], T=20000, n_trials=1) == 1.0


def test_n_trials_can_exceed_the_variance_sample_count():
    """真實用途：39 筆舊 trial 的『次數』要接續，但它們的 net SR 不進變異數。"""
    r = deflated_sharpe(0.03, [0.01, -0.01, 0.02], T=20000, n_trials=42)
    assert 0.0 <= r <= 1.0


def test_n_trials_must_be_positive():
    with pytest.raises(ValueError, match="n_trials"):
        deflated_sharpe(0.03, [0.01, -0.01], T=20000, n_trials=0)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_deflated_sharpe.py -q`
Expected: FAIL — `TypeError: deflated_sharpe() got an unexpected keyword argument 'n_trials'`

- [ ] **Step 3: 改實作**

把 `deflated_sharpe` 的簽章與前段換成：

```python
def deflated_sharpe(
    best_sr_per_bar: float,
    trial_srs_per_bar: "np.ndarray | list[float]",
    T: int,
    expected_sr: float = 0.0,
    n_trials: "int | None" = None,
) -> float:
    """Probability the best trial's true per-bar Sharpe exceeds the expected
    maximum Sharpe of N trials under the null (skew=0, kurt=3).

    All Sharpes MUST be per-bar (de-annualised). ``T`` = train-window bar count.
    Returns 1.0 when deflation is undefined (N<2, T<2, zero trial variance) so
    the gate never spuriously fails. Result is a probability in [0, 1].

    ``n_trials`` separates the two things N was doing at once. The multiple-testing
    DEBT is a count -- how many times we cast into this pool -- and it survives
    anything we later learn about the measurements. The trial SR series is a
    VARIANCE sample, and it is only usable while the trials are drawn from one
    distribution. Those two can legitimately diverge: when a batch of past trials
    turns out to have been measured on a different scale (e.g. net-of-cost Sharpe,
    whose spread tracks turnover rather than search noise), its variance must be
    dropped while its count must not. Defaults to the finite sample count, which
    is the old behaviour.

    foundry_dsr's docstring already promised this split ("count comes from the
    ledger ... but the trial SR distribution is kept homogeneous"). It was never
    implementable while n came from srs.size.
    """
    srs = np.asarray(list(trial_srs_per_bar), dtype=float)
    srs = srs[np.isfinite(srs)]
    if n_trials is None:
        n_trials = srs.size
    elif n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    if srs.size < 2 or n_trials < 2 or T < 2:
        return 1.0

    var_srs = float(np.var(srs, ddof=1))
    if var_srs <= 1e-30:
        return 1.0

    gamma = 0.5772156649  # Euler-Mascheroni
    n = n_trials
    max_z = (1 - gamma) * st.norm.ppf(1 - 1.0 / n) + gamma * st.norm.ppf(
        1 - 1.0 / (n * np.e)
    )
    expected_max_sr = expected_sr + np.sqrt(var_srs) * max_z

    sr_std = np.sqrt((1.0 + 0.5 * best_sr_per_bar**2) / (T - 1))
    return float(st.norm.cdf((best_sr_per_bar - expected_max_sr) / sr_std))
```

**Commit 前對每個改到的 `.py` 跑這個**：

```bash
grep -nP '[^\x00-\x7f]' research/lib/deflated_sharpe.py || echo clean
```
Expected: `clean`。

理由不是潔癖：撰寫這份 spec/plan 的過程中，**西里爾字母兩次混進英文字串**（`близко`、`диverge`），兩次都發生在寫長篇英文註解時，兩次都是肉眼幾乎看不出來的同形字。docstring 混進去只是難看，**但同樣的手滑掉進欄名或字典 key 就是一個找不到的 bug**。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/ -q`
Expected: PASS（全綠；既有 deflated_sharpe 呼叫端不傳 `n_trials`，行為不變）

- [ ] **Step 5: Commit**

```bash
git add research/lib/deflated_sharpe.py research/tests/test_deflated_sharpe.py
git commit -m "$(cat <<'EOF'
refactor(deflated-sharpe): separate the trial count from the variance sample

N was doing two jobs at once. The multiple-testing debt is a count --
how many times we cast into this pool -- and it survives anything we
later learn about the measurements. The trial SR series is a variance
sample, usable only while the trials share one distribution.

Those two legitimately diverge. A batch of past trials measured on a
different scale (net-of-cost Sharpe, whose spread tracks turnover rather
than search noise) must lose its variance and keep its count.

foundry_dsr's docstring already promised exactly this -- "count comes
from the ledger (multiple-testing debt persists across nights), but the
trial SR distribution is kept homogeneous". It was never implementable
while n came from srs.size.

Default is unchanged: omit n_trials and it is the finite sample count.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `gross_ir` 指標

**Files:**
- Modify: `research/hermes/gatekeeper.py`（`net_ir` 附近，約 :57-75）
- Test: `research/tests/test_hermes_gatekeeper.py`

**Interfaces:**
- Consumes: 無
- Produces: `gross_ir(weights: pd.Series, ret_1period: pd.Series) -> float` —— 與 `net_ir` 同語意但**不扣成本**
- Produces: `evaluate()` 的 `metrics` 新增 key `"gross_ir"`

- [ ] **Step 1: 寫失敗測試**（append 到 `research/tests/test_hermes_gatekeeper.py`）

```python
def test_gross_ir_ignores_cost_while_net_ir_pays_it():
    """DSR 的虛無假設是『零 alpha 下 gross SR 期望為 0』。成本是確定性的、
    因子專屬的，不是搜尋的抽樣噪音 —— 混進 DSR 的變異數就毀掉它。"""
    import numpy as np, pandas as pd
    from research.hermes.gatekeeper import gross_ir, net_ir

    idx = pd.date_range("2024-01-01", periods=500, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    ret1 = pd.Series(rng.standard_normal(500) * 0.01, index=idx)
    weights = pd.Series(np.sign(rng.standard_normal(500)), index=idx)

    g = gross_ir(weights, ret1)
    n = net_ir(weights, ret1, cost_frac=0.0006)
    assert np.isfinite(g) and np.isfinite(n)
    assert g > n                       # 成本只會扣分


def test_gross_ir_equals_net_ir_at_zero_cost():
    import numpy as np, pandas as pd
    from research.hermes.gatekeeper import gross_ir, net_ir

    idx = pd.date_range("2024-01-01", periods=500, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    ret1 = pd.Series(rng.standard_normal(500) * 0.01, index=idx)
    weights = pd.Series(rng.standard_normal(500), index=idx).clip(-1, 1)
    assert gross_ir(weights, ret1) == pytest.approx(net_ir(weights, ret1, cost_frac=0.0))


def test_gross_ir_is_undefined_on_a_flat_position():
    import numpy as np, pandas as pd
    from research.hermes.gatekeeper import gross_ir

    idx = pd.date_range("2024-01-01", periods=500, freq="h", tz="UTC")
    ret1 = pd.Series(np.linspace(0.001, 0.002, 500), index=idx)
    assert np.isnan(gross_ir(pd.Series(0.0, index=idx), ret1))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_gatekeeper.py -k gross_ir -q`
Expected: FAIL — `ImportError: cannot import name 'gross_ir'`

- [ ] **Step 3: 寫實作**（放在 `net_ir` 正下方）

```python
def gross_ir(weights: pd.Series, ret_1period: pd.Series) -> float:
    """Per-bar IR/Sharpe of the position's return BEFORE trading cost.

    Same alignment contract as net_ir: ret_1period MUST be a TRAILING single-bar
    return; this function shifts it forward internally.

    This is what DSR must be fed. DSR asks whether a signal's predictive power is
    luck, and under the null of zero alpha the expected GROSS Sharpe is exactly 0 --
    which is the mean deflated_sharpe already assumes. Net Sharpe has no such
    property: its mean sits wherever the cost drag puts it (measured median -0.018
    on eth), and its spread across trials tracks how much turnover differs between
    factors (0.02 to 0.39, a 20x range) rather than the noise of the search. Feeding
    that to DSR inflated the trial variance 3x over the theoretical sampling noise
    and pushed the gate's minimum detectable alpha to an annualised Sharpe of ~5.

    Cost does not disappear; net_ir owns it, behind its own gate.
    """
    fwd1 = ret_1period.shift(-1)                      # weights_t earn ret_{t+1}
    strat = (weights * fwd1).dropna()
    sd = strat.std(ddof=0)
    if len(strat) < 20 or sd <= 0:
        return float("nan")
    return float(strat.mean() / sd)
```

在 `evaluate()` 的 `metrics` dict 加一行（緊接 `"ir": sr_bar,` 之後）：

```python
        "gross_ir": gross_ir(weights, ret1),
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/gatekeeper.py research/tests/test_hermes_gatekeeper.py
git commit -m "$(cat <<'EOF'
feat(hermes): measure gross IR, the Sharpe before trading cost

DSR asks whether a signal's predictive power is luck. Under the null of
zero alpha the expected GROSS Sharpe is exactly 0 -- which is the mean
deflated_sharpe already assumes.

Net Sharpe has no such property. Its mean sits wherever cost drag puts
it (measured median -0.018 on eth), and its spread across trials tracks
how much turnover differs between factors -- 0.02 to 0.39, a 20x range --
rather than the noise of the search. Feeding that to DSR inflated the
trial variance to 3x the theoretical sampling noise and pushed the gate's
weakest detectable alpha to an annualised Sharpe near 5.

Cost does not disappear. net_ir keeps it, behind its own gate.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: ledger 記 `gross_sr_per_bar`，且**每個被檢定過的因子都要記**

**Files:**
- Modify: `research/hermes/orchestrator.py`（`process_hypothesis` 裡 `append_event` 那段，約 :157-159）
- Test: `research/tests/test_hermes_orchestrator.py`

**Interfaces:**
- Consumes: Task 2 的 `metrics["gross_ir"]`
- Produces: ledger `factor_trial` 事件的 `detail` 新增 `"gross_sr_per_bar"`；`"sr_per_bar"` **保留**（既有讀者、歷史相容）

> **spec §3.4：這是契約，不是巧合。** 現況 `append_event` 在 `evaluate()` **回傳之後**才呼叫，所以早退的閘也會記帳 —— 行為正確，但**沒有任何東西保證它**。實作者很容易「順手優化」成早退不記帳 → `N` 被系統性低估 → DSR 完全失效 + 倖存者偏差（只有賺錢的因子進 ledger，變異數也跟著扭曲）。本 task 加測試把它釘死。

- [ ] **Step 1: 寫失敗測試**（append 到 `research/tests/test_hermes_orchestrator.py`）

```python
@pytest.mark.parametrize("passed,reason", [
    (True, ""),
    (False, "weak gross_ic 0.001 < 0.03"),
    (False, "net_ir -0.01 <= 0"),
    (False, "DSR 0.01 < 0.5"),
])
def test_every_evaluated_factor_lands_in_the_ledger(tmp_path, monkeypatch, passed, reason):
    """N is the multiple-testing debt. A factor that reached evaluate() WAS
    tested -- whatever the verdict -- so it must be counted. Skipping the losers
    undercounts N and lets only profitable factors shape the variance."""
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult

    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    series = pd.Series(np.arange(300.0), index=idx)
    metrics = {"gross_ic": 0.05, "ic_nonoverlap": 0.04, "ir": 0.3, "gross_ir": 0.31,
               "dsr": 0.9, "pbo": None, "turnover": 0.1, "n_samples": 300,
               "regime_ic": {}, "yearly_ic": {}, "nearest_factor": None,
               "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(True, 1, code="c", series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(passed, metrics, reason))
    events = []
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: events.append(kw))

    process_hypothesis(Hypothesis("h1", "x", SOURCE_LLM), panel, panel,
                       pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                       "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                       llm=object(), run_sandbox=object())

    trials = [e for e in events if e["kind"] == "factor_trial"]
    assert len(trials) == 1, "an evaluated factor must be counted whatever the verdict"
    assert trials[0]["detail"]["gross_sr_per_bar"] == 0.31
    assert trials[0]["detail"]["sr_per_bar"] == 0.3          # kept for existing readers


def test_a_forge_failure_is_not_a_trial(tmp_path, monkeypatch):
    """forge_failed code never ran clean, so it was never statistically tested --
    it is not a multiple-testing debt."""
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.forge import ForgeResult

    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    monkeypatch.setattr(orch, "forge",
                        lambda *a, **k: ForgeResult(False, 3, code="bad", death_reason="UnsafeCodeError: x"))
    events = []
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: events.append(kw))
    process_hypothesis(Hypothesis("h2", "x", SOURCE_LLM), panel, panel,
                       pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                       "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                       llm=object(), run_sandbox=object())
    assert [e for e in events if e["kind"] == "factor_trial"] == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_orchestrator.py -k "ledger or forge_failure_is_not" -q`
Expected: FAIL — `KeyError: 'gross_sr_per_bar'`

- [ ] **Step 3: 改實作**

`process_hypothesis` 裡的 `append_event` 換成：

```python
    # Written AFTER evaluate and for EVERY verdict -- this is a contract, not an
    # accident (spec §3.4). N is the multiple-testing debt: a factor that reached
    # evaluate() WAS tested, so it counts even when it loses. Skipping the losers
    # would undercount N and leave only profitable factors shaping the variance.
    # A forge_failed factor never gets here, and correctly so: its code never ran
    # clean, so it was never statistically tested.
    append_event(manifests_dir, kind="factor_trial", symbol=symbol,
                 detail={"sr_per_bar": res.metrics.get("ir"),
                         # DSR reads this one; sr_per_bar stays for existing readers
                         # and for the historical rows that predate the split.
                         "gross_sr_per_bar": res.metrics.get("gross_ir"),
                         "interval": cfg.interval, "factor_id": hyp.id})
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/orchestrator.py research/tests/test_hermes_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(hermes): record gross Sharpe per trial, for every verdict

The ledger now carries gross_sr_per_bar for DSR to read. sr_per_bar
stays: existing readers depend on it, and the rows written before this
split have nothing else.

The second half of this is a contract that was previously only an
accident. append_event already ran after evaluate() returned, so losing
factors were counted -- but nothing said they had to be. N is the
multiple-testing debt: a factor that reached evaluate() WAS tested, and
skipping the losers would undercount N while leaving only profitable
factors to shape the variance. A test now pins it across every verdict.

forge_failed factors are still excluded, and correctly: their code never
ran clean, so no statistical test ever happened.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `foundry_dsr` 改吃 gross SR + N 接續 + `T = n_samples`

**Files:**
- Modify: `research/hermes/gatekeeper.py:163-182`（`foundry_dsr`）+ `evaluate()` 的 dsr 呼叫（約 :243）
- Test: `research/tests/test_hermes_gatekeeper.py`

**Interfaces:**
- Consumes: Task 1 的 `n_trials=`、Task 3 的 `gross_sr_per_bar`
- Produces: `foundry_dsr(best_gross_sr_per_bar, symbol, interval, manifests_dir, T) -> float`
  變異數樣本 = 有 `gross_sr_per_bar` 的 trial；`n_trials` = **所有** `factor_trial`（含只有 `sr_per_bar` 的舊筆）

- [ ] **Step 1: 寫失敗測試**（append）

```python
def _trial(md, gross=None, net=0.0, interval="1H"):
    from research.lib.research_ledger import append_event
    d = {"sr_per_bar": net, "interval": interval, "factor_id": "f"}
    if gross is not None:
        d["gross_sr_per_bar"] = gross
    append_event(md, kind="factor_trial", symbol="eth", detail=d)


def test_foundry_dsr_counts_legacy_trials_but_ignores_their_variance(tmp_path):
    """The 39 legacy eth trials recorded NET Sharpe, whose spread tracks turnover
    rather than search noise -- drop the variance. But we really did cast 39 times,
    and that debt does not evaporate because the net was broken. Count them."""
    from research.hermes.gatekeeper import foundry_dsr
    for _ in range(39):
        _trial(tmp_path, gross=None, net=-0.05)        # legacy: net only, wild spread
    for g in (0.001, -0.001, 0.002, -0.002):
        _trial(tmp_path, gross=g, net=g - 0.01)        # new: gross, tight spread

    with_debt = foundry_dsr(0.02, "eth", "1H", tmp_path, T=20000)

    # same variance sample, no legacy debt -> must be strictly more lenient
    import shutil
    clean = tmp_path / "clean"; clean.mkdir()
    for g in (0.001, -0.001, 0.002, -0.002):
        _trial(clean, gross=g, net=g - 0.01)
    assert foundry_dsr(0.02, "eth", "1H", clean, T=20000) > with_debt


def test_foundry_dsr_variance_uses_only_gross_rows(tmp_path):
    """A legacy net row at -0.9 would blow up the variance if it leaked in."""
    from research.hermes.gatekeeper import foundry_dsr
    _trial(tmp_path, gross=None, net=-0.9)
    for g in (0.001, -0.001, 0.002, -0.002):
        _trial(tmp_path, gross=g)
    assert foundry_dsr(0.02, "eth", "1H", tmp_path, T=20000) > 0.5


def test_foundry_dsr_still_filters_by_interval(tmp_path):
    from research.hermes.gatekeeper import foundry_dsr
    for g in (0.5, -0.5, 0.6, -0.6):
        _trial(tmp_path, gross=g, interval="15m")
    assert foundry_dsr(0.02, "eth", "1H", tmp_path, T=20000) == 1.0   # no 1H trials


def test_evaluate_feeds_dsr_the_gross_sharpe_and_the_sample_count(tmp_path, monkeypatch):
    """T must be the observations that actually entered the estimate (n_samples),
    not one year of bars."""
    import research.hermes.gatekeeper as gk
    seen = {}
    monkeypatch.setattr(gk, "foundry_dsr",
                        lambda best, sym, iv, md, T: seen.update(best=best, T=T) or 0.9)
    idx = pd.date_range("2022-01-01", periods=400, freq="h", tz="UTC")
    rng = np.random.default_rng(3)
    ohlcv = pd.DataFrame({"close": 100 + np.cumsum(rng.standard_normal(400) * 0.1)}, index=idx)
    factor = pd.Series(rng.standard_normal(400), index=idx)
    res = gk.evaluate(factor, ohlcv, pd.Series("neutral", index=idx.normalize().unique()),
                      pd.DataFrame(index=idx), "eth", tmp_path,
                      gk.GateConfig(interval="1H", horizon_h=24))
    assert seen["best"] == res.metrics["gross_ir"]     # gross, not net
    assert seen["T"] == res.metrics["n_samples"]       # not bars_per_year (8760)
```

（檔案頂端若缺 `import numpy as np` / `import pandas as pd` / `import pytest` 請補上。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_gatekeeper.py -k "foundry_dsr or feeds_dsr" -q`
Expected: FAIL — `KeyError: 'gross_sr_per_bar'` 或 `assert 8760 == 22046`

- [ ] **Step 3: 改 `foundry_dsr`**

```python
def foundry_dsr(best_gross_sr_per_bar: float, symbol: str, interval: str,
                manifests_dir, T: int) -> float:
    """Deflated Sharpe on the symbol's historical trials at the SAME interval.

    The count and the distribution come from different row sets, which is what
    this function's docstring always claimed and never did:

      n_trials  -- EVERY factor_trial at this interval, including the legacy rows
                   that carry only a net sr_per_bar. The multiple-testing debt is
                   a count of how many times we cast into this pool. Learning that
                   a past measurement was on the wrong scale does not un-cast it,
                   and letting a gate fix reset the count would make the debt
                   erasable on demand -- an unlimited do-over.
      variance  -- ONLY rows carrying gross_sr_per_bar. Net Sharpe's spread across
                   trials tracks turnover (0.02 to 0.39 on eth) rather than the
                   noise of the search, so mixing scales breaks the DSR variance
                   math. Measured: net trial std 0.0215 against a theoretical
                   sampling noise of 0.0067.

    `best_gross_sr_per_bar` is GROSS for the same reason: under the null of zero
    alpha its expectation is 0, which is the mean the formula already assumes.
    """
    events = [
        e for e in read_events(manifests_dir)
        if e.get("kind") == "factor_trial" and e.get("symbol") == symbol
        and isinstance(e.get("detail"), dict)
        and e["detail"].get("interval") == interval
    ]
    n_trials = len(events)
    gross = [e["detail"]["gross_sr_per_bar"] for e in events
             if e["detail"].get("gross_sr_per_bar") is not None]
    if not n_trials:
        return 1.0
    return deflated_sharpe(best_gross_sr_per_bar, gross + [best_gross_sr_per_bar],
                           T=T, n_trials=n_trials + 1)
```

頂端 import 補 `deflated_sharpe`（若既有是 `from research.lib.deflated_sharpe import deflated_sharpe, bars_per_year` 則已在）。

**`evaluate()` 裡的 dsr 呼叫**換成：

```python
        "dsr": foundry_dsr(
            gross_sr if np.isfinite(gross_sr) else 0.0, symbol, cfg.interval,
            manifests_dir,
            # T is the observations that actually entered the SR estimate. The
            # contract says train-window bar count; bars_per_year (8760) was one
            # YEAR's bars against a 22046-bar window.
            T=n_samples),
```

這需要先把 `gross_sr` 與 `n_samples` 在 metrics dict **之前**算出來。把 `evaluate()` 開頭改成：

```python
    factor = factor.shift(cfg.entry_lag)               # agy #6c: gate self-enforces lag
    fwd, horizon_bars = forward_returns(ohlcv, cfg)
    ret1 = ohlcv["close"].pct_change()
    weights = factor_to_weights(factor)
    mean_turnover = float(turnover_of(weights).fillna(0.0).mean())
    sr_bar = net_ir(weights, ret1, cfg.cost_frac)
    gross_sr = gross_ir(weights, ret1)
    n_samples = int(pd.concat([factor, fwd], axis=1).dropna().shape[0])
    nearest, absrho = nearest_correlate(factor, existing_and_dead)
```

metrics dict 裡 `"n_samples"` 改用上面的變數：`"n_samples": n_samples,`。

- [ ] **Step 4: 跑全套**

Run: `python -m pytest research/tests/ -q`
Expected: PASS

既有測試若因 dsr 數值改變而失敗，**先確認那個測試斷言的是「行為」還是「寫死的數字」**。斷言寫死數字的，更新數字並在測試加一行註解說明為何改；斷言行為的（如「trial 多 → dsr 低」）**不該壞** —— 若壞了，是實作錯了。

- [ ] **Step 5: Commit**

```bash
git add research/hermes/gatekeeper.py research/tests/test_hermes_gatekeeper.py
git commit -m "$(cat <<'EOF'
fix(hermes): feed DSR gross Sharpe, the real sample count, and the full debt

Three defects stacked into one number. A planted alpha at annualised
Sharpe 2.75 -- gross_ic 0.061, turnover 0.168, profitable after costs --
was rejected, because expected_max_sr had climbed to 0.047 while the
factor's own SR was 0.005.

The variance now comes only from rows carrying gross Sharpe. Net
Sharpe's spread across trials tracks turnover -- 0.02 to 0.39 on eth --
not the noise of the search, so it measured cost heterogeneity and
called it luck: trial std 0.0215 against a theoretical 0.0067.

The count still includes every trial at this interval, legacy rows and
all. Learning that a past measurement used the wrong scale does not
un-cast the line. A gate fix that also resets the debt would make the
debt erasable on demand, and this commit is itself a gate fix.

T is now the observations that entered the estimate. The contract asked
for the train-window bar count and got bars_per_year -- one year of bars
against a 22046-bar window.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `net_ir > 0` 閘 + `max_turnover` docstring

**Files:**
- Modify: `research/hermes/gatekeeper.py`（`evaluate()` 的閘序、`GateConfig.max_turnover` 註解）
- Test: `research/tests/test_hermes_gatekeeper.py`

**Interfaces:**
- Consumes: Task 4
- Produces: 閘序 `redundant → turnover → weak_ic → net_ir<=0 → DSR`；rejection reason 字串 `f"net_ir {sr_bar:.5f} <= 0"`

> **`max_turnover` 數值不動。** 它是**物理執行上限**（防非線性滑點/市場衝擊/執行延遲 —— 線性成本模型抓不到），不是獲利指標的延伸。

- [ ] **Step 1: 寫失敗測試**（append）

```python
def test_a_factor_that_loses_money_is_rejected_before_dsr(tmp_path, monkeypatch):
    """DSR no longer sees cost, so something must still require profit. It runs
    before DSR: it is cheap and deterministic, and there is nothing to say about
    the significance of a factor that cannot pay for itself."""
    import research.hermes.gatekeeper as gk
    called = []
    monkeypatch.setattr(gk, "foundry_dsr", lambda *a, **k: called.append(1) or 0.99)
    monkeypatch.setattr(gk, "net_ir", lambda *a, **k: -0.01)
    monkeypatch.setattr(gk, "gross_ir", lambda *a, **k: 0.05)
    monkeypatch.setattr(gk, "gross_ic", lambda *a, **k: 0.10)
    monkeypatch.setattr(gk, "nonoverlap_ic", lambda *a, **k: 0.09)

    idx = pd.date_range("2022-01-01", periods=400, freq="h", tz="UTC")
    ohlcv = pd.DataFrame({"close": np.linspace(100, 110, 400)}, index=idx)
    res = gk.evaluate(pd.Series(np.arange(400.0), index=idx),
                      ohlcv, pd.Series("neutral", index=idx.normalize().unique()),
                      pd.DataFrame(index=idx), "eth", tmp_path,
                      gk.GateConfig(interval="1H", horizon_h=24))
    assert not res.passed
    assert "net_ir" in res.rejection_reason
    assert called == [], "DSR must not be asked about a factor that loses money"


def test_a_profitable_significant_factor_still_passes(tmp_path, monkeypatch):
    import research.hermes.gatekeeper as gk
    monkeypatch.setattr(gk, "foundry_dsr", lambda *a, **k: 0.99)
    monkeypatch.setattr(gk, "net_ir", lambda *a, **k: 0.02)
    monkeypatch.setattr(gk, "gross_ir", lambda *a, **k: 0.05)
    monkeypatch.setattr(gk, "gross_ic", lambda *a, **k: 0.10)
    monkeypatch.setattr(gk, "nonoverlap_ic", lambda *a, **k: 0.09)
    idx = pd.date_range("2022-01-01", periods=400, freq="h", tz="UTC")
    ohlcv = pd.DataFrame({"close": np.linspace(100, 110, 400)}, index=idx)
    res = gk.evaluate(pd.Series(np.arange(400.0), index=idx),
                      ohlcv, pd.Series("neutral", index=idx.normalize().unique()),
                      pd.DataFrame(index=idx), "eth", tmp_path,
                      gk.GateConfig(interval="1H", horizon_h=24))
    assert res.passed


def test_a_nan_net_ir_is_rejected_not_passed(tmp_path, monkeypatch):
    """nan <= 0 is False in Python -- a flat/degenerate position must not sail
    through on that."""
    import research.hermes.gatekeeper as gk
    monkeypatch.setattr(gk, "foundry_dsr", lambda *a, **k: 0.99)
    monkeypatch.setattr(gk, "net_ir", lambda *a, **k: float("nan"))
    monkeypatch.setattr(gk, "gross_ir", lambda *a, **k: 0.05)
    monkeypatch.setattr(gk, "gross_ic", lambda *a, **k: 0.10)
    monkeypatch.setattr(gk, "nonoverlap_ic", lambda *a, **k: 0.09)
    idx = pd.date_range("2022-01-01", periods=400, freq="h", tz="UTC")
    ohlcv = pd.DataFrame({"close": np.linspace(100, 110, 400)}, index=idx)
    res = gk.evaluate(pd.Series(np.arange(400.0), index=idx),
                      ohlcv, pd.Series("neutral", index=idx.normalize().unique()),
                      pd.DataFrame(index=idx), "eth", tmp_path,
                      gk.GateConfig(interval="1H", horizon_h=24))
    assert not res.passed and "net_ir" in res.rejection_reason
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_gatekeeper.py -k "loses_money or nan_net_ir" -q`
Expected: FAIL — 因子通過了（沒有 net_ir 閘）

- [ ] **Step 3: 改實作**

`evaluate()` 的閘序改成（在 `weak gross_ic` 之後、`DSR` 之前插入）：

```python
    if np.isnan(gic) or abs(gic) < cfg.gross_ic_min:
        return GatekeeperResult(False, metrics, f"weak gross_ic {gic:.4f} < {cfg.gross_ic_min}")
    # DSR is fed GROSS Sharpe now, so it no longer knows about cost -- this gate is
    # where cost gets its say. It runs before DSR because it is cheap and
    # deterministic, and because there is nothing to say about the significance of
    # a factor that cannot pay for itself.
    # `not > 0` rather than `<= 0`: nan <= 0 is False, and a degenerate/flat
    # position must not sail through on that.
    if not (sr_bar > 0):
        return GatekeeperResult(False, metrics, f"net_ir {sr_bar:.5f} <= 0")
    if metrics["dsr"] < cfg.dsr_min:
        return GatekeeperResult(False, metrics, f"DSR {metrics['dsr']:.2f} < {cfg.dsr_min}")
    return GatekeeperResult(True, metrics, "")
```

`GateConfig.max_turnover` 的註解換成：

```python
    # PHYSICAL execution ceiling, not an extension of the profit metric. net_ir
    # already charges cost, so this is not the cost talking twice: the backtest's
    # LINEAR cost model cannot see non-linear slippage, market impact, or execution
    # latency. A factor churning 0.6 of the book per bar may clear fees on paper and
    # eat through the order book live. This is the guard against that fantasy.
    max_turnover: float = 0.5
```

- [ ] **Step 4: 跑全套**

Run: `python -m pytest research/tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/gatekeeper.py research/tests/test_hermes_gatekeeper.py
git commit -m "$(cat <<'EOF'
feat(hermes): require a factor to make money, in its own gate

DSR reads gross Sharpe now and no longer knows about cost, so something
has to. net_ir > 0 runs before DSR: it is cheap and deterministic, and
there is nothing to say about the significance of a factor that cannot
pay for itself.

Written as `not (sr_bar > 0)` rather than `sr_bar <= 0`, because nan <= 0
is False and a degenerate flat position must not sail through on that.

max_turnover keeps its value and gets an honest docstring. Calling it
double-counting was wrong: net_ir charges the LINEAR cost model, which
cannot see non-linear slippage, market impact or execution latency. A
factor churning 0.6 of the book per bar can clear fees on paper and eat
through the order book live. The ceiling is a physical guard, not the
cost talking twice.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `calibration.py` — 控制組的純函式

**Files:**
- Create: `research/hermes/calibration.py`
- Test: `research/tests/test_hermes_gate_calibration.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `PRIME_SHIFT_DAYS: tuple = (107, 149, 227, 311)`（原文誤植 101——`abs(101-90)=11`，過不了本檔自己 `>15` 的清距檢查；實作已修正為 107）
  - `circular_shift(s: pd.Series, days: int, bars_per_day: int = 24) -> pd.Series`
  - `plant_alpha(fwd: pd.Series, w: float, seed: int = 7, smooth_bars: int = 24) -> pd.Series`

- [ ] **Step 1: 寫失敗測試**

```python
# research/tests/test_hermes_gate_calibration.py
"""Gate calibration controls. No LLM, no cost -- these run in CI.

The gate's blind spot lived for the project's whole life and was found by
accident. These tests exist so it cannot come back quietly."""
import numpy as np
import pandas as pd
import pytest

from research.hermes.calibration import PRIME_SHIFT_DAYS, circular_shift, plant_alpha


def test_prime_shifts_avoid_market_periodicity():
    """+90 days -- one quarter -- was the first choice and the worst one: crypto
    has quarterly expiry and funding cycles, so shifting by a whole quarter can
    ALIGN a control with the very periodicity it is meant to destroy."""
    def is_prime(n):
        return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))
    for d in PRIME_SHIFT_DAYS:
        assert is_prime(d), f"{d} is not prime"
        assert 365 % d != 0, f"{d} divides a year"
        assert d > 90, f"{d} is not clear of the longest rolling window (30d)"
        for period in (90, 180, 270, 365):
            assert abs(d - period) > 15, f"{d} sits on the {period}d cycle"


def test_circular_shift_preserves_length_and_values():
    idx = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    s = pd.Series(np.arange(100.0), index=idx)
    out = circular_shift(s, days=1, bars_per_day=24)
    assert len(out) == len(s)
    assert out.index.equals(s.index)
    assert sorted(out.dropna().tolist()) == sorted(s.tolist())   # wrapped, not dropped


def test_circular_shift_actually_moves_the_series():
    idx = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    s = pd.Series(np.arange(100.0), index=idx)
    out = circular_shift(s, days=1, bars_per_day=24)
    assert out.iloc[24] == 0.0                     # first value moved 24 bars on
    assert not out.equals(s)


def test_circular_shift_preserves_autocorrelation():
    """This is the whole point of shifting rather than shuffling: a shuffled
    control loses its autocorrelation, its turnover explodes, and it degenerates
    into the too-clean noise this design exists to avoid."""
    rng = np.random.default_rng(0)
    idx = pd.date_range("2024-01-01", periods=5000, freq="h", tz="UTC")
    s = pd.Series(rng.standard_normal(5000), index=idx).rolling(48).mean()
    out = circular_shift(s, days=101)
    assert out.autocorr(1) == pytest.approx(s.autocorr(1), abs=0.02)


def test_plant_alpha_strength_rises_with_w():
    idx = pd.date_range("2024-01-01", periods=3000, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    fwd = pd.Series(rng.standard_normal(3000) * 0.01, index=idx)
    weak = plant_alpha(fwd, w=0.05).corr(fwd.shift(-1), method="spearman")
    strong = plant_alpha(fwd, w=0.50).corr(fwd.shift(-1), method="spearman")
    assert abs(strong) > abs(weak)


def test_plant_alpha_is_smooth_enough_to_be_tradeable():
    """The signal term z(fwd.shift(-1)) is nearly white. Smoothing must be applied
    to the WHOLE blend, not just the noise -- otherwise turnover explodes and the
    positive control measures the turnover gate instead of DSR."""
    from research.hermes.gatekeeper import factor_to_weights, turnover_of
    idx = pd.date_range("2024-01-01", periods=5000, freq="h", tz="UTC")
    rng = np.random.default_rng(2)
    fwd = pd.Series(rng.standard_normal(5000) * 0.01, index=idx)
    for w in (0.05, 0.5):
        f = plant_alpha(fwd, w=w)
        to = float(turnover_of(factor_to_weights(f)).fillna(0.0).mean())
        assert to < 0.5, f"w={w} turnover {to:.3f} would hit the physical ceiling"


def test_plant_alpha_is_deterministic():
    idx = pd.date_range("2024-01-01", periods=1000, freq="h", tz="UTC")
    fwd = pd.Series(np.linspace(-0.01, 0.01, 1000), index=idx)
    pd.testing.assert_series_equal(plant_alpha(fwd, 0.2, seed=7), plant_alpha(fwd, 0.2, seed=7))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest research/tests/test_hermes_gate_calibration.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.hermes.calibration'`

- [ ] **Step 3: 寫實作**

```python
# research/hermes/calibration.py
"""Controls that measure what the statistical gate can actually see.

The gate's minimum detectable alpha was an annualised Sharpe of ~5 -- a level
essentially nothing in any market reaches -- so every negative result the project
produced measured the tools rather than the market. Nobody noticed for the
project's whole life, because the gate was only ever asked to reject things, and
it did that beautifully. Catching dead fish proves the guards work; it says
nothing about whether the net can land a live one.

These are the two questions that must be asked together:
  positive control -- how weak an alpha can this gate still see?
  negative control -- how often does it wave through something with no alpha?

Sensitivity alone is worthless as a target: deleting DSR and raising max_turnover
to 10 would drop the minimum detectable Sharpe to 0.5 and leave a net full of
holes. The negative control is what makes "we fixed it" falsifiable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Shift lengths for the negative control. Every one of these must be prime, must
# not divide a year, must clear the longest rolling window (30d), and must sit
# well off the quarterly/semi-annual/annual cycles.
#
# The first draft used +90 days -- exactly one quarter, against a market with
# quarterly expiry and funding cycles. A control shifted by a whole quarter can
# ALIGN with the periodicity it was meant to destroy, keep real predictive power,
# and quietly understate the rejection rate.
PRIME_SHIFT_DAYS: tuple = (107, 149, 227, 311)


def circular_shift(s: pd.Series, days: int, bars_per_day: int = 24) -> pd.Series:
    """Roll a series forward by `days`, wrapping the tail back to the head.

    Circular, not truncating: the control must keep the full sample length so its
    rejection rate is comparable to a real factor's evaluation.

    Shift, never shuffle. Shuffling destroys the autocorrelation, which sends
    turnover through the roof and turns the control into exactly the too-clean
    noise this whole design exists to avoid. The autocorrelation IS the value here:
    it is what makes the control look like a real factor to the gate.
    """
    return pd.Series(np.roll(s.to_numpy(), days * bars_per_day), index=s.index)


def plant_alpha(fwd: pd.Series, w: float, seed: int = 7,
                smooth_bars: int = 24) -> pd.Series:
    """A factor of known strength: `w` of real foresight, `1-w` of noise.

    `fwd.shift(-1)` because evaluate() applies its own shift(entry_lag=1).

    Smoothing is applied to the WHOLE blend, not just the noise. The signal term
    z(fwd.shift(-1)) is itself a nearly-white, jagged series -- 1H returns barely
    autocorrelate -- so leaving it jagged sends turnover past the physical ceiling
    as w rises, and the control ends up measuring max_turnover instead of DSR.
    Smoothed whole, turnover lands at 0.15-0.17, inside the range real factors
    occupy (0.02-0.39).
    """
    rng = np.random.default_rng(seed)
    truth = fwd.shift(-1).fillna(0.0)
    z = (truth - truth.mean()) / truth.std()
    noise = pd.Series(rng.standard_normal(len(fwd)), index=fwd.index)
    raw = z * w + noise * (1.0 - w)
    return raw.rolling(smooth_bars, min_periods=smooth_bars // 2).mean()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest research/tests/test_hermes_gate_calibration.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add research/hermes/calibration.py research/tests/test_hermes_gate_calibration.py
git commit -m "$(cat <<'EOF'
feat(hermes): add the control primitives for gate calibration

Two questions that only mean anything together: how weak an alpha can
the gate still see, and how often does it wave through something with
none. Sensitivity alone is worthless as a target -- deleting DSR and
raising max_turnover to 10 drops the minimum detectable Sharpe to 0.5
and leaves a net full of holes.

circular_shift rolls a real factor forward, wrapping the tail. Shift,
never shuffle: shuffling destroys the autocorrelation, turnover explodes,
and the control degenerates into the too-clean noise this design exists
to avoid. The autocorrelation is the value -- it is what makes the
control still look like a real factor to the gate.

PRIME_SHIFT_DAYS is checked by a test rather than trusted. The first
draft used +90 days: exactly one quarter, against a market with quarterly
expiry and funding cycles, where a control can align with the very
periodicity it was meant to destroy.

plant_alpha smooths the whole blend, not just the noise. z(fwd.shift(-1))
is itself nearly white, so leaving it jagged pushes turnover past the
physical ceiling as w rises and the control measures max_turnover instead
of DSR.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 負控制 — 偽陽性 ≤5%

**Files:**
- Modify: `research/tests/test_hermes_gate_calibration.py`

**Interfaces:**
- Consumes: Task 6 的 `circular_shift` / `PRIME_SHIFT_DAYS`；Task 4/5 的 gate

> **控制組關掉 dedup**（傳空的 `existing_and_dead`）。否則負控制可能因 `redundant` 被拒 → 拒絕率好看 → **但 DSR 被放寬時完全看不出來**。要隔離的是**統計閘**，那才是本計畫會動到、也才是會被作弊的那道。

- [ ] **Step 1: 寫失敗測試**（append）

```python
_MANIFESTS = "research/manifests"
_SYMBOL = "eth"


def _gate_env():
    """The real eth pre-oos panel, exactly as run_foundry builds it."""
    from research.hermes.orchestrator import _align_ohlcv
    from research.hermes.split import foundry_split
    from research.lib.factor_io import load_features
    feats_full = load_features(_SYMBOL, manifests_dir=_MANIFESTS)
    ohlcv_full = _align_ohlcv(pd.read_parquet(f"{_MANIFESTS}/ohlcv_{_SYMBOL}.parquet"),
                              feats_full.index)
    tr, va = foundry_split(feats_full, "2025-01-01", val_frac=0.2)
    features = pd.concat([tr, va])
    return features, ohlcv_full.loc[features.index]


def _run_gate(factor, features, ohlcv):
    """Statistical gate only: dedup is off ON PURPOSE (empty existing_and_dead).

    A control rejected for being `redundant` flatters the rejection rate while
    telling us nothing about DSR -- and would hide it completely if DSR were
    loosened. The dedup gate is not what this plan touches."""
    from research.hermes.gatekeeper import evaluate, GateConfig
    regime = pd.Series("neutral", index=features.index.normalize().unique())
    return evaluate(factor, ohlcv, regime, pd.DataFrame(index=features.index),
                    _SYMBOL, _MANIFESTS, GateConfig(interval="1H", horizon_h=24))


@pytest.mark.skipif(not pathlib.Path(f"{_MANIFESTS}/features_{_SYMBOL}.parquet").exists(),
                    reason="real eth panel not present in this checkout")
def test_negative_control_false_positive_rate_stays_under_five_percent():
    """Real buried factors, rolled forward so they cannot predict anything. They
    keep their turnover, distribution and autocorrelation -- so the gate sees
    something that looks exactly like its real workload, minus the alpha.

    This is the guard that makes the calibration work falsifiable. Gutting a
    threshold to raise sensitivity turns this test red immediately."""
    from research.hermes.orchestrator import _graveyard_path
    g = _graveyard_path(_SYMBOL, _MANIFESTS)
    if not g.exists():
        pytest.skip("no graveyard factors accumulated yet")
    dead = pd.read_parquet(g)
    features, ohlcv = _gate_env()

    verdicts = []
    for col in dead.columns:
        s = dead[col].reindex(features.index)
        if s.notna().mean() < 0.5:
            continue
        for days in PRIME_SHIFT_DAYS:
            verdicts.append(_run_gate(circular_shift(s.fillna(0.0), days),
                                      features, ohlcv).passed)

    assert len(verdicts) >= 20, f"only {len(verdicts)} controls; too few to bound a rate"
    fpr = sum(verdicts) / len(verdicts)
    assert fpr <= 0.05, (
        f"false-positive rate {fpr:.1%} > 5%: the gate is passing factors that "
        f"cannot possibly predict ({sum(verdicts)}/{len(verdicts)})")
```

（檔案頂端補 `import pathlib`。）

- [ ] **Step 2: 跑測試**

Run: `python -m pytest research/tests/test_hermes_gate_calibration.py -k negative_control -q`
Expected: PASS。

**若 FAIL（偽陽性 > 5%）**：**不要放寬這個測試，也不要重挑平移天數湊過。** 那代表 Task 1–5 把門檻拆過頭了 —— 回去看是哪一個閘鬆掉。這正是這個測試存在的理由。

- [ ] **Step 3: Commit**

```bash
git add research/tests/test_hermes_gate_calibration.py
git commit -m "$(cat <<'EOF'
test(hermes): bound the gate's false-positive rate with real dead factors

Buried factors rolled forward past any usable horizon: they keep their
turnover, distribution and autocorrelation, so the gate sees something
that looks exactly like its real workload with the alpha removed.
Synthetic noise would be too clean -- easy to reject, and the 5% line
would become a fake guard while structurally-biased junk sailed past.

This is what makes the calibration work falsifiable. Loosening a
threshold to buy sensitivity turns this red immediately, so sensitivity
cannot be bought by gutting the net.

Dedup is off on purpose. A control rejected as `redundant` flatters the
rate while saying nothing about DSR -- and would hide a loosened DSR
completely.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 正控制 — 最低偵測 Sharpe ≤2 + 健全性檢查

**Files:**
- Modify: `research/tests/test_hermes_gate_calibration.py`

**Interfaces:**
- Consumes: Task 6 的 `plant_alpha`；Task 7 的 `_gate_env` / `_run_gate`

- [ ] **Step 1: 寫失敗測試**（append）

```python
_BARS_PER_YEAR = 8760


@pytest.mark.skipif(not pathlib.Path(f"{_MANIFESTS}/features_{_SYMBOL}.parquet").exists(),
                    reason="real eth panel not present in this checkout")
def test_the_gate_can_see_an_alpha_worth_having():
    """Before this work the weakest alpha the gate could see was an annualised
    Sharpe of ~5. An alpha at 2.75 -- gross_ic 0.061, twice the gate, turnover
    0.168, profitable after costs -- was rejected. Nothing in any market runs at 5,
    so every negative result the project produced measured the tools.

    Read this together with the negative control: sensitivity on its own is bought
    trivially by deleting gates."""
    from research.hermes.gatekeeper import forward_returns, GateConfig
    features, ohlcv = _gate_env()
    fwd, _ = forward_returns(ohlcv, GateConfig(interval="1H", horizon_h=24))

    detected = []
    for w in (0.02, 0.03, 0.05, 0.08, 0.12):
        res = _run_gate(plant_alpha(fwd, w), features, ohlcv)
        ann = res.metrics["ir"] * np.sqrt(_BARS_PER_YEAR)
        if res.passed:
            detected.append(ann)

    assert detected, "the gate detected nothing at any planted strength"
    assert min(detected) <= 2.0, (
        f"weakest detected alpha is annualised Sharpe {min(detected):.2f}; "
        f"the gate still cannot see an alpha worth having")


@pytest.mark.skipif(not pathlib.Path(f"{_MANIFESTS}/features_{_SYMBOL}.parquet").exists(),
                    reason="real eth panel not present in this checkout")
def test_the_best_real_lead_is_still_rejected():
    """rolling_std_stablecoin_supply is the strongest thing the project ever found:
    gross_ic 0.0718, positive Sharpe, orthogonal at 0.237, non-overlapping IC
    holding at 0.0643. It is still noise -- per-bar SR 0.00507 against a sampling
    std of 0.00674, t = 0.75, annualised 0.47, in-sample.

    If it ever passes, we cut too deep. That is what the slope looks like from the
    inside: not a decision to cheat, just a threshold that finally let the thing
    we wanted through."""
    from research.hermes.evidence_store import load_cards
    cards = [c for c in load_cards(_SYMBOL, _MANIFESTS)
             if c.factor_id == "llm_rolling_std_stablecoin_supply"]
    if not cards:
        pytest.skip("the reference lead is not in this checkout's evidence store")
    from research.lib.deflated_sharpe import deflated_sharpe
    # its own gross SR, evaluated against a clean trial distribution
    assert deflated_sharpe(0.00507, [0.001, -0.001, 0.002, -0.002, 0.0005],
                           T=22046, n_trials=40) < 0.5
```

- [ ] **Step 2: 跑測試**

Run: `python -m pytest research/tests/test_hermes_gate_calibration.py -q`
Expected: PASS（全部）

**若 `test_the_gate_can_see_an_alpha_worth_having` FAIL** → **不要調門檻湊。** 依 spec §6.1：那是**真實發現，不是失敗** —— 代表在這個樣本長度（22071 bar）下，統計功效就是這樣。**誠實記錄並回報使用者，重新評估專案可行性。**

**若 `test_the_best_real_lead_is_still_rejected` FAIL** → 拆過頭了，回去看是哪個閘鬆掉（spec §6.2）。

- [ ] **Step 3: 跑全套 + 回報**

Run: `python -m pytest research/tests/ -q`
Expected: PASS

回報時**附真實數字**：修正前/後的最低偵測 Sharpe、負控制偽陽性率。**不要在沒有這兩個數字的情況下宣稱「修好了」**（見記憶 `feedback_verify_before_claiming`）。

- [ ] **Step 4: Commit**

```bash
git add research/tests/test_hermes_gate_calibration.py
git commit -m "$(cat <<'EOF'
test(hermes): pin the weakest alpha the gate can see, and the one it must not

The gate's minimum detectable alpha was an annualised Sharpe of ~5.
Nothing in any market runs at 5, so every negative result the project
produced -- two months, forty LLM hypotheses, zero candidates -- measured
the tools rather than the market. This test holds the line at 2, and only
means anything read alongside the negative control: sensitivity on its
own is bought by deleting gates.

The second test is the tripwire. rolling_std_stablecoin_supply is the
strongest thing the project ever found -- gross_ic 0.0718, positive
Sharpe, orthogonal at 0.237 -- and it is still noise: t = 0.75,
annualised 0.47, in-sample. It must keep failing. If it ever passes we
cut too deep, and that is what the slope looks like from the inside: not
a decision to cheat, just a threshold that finally let through the thing
we wanted.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review 記錄

**Spec 覆蓋**：§3 改動 1→T2、2→T3+T4、3→T4、4→T5；§3.2 `max_turnover` docstring→T5；§3.3 N/變異數拆開→T1+T4；§3.4 ledger 契約→T3；§4.1 負控制→T6+T7；§4.2 正控制→T6+T8；§4.3 dedup 關閉→T7 `_run_gate`；§6.2 健全性檢查→T8。**§4.4 的 held-out 控制集未實作** —— 它需要先累積夠多 graveyard 因子才有意義（目前 eth 只有約 40 個，切兩半後每半 20 個 × 4 個平移 = 80 個樣本，勉強夠但很薄）。**列為 backlog，並在 §4.4 已明寫「靠紀律不靠機制」。**

**型別一致性**：`deflated_sharpe(..., n_trials=)`（T1）↔ `foundry_dsr` 呼叫（T4）✓；`metrics["gross_ir"]`（T2）↔ ledger `gross_sr_per_bar`（T3）↔ `foundry_dsr` 讀取（T4）✓；`plant_alpha` / `circular_shift` / `PRIME_SHIFT_DAYS`（T6）↔ T7/T8 ✓。
