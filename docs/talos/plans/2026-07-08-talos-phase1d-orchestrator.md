# Talos Phase 1D — Foundry Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Phase 1 收尾——把 1B（佇列）→1C（forge）→1A（守門）→1E（證據卡）串成一條 Foundry 生產線，加**預算 + early stopping**，並以 **write-file→reconcile** 觸發（Talos 寫 job，foundry runner 執行，**絕不 inline 跑**）。

**Architecture:** `research/hermes/orchestrator.py`。核心：`process_hypothesis`（單因子全程）+ `run_foundry`（掃佇列、控預算/early-stop）+ job 觸發層（`enqueue_foundry_job` 寫 `runs/foundry_jobs/<id>/job.json`；`run_foundry_job` reconcile 執行）。**`run_sandbox` 注入層**把 `DockerSandbox.run` 包成 forge 要的 `run(code, panel)->Series`（寫 panel→parquet、跑沙盒、讀 candidate.parquet 回 Series）。

**Tech Stack:** Python 3.11、既有 `hermes`（forge/gatekeeper/hypothesis_queue/evidence_*/candidate_store/sandbox）+ `factor_io.load_features` + `research_ledger.append_event`。pytest research scope。

> **職責邊界**：1D 是唯一「串全部」的地方。1A/1B/1C/1E 都是被它調用的純元件。1D 也是 `factor_trial` ledger 事件的**寫入者**（1A 的 DSR 只讀）——順序關鍵：**先 `evaluate` 後寫 ledger**（本次 trial 供**未來**因子的 DSR，evaluate 內部已含當前 trial，見 1A agy-3 #3）。

**設計來源：** overview（1D + P5 nightly 預算）+ `talos-design.md` §3.5 write-file→reconcile + §3.6。

---

## 設計原則

1. **write-file→reconcile**（憲法鐵律）：nightly cron / on-demand 寫 `job.json`；foundry runner 撿起跑。Talos 決策與執行解耦，不擾動線上。
2. **1D 串接不重造**：forge（1C）/ evaluate（1A）/ build_queue（1B）/ write_candidate + upsert_card（1E）皆直接調用。
3. **每因子留痕**：pass→候選庫 + candidate 卡；fail→graveyard 卡 + 死因。**每次 evaluate 後寫 `factor_trial` ledger**（`sr_per_bar`+`interval`）供未來 DSR。
4. **有界（P5）**：`Budget`（最多因子數 + 每因子 max_retries）+ early stopping（前 N 個全爛→中止當晚，防發散燒 token）。

---

## File Structure

| 檔案 | 責任 |
|------|------|
| `research/hermes/orchestrator.py` | `make_run_sandbox` 注入 + `process_hypothesis` + `Budget`/early-stop + `run_foundry` + job 觸發/reconcile |
| `research/tests/test_hermes_orchestrator.py` | 全 fake（無 docker/LLM/網路）單測 |

---

## Task 1: `run_sandbox` 注入層（DockerSandbox → `run(code, panel)->Series`）

**Files:** Create `research/hermes/orchestrator.py`; Test `research/tests/test_hermes_orchestrator.py`. Reference: `sandbox.DockerSandbox.run(source, input_parquet, output_dir)->path`、`candidate_store`（atomic 慣例）。

forge/pit 需要 `run(code, panel)->Series`。此層：panel→temp parquet → `sandbox.run` → 讀 `candidate.parquet` 首欄 → 貼回 panel.index 的 Series。

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_orchestrator.py
import numpy as np
import pandas as pd
import pytest
from research.hermes.orchestrator import make_run_sandbox


class _FakeSandbox:
    """Stands in for DockerSandbox: 'runs' by writing a candidate parquet."""
    def __init__(self, fn): self.fn = fn
    def run(self, source, input_parquet, output_dir):
        panel = pd.read_parquet(input_parquet)
        out = pd.DataFrame({"candidate": self.fn(panel)})
        from pathlib import Path
        p = Path(output_dir) / "candidate.parquet"; out.to_parquet(p)
        return str(p)


def test_run_sandbox_roundtrips_panel_to_series(tmp_path):
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    sb = _FakeSandbox(lambda p: p["close"].pct_change(3))
    run = make_run_sandbox(sb, scratch_dir=tmp_path)
    s = run("def compute(df): ...", panel)
    assert isinstance(s, pd.Series)
    assert s.index.equals(panel.index)               # index restored from panel
    pd.testing.assert_series_equal(s, panel["close"].pct_change(3), check_names=False)
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
# research/hermes/orchestrator.py
"""Talos Foundry orchestrator (Phase 1D) — wires 1B->1C->1A->1E with budget +
early stopping, triggered write-file->reconcile (never inline)."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pandas as pd


def make_run_sandbox(sandbox, scratch_dir):
    """Adapt DockerSandbox.run into forge's run(code, panel)->Series.
    Writes panel to a temp parquet, runs the sandbox, reads the single-column
    candidate.parquet back and re-attaches panel's index."""
    scratch = Path(scratch_dir)

    def run(code: str, panel: pd.DataFrame) -> pd.Series:
        job = scratch / f"sbx_{uuid.uuid4().hex[:12]}"
        job.mkdir(parents=True, exist_ok=True)
        in_path = job / "in.parquet"
        panel.to_parquet(in_path)
        out_path = sandbox.run(code, input_parquet=str(in_path), output_dir=str(job))
        out = pd.read_parquet(out_path)
        series = out.iloc[:, 0]
        series.index = panel.index               # runner drops index; restore it
        return series

    return run
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): sandbox run(code,panel)->Series injection layer (Phase 1D)`

---

## Task 2: `process_hypothesis`（單因子全程 forge→evaluate→card→ledger）

**Files:** Modify `orchestrator.py` + test. Reference: `forge`（1C）、`gatekeeper.evaluate`/`GateConfig`（1A）、`evidence_card.EvidenceCard`、`evidence_store.upsert_card`、`candidate_store.write_candidate`、`research_ledger.append_event`。

流程：`forge` → 失敗寫 graveyard 卡 + 回。成功 → `evaluate` → **寫 `factor_trial` ledger**（供未來 DSR）→ 建 `EvidenceCard`（metrics 對映）→ pass **merge 進候選庫** + candidate 卡 / fail **merge 進墓地 parquet** + graveyard 卡。

> **agy 3b（致命）**：`write_candidate` 底層 `_atomic_to_parquet` 用 `os.replace` **整檔取代**，不是 append。迴圈中逐因子寫單欄 DataFrame → 每個新因子抹掉前一個，整晚只活最後一個。**必須 merge-on-write**（讀既有 → 加/換欄 → 原子寫）。
>
> **agy 3c（設計斷層）**：死因子的 `series` 從未落地（只存 `EvidenceCard`），所以 1A `nearest_correlate` 的**墓地數值比對從來沒接上**——C-6 憲法形同虛設。修正：死因子序列寫 `candidate_features/graveyard_<sym>.parquet`，`run_foundry` 載入併進 `existing_and_dead`（Task 4）。

- [ ] **Step 1: Failing test**

```python
def test_process_hypothesis_pass_writes_candidate_and_card(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult
    from research.hermes.evidence_store import load_cards

    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    series = pd.Series(np.arange(300.0), index=idx)

    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(True, 1, code="c", series=series))
    metrics = {"gross_ic": 0.05, "ic_nonoverlap": 0.04, "ir": 0.3, "dsr": 0.9, "pbo": None,
               "turnover": 0.1, "n_samples": 300, "regime_ic": {}, "yearly_ic": {},
               "nearest_factor": None, "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(True, metrics, ""))
    events = []
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: events.append(kw["kind"]))

    hyp = Hypothesis("h1", "mom", SOURCE_LLM)
    outcome = process_hypothesis(hyp, panel, panel, pd.Series("bull", index=idx),
                                 pd.DataFrame(index=idx), "eth", tmp_path,
                                 GateConfig(interval="1D", horizon_h=24),
                                 llm=object(), run_sandbox=object())
    assert outcome == "candidate"
    assert "factor_trial" in events                  # ledger written for future DSR
    cards = {c.factor_id: c for c in load_cards("eth", tmp_path)}
    assert cards["h1"].verdict == "candidate" and cards["h1"].gross_ic == 0.05


def test_process_hypothesis_forge_fail_writes_graveyard(tmp_path, monkeypatch):
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.forge import ForgeResult
    from research.hermes.evidence_store import load_cards
    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(False, 3, code="bad", death_reason="UnsafeCodeError: x"))
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)
    outcome = process_hypothesis(Hypothesis("h2", "x", SOURCE_LLM), panel, panel,
                                 pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                                 "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                                 llm=object(), run_sandbox=object())
    assert outcome == "forge_failed"
    assert load_cards("eth", tmp_path)[0].verdict == "graveyard"


def test_two_passing_factors_both_survive_in_candidate_parquet(tmp_path, monkeypatch):
    # agy 3b (fatal): write_candidate replaces the whole parquet — the loop must merge
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis
    from research.hermes.candidate_store import _candidate_path
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult
    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    metrics = {"gross_ic": 0.05, "ic_nonoverlap": 0.04, "ir": 0.3, "dsr": 0.9, "pbo": None,
               "turnover": 0.1, "n_samples": 300, "regime_ic": {}, "yearly_ic": {},
               "nearest_factor": None, "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(True, metrics, ""))
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)
    for fid in ("f1", "f2"):
        s = pd.Series(np.arange(300.0), index=idx, name=fid)
        monkeypatch.setattr(orch, "forge", lambda *a, s=s, **k: ForgeResult(True, 1, code="c", series=s))
        process_hypothesis(Hypothesis(fid, fid, SOURCE_LLM), panel, panel,
                           pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                           "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                           llm=object(), run_sandbox=object())
    cand = pd.read_parquet(_candidate_path("eth", tmp_path))
    assert set(cand.columns) == {"f1", "f2"}          # f1 NOT clobbered by f2


def test_rejected_factor_series_lands_in_graveyard_parquet(tmp_path, monkeypatch):
    # agy 3c: dead factor VALUES must persist so 1A can numerically dedup (C-6)
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import process_hypothesis, _graveyard_path
    from research.hermes.hypothesis import Hypothesis, SOURCE_LLM
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.forge import ForgeResult
    idx = pd.date_range("2022-01-01", periods=300, freq="1D")
    panel = pd.DataFrame({"close": 100 + np.cumsum(np.ones(300))}, index=idx)
    series = pd.Series(np.arange(300.0), index=idx)
    metrics = {"gross_ic": 0.001, "ic_nonoverlap": 0.0, "ir": 0.0, "dsr": 0.1, "pbo": None,
               "turnover": 0.1, "n_samples": 300, "regime_ic": {}, "yearly_ic": {},
               "nearest_factor": None, "nearest_abs_spearman": 0.1}
    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(True, 1, code="c", series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(False, metrics, "weak gross_ic"))
    monkeypatch.setattr(orch, "append_event", lambda md, **kw: None)
    out = process_hypothesis(Hypothesis("dead1", "x", SOURCE_LLM), panel, panel,
                             pd.Series("bull", index=idx), pd.DataFrame(index=idx),
                             "eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                             llm=object(), run_sandbox=object())
    assert out == "rejected"
    grave = pd.read_parquet(_graveyard_path("eth", tmp_path))
    assert "dead1" in grave.columns                   # values persisted for C-6 dedup
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
import hashlib
from datetime import datetime, timezone

from research.hermes.candidate_store import CANDIDATE_SUBDIR, _candidate_path, write_candidate
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.evidence_store import upsert_card
from research.hermes.forge import forge
from research.hermes.gatekeeper import evaluate
from research.lib.factor_io import _atomic_to_parquet, _symbol_short
from research.lib.research_ledger import append_event

MAX_FORGE_RETRIES = 3          # P5: bounded repair, then bury


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _graveyard_path(symbol, manifests_dir) -> Path:
    """Dead factors' VALUES live next to the candidates, never in production."""
    return Path(manifests_dir) / CANDIDATE_SUBDIR / f"graveyard_{_symbol_short(symbol)}.parquet"


def _merge_column(existing: "pd.DataFrame | None", name: str, series) -> pd.DataFrame:
    """Add/replace one column, aligning on the union of indexes."""
    frame = pd.DataFrame({name: series})
    if existing is None or existing.empty:
        return frame
    return existing.drop(columns=[name], errors="ignore").join(frame, how="outer")


def _merge_into_candidates(symbol, manifests_dir, factor_id, series) -> None:
    """agy 3b: write_candidate replaces the WHOLE parquet (os.replace). Read-merge-write
    so a nightly sweep keeps every passing factor, not just the last one."""
    path = _candidate_path(symbol, Path(manifests_dir))
    existing = pd.read_parquet(path) if path.exists() else None
    write_candidate(_merge_column(existing, factor_id, series), symbol, manifests_dir)


def _merge_into_graveyard(symbol, manifests_dir, factor_id, series) -> None:
    """agy 3c: persist DEAD factor values so 1A nearest_correlate can numerically
    dedup against the graveyard (C-6). Nothing else reads this file."""
    path = _graveyard_path(symbol, manifests_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_parquet(path) if path.exists() else None
    _atomic_to_parquet(_merge_column(existing, factor_id, series), path)


def process_hypothesis(hyp, panel, ohlcv, daily_regime, existing_and_dead,
                       symbol, manifests_dir, cfg, llm, run_sandbox) -> str:
    fr = forge(hyp, llm, run_sandbox, panel, max_retries=MAX_FORGE_RETRIES)
    code_sha = hashlib.sha256((fr.code or "").encode()).hexdigest()
    common = dict(factor_id=hyp.id, symbol=symbol, source=hyp.source,
                  code_sha256=code_sha, generated_at=_now(), trial_step=fr.attempts,
                  interval=cfg.interval, formula=hyp.description,
                  rationale=f"foundry {hyp.source}")

    if not fr.success:
        # no series exists (code never ran clean) -> card only, nothing to bury numerically
        upsert_card(EvidenceCard(**common, verdict=VERDICT_GRAVEYARD,
                                 death_reason=fr.death_reason or "forge failed"),
                    symbol, manifests_dir)
        return "forge_failed"

    res = evaluate(fr.series, ohlcv, daily_regime, existing_and_dead, symbol, manifests_dir, cfg)
    # write factor_trial AFTER evaluate: foundry_dsr already appends the CURRENT
    # trial in-memory, so pre-writing it would double-count (1A agy-3 #3).
    append_event(manifests_dir, kind="factor_trial", symbol=symbol,
                 detail={"sr_per_bar": res.metrics.get("ir"), "interval": cfg.interval,
                         "factor_id": hyp.id})
    m = res.metrics
    card = EvidenceCard(
        **common,
        gross_ic=m["gross_ic"], ic_nonoverlap=m["ic_nonoverlap"], ir=m["ir"],
        dsr=m["dsr"], pbo=m["pbo"], turnover=m["turnover"], n_samples=m["n_samples"],
        regime_ic=m["regime_ic"], yearly_ic=m["yearly_ic"],
        nearest_factor=m["nearest_factor"], nearest_abs_spearman=m["nearest_abs_spearman"],
        verdict=VERDICT_CANDIDATE if res.passed else VERDICT_GRAVEYARD,
        death_reason=None if res.passed else res.rejection_reason)

    if res.passed:
        _merge_into_candidates(symbol, manifests_dir, hyp.id, fr.series)
    else:
        _merge_into_graveyard(symbol, manifests_dir, hyp.id, fr.series)
    upsert_card(card, symbol, manifests_dir)
    return "candidate" if res.passed else "rejected"
```

> 實作前 `Read research/hermes/candidate_store.py` 確認 `_candidate_path`/`CANDIDATE_SUBDIR` 可匯入（皆為模組層名稱），與 `factor_io._atomic_to_parquet`/`_symbol_short` 簽名。勿臆測。

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): process_hypothesis forge->evaluate->card->ledger (Phase 1D)`

---

## Task 3: `Budget` + early stopping

**Files:** Modify `orchestrator.py` + test.

- [ ] **Step 1: Failing test**

```python
def test_early_stop_after_consecutive_failures():
    from research.hermes.orchestrator import Budget, should_early_stop
    b = Budget(max_factors=100, early_stop_after=3)
    assert should_early_stop(["forge_failed", "rejected", "forge_failed"], b) is True
    assert should_early_stop(["forge_failed", "candidate", "forge_failed"], b) is False  # a win resets
    assert should_early_stop(["forge_failed", "rejected"], b) is False                    # under threshold


def test_budget_caps_factor_count():
    from research.hermes.orchestrator import Budget
    assert Budget(max_factors=2, early_stop_after=99).max_factors == 2
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    max_factors: int = 50           # per-run cap on hypotheses tried
    early_stop_after: int = 8       # consecutive non-candidate outcomes -> stop the night


def should_early_stop(outcomes: list, budget: Budget) -> bool:
    """True once the tail has `early_stop_after` consecutive non-candidate results
    (P5: don't burn the nightly budget once the run is clearly diverging). A
    candidate resets the streak."""
    streak = 0
    for o in reversed(outcomes):
        if o == "candidate":
            break
        streak += 1
    return streak >= budget.early_stop_after
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): Budget + consecutive-failure early stopping (Phase 1D, P5)`

---

## Task 4: `run_foundry`（掃佇列 + 預算/early-stop）

**Files:** Modify `orchestrator.py` + test. Reference: `hypothesis_queue.build_queue`（1B）、`factor_io.load_features`。

載入 symbol feature panel → `build_queue` → 逐 hypothesis `process_hypothesis`，受 `Budget.max_factors` 與 `should_early_stop` 約束 → 回 summary（counts）。

- [ ] **Step 1: Failing test**

```python
def test_run_foundry_respects_budget_and_early_stop(tmp_path, monkeypatch):
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=100, freq="1D")
    panel = pd.DataFrame({"close": np.arange(100.0)}, index=idx)
    monkeypatch.setattr(orch, "load_features", lambda s, manifests_dir=None: panel)
    monkeypatch.setattr(orch, "build_queue", lambda **k: [Hypothesis(f"h{i}", f"x{i}", SOURCE_ZOO) for i in range(20)])
    # every hypothesis fails -> early stop should fire before all 20 processed
    seen = []
    def fake_process(hyp, *a, **k): seen.append(hyp.id); return "forge_failed"
    monkeypatch.setattr(orch, "process_hypothesis", fake_process)
    summary = run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                          llm=object(), sandbox=object(),
                          budget=Budget(max_factors=20, early_stop_after=3),
                          zoo_dir=tmp_path, run_sandbox=object())
    assert len(seen) == 3                             # stopped after 3 consecutive fails
    assert summary["forge_failed"] == 3 and summary["candidate"] == 0


def test_run_foundry_raises_when_panel_has_no_close(tmp_path, monkeypatch):
    import pandas as pd, numpy as np
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import run_foundry, Budget
    from research.hermes.gatekeeper import GateConfig
    idx = pd.date_range("2022-01-01", periods=50, freq="1D")
    monkeypatch.setattr(orch, "load_features",
                        lambda s, manifests_dir=None: pd.DataFrame({"funding_z": np.arange(50.0)}, index=idx))
    with pytest.raises(ValueError, match="close"):      # agy 4a
        run_foundry("eth", tmp_path, GateConfig(interval="1D", horizon_h=24),
                    llm=object(), sandbox=object(), budget=Budget(), zoo_dir=tmp_path)
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
from collections import Counter

from research.hermes.hypothesis_queue import build_queue
from research.lib.factor_io import load_features

_OHLCV_COLS = ("open", "high", "low", "close", "volume")


def run_foundry(symbol, manifests_dir, cfg, llm, sandbox, budget, zoo_dir,
                daily_regime=None, run_sandbox=None) -> dict:
    """Sweep the hypothesis queue for one symbol under Budget + early stopping.
    zoo_dir is REQUIRED (agy 4c: build_queue does Path(zoo_dir).rglob -> Path(None)
    raises TypeError)."""
    panel = load_features(symbol, manifests_dir=manifests_dir)
    ohlcv = panel[[c for c in _OHLCV_COLS if c in panel.columns]]
    if "close" not in ohlcv.columns:                # agy 4a: evaluate hard-depends on close
        raise ValueError(f"feature panel for {symbol} has no 'close' column; cannot evaluate")
    existing = panel.drop(columns=list(ohlcv.columns), errors="ignore")

    # agy 3c: include buried factor VALUES so nearest_correlate can dedup vs the graveyard
    gpath = _graveyard_path(symbol, manifests_dir)
    if gpath.exists():
        existing = existing.join(pd.read_parquet(gpath), how="outer", rsuffix="_dead")

    run_sb = run_sandbox or make_run_sandbox(sandbox, Path(manifests_dir) / "_foundry_scratch")
    if daily_regime is None:
        # agy 4b: regime_ic expects DAILY labels (it ffills onto the factor index);
        # a panel-frequency fallback would violate that contract.
        daily_idx = panel.index.normalize().unique()
        daily_regime = pd.Series("neutral", index=daily_idx)

    queue = build_queue(symbol=symbol, manifests_dir=manifests_dir,
                        zoo_dir=zoo_dir, llm_raw=[])[: budget.max_factors]
    outcomes: list = []
    for hyp in queue:
        outcomes.append(process_hypothesis(hyp, panel, ohlcv, daily_regime, existing,
                                           symbol, manifests_dir, cfg, llm, run_sb))
        if should_early_stop(outcomes, budget):
            break
    return dict(Counter(outcomes))
```

> **注意**：新過關/新入墓的因子在**當次 run 內**不會即時進 `existing`（`existing` 在迴圈前一次載入）。同夜內的自我去重留給下一次 run；若要即時，迴圈內把 `fr.series` 併進 `existing`。實作時擇一並註記，勿默默略過。

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): run_foundry queue sweep with budget/early-stop (Phase 1D)`

---

## Task 5: write-file→reconcile job 觸發 + 匯出

**Files:** Modify `orchestrator.py`, `research/hermes/__init__.py`; test.

**憲法**：Talos 不 inline 跑。`enqueue_foundry_job` 寫 `runs/foundry_jobs/<id>/job.json`（nightly cron / on-demand 呼叫）；`run_foundry_job` 由獨立 foundry runner 撿起、讀 job、調 `run_foundry`。

- [ ] **Step 1: Failing test**

```python
def test_enqueue_writes_job_and_runner_reconciles(tmp_path, monkeypatch):
    import json
    from research.hermes import orchestrator as orch
    from research.hermes.orchestrator import enqueue_foundry_job, run_foundry_job

    job_path = enqueue_foundry_job("eth", runs_dir=tmp_path,
                                   params={"interval": "1D", "horizon_h": 24})
    assert job_path.exists()
    job = json.loads(job_path.read_text())
    assert job["symbol"] == "eth" and job["status"] == "queued"

    called = {}
    monkeypatch.setattr(orch, "run_foundry",
                        lambda symbol, *a, **k: called.setdefault("symbol", symbol) or {"candidate": 1})
    summary = run_foundry_job(job_path, manifests_dir=tmp_path, llm=object(),
                              sandbox=object(), zoo_dir=tmp_path)
    assert called["symbol"] == "eth" and summary["candidate"] == 1
    assert json.loads(job_path.read_text())["status"] == "done"     # reconciled
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
import json


def enqueue_foundry_job(symbol, runs_dir, params: dict) -> Path:
    """Write a queued foundry job (write-file->reconcile). Talos NEVER inline-runs;
    a separate foundry runner picks this up. Called by nightly cron / on-demand."""
    job_id = f"foundry_{symbol}_{uuid.uuid4().hex[:8]}"
    job_dir = Path(runs_dir) / "foundry_jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job_path = job_dir / "job.json"
    job_path.write_text(json.dumps(
        {"job_id": job_id, "symbol": symbol, "params": params,
         "status": "queued", "created_at": _now()}, indent=2), encoding="utf-8")
    return job_path


def run_foundry_job(job_path, manifests_dir, llm, sandbox, zoo_dir, budget=None) -> dict:
    """Foundry runner reconcile: read job.json, run_foundry, mark done."""
    from research.hermes.gatekeeper import GateConfig
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    p = job["params"]
    cfg = GateConfig(interval=p.get("interval", "1H"), horizon_h=p.get("horizon_h", 24))
    summary = run_foundry(job["symbol"], manifests_dir, cfg, llm, sandbox,
                          budget or Budget(), zoo_dir=zoo_dir)
    job["status"] = "done"; job["summary"] = summary; job["finished_at"] = _now()
    Path(job_path).write_text(json.dumps(job, indent=2), encoding="utf-8")
    return summary
```

在 `research/hermes/__init__.py` 追加匯出 `run_foundry`, `enqueue_foundry_job`, `run_foundry_job`, `Budget`（先 `Read` 現況再改）。

- [ ] **Step 4: Run orchestrator suite + 全 hermes 套件無回歸。**

- [ ] **Step 5: Commit** `feat(hermes): foundry job enqueue + reconcile runner, export (Phase 1D)`

---

## Self-Review

**Spec coverage：** run_sandbox 注入 → Task 1；forge→evaluate→card→ledger 全程 → Task 2；預算+early-stop（P5）→ Task 3；佇列掃描 → Task 4；write-file→reconcile job（憲法）→ Task 5。

**關鍵順序（1A 契約）：** `factor_trial` ledger 在 `evaluate` **之後**寫——本次 trial 供**未來**因子 DSR；當前因子的 DSR 由 evaluate 內部含入（1A agy-3 #3）。Task 2 測試驗 `factor_trial` 有寫。

**已知界線 / 待後續：**
- **真 docker/LLM 全在注入層**：`sandbox`（DockerSandbox）+ `llm`（OpenRouter client）由呼叫端傳入；1D 測試全 fake，無 docker/網路。真跑由 nightly cron 環境提供。
- **PBO 仍 None**（1A 留）：不阻塞；之後 CPCV 接線增量。
- **nightly cron 排程**：`enqueue_foundry_job` 是被 cron 呼叫的入口；cron 本身（crontab / dashboard scheduler）非本計畫程式碼，是部署設定。
- **token 預算**：`Budget` 目前以因子數 + 連敗數控；細粒度 token 計數待接 LLM client 用量回報後增量。

**Placeholder scan：** 無草稿殘留（agy 5：v1 曾在 Task 2 留 `_card`/`cfg_symbol`/`entry_lag and 3 or 3` 並註記「實作時刪」——會被複製貼上帶毒，已直接改為最終正確碼 + `MAX_FORGE_RETRIES=3` 常數）。Task 2/4 明示「實作前 Read `candidate_store`/`factor_io` 對齊簽名」。

**Type consistency：** `make_run_sandbox`/`process_hypothesis`/`Budget`/`should_early_stop`/`run_foundry`/`enqueue_foundry_job`/`run_foundry_job` 跨 task 一致；`process_hypothesis` 建的 `EvidenceCard` 欄位 == 1E schema（含 `gross_ic`，1A Task 7 已改名）；`GatekeeperResult.metrics` 鍵 == card 欄位（1A Task 7 測試已強制）。

**憲法對齊：** write-file→reconcile（不 inline）；每因子留痕（候選/墓地卡 + ledger）；有界（Budget + early stop）。

---

## 附錄：agy 二審（全採納）

agy 逐一核對 `forge`/`evaluate`/`GateConfig`/`EvidenceCard.gross_ic`/`upsert_card`/`write_candidate`/`append_event`/`build_queue`/`load_features` 簽名——**全查核無誤**。並確認：**ledger 順序推論正確**（`foundry_dsr` 內 `trials.append(best_sr_per_bar)` 已含當前 trial，先寫 ledger 會 double-count）；**graveyard 卡免 core-metrics 檢驗**（`__post_init__` 只對 candidate 檢）。修正：

- **3b（致命）**：`write_candidate` 底層 `os.replace` **整檔取代**，迴圈逐因子寫→整晚只活最後一個。改 **merge-on-write**（`_merge_into_candidates` 讀既有→加欄→原子寫）+ 兩因子並存回歸測試。
- **3c（設計斷層）**：死因子 series 從未落地（只存卡片）→ 1A `nearest_correlate` 的**墓地數值比對從來沒接上**，C-6 憲法形同虛設。新增 `graveyard_<sym>.parquet` 落地死因子值，`run_foundry` 載入併進 `existing_and_dead` + 回歸測試。
- **4a**：panel 缺 `close` → `evaluate` `KeyError` 當場炸。加顯式 `ValueError` guard + 測試。
- **4b**：`daily_regime` fallback 用 panel 頻率，違反 `regime_ic` 的日級+ffill 契約。改 `panel.index.normalize().unique()`。
- **4c**：`zoo_dir=None` → `build_queue` 內 `Path(None)` `TypeError`。改**必填參數**（連帶 `run_foundry_job` 傳入）。
- **5（計畫缺陷）**：Task 2 的 `_card`/`cfg_symbol` 草稿殘留 + `cfg.entry_lag and 3 or 3` 怪 trick 會誤導實作者複製貼上。**已直接改為最終正確碼**。

---

## Execution Handoff

計畫存 `docs/talos/plans/2026-07-08-talos-phase1d-orchestrator.md`，已納 agy 二審全部修正。這是 Phase 1 最後一塊。選執行：Subagent-Driven（推薦）或 Inline，每 Task 停等核准。**1D 完成即 Foundry 端到端可跑（on-demand + nightly）。**
