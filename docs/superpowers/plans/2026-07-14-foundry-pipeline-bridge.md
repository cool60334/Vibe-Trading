# Foundry → Pipeline Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Foundry's rigorously-vetted factors reach the pipeline's stage2–5 (build strategy → backtest → OOS-select), without ever touching the production feature store the live trader reads.

**Architecture:** Foundry persists its forged factor code (today it is thrown away). A bridge step reads each `verdict=candidate` card, re-runs that code in the Docker sandbox over the **full span** (Foundry's stored values stop at `oos_start`, so frozen values would leave the whole OOS window NaN), reconciles the recomputed pre-oos slice against Foundry's stored values, adapts the card into a pydantic `FactorEntry`, and materialises a **per-run overlay cache**. A single loader unions that cache over the production features at read time. The loader never computes — pipeline stages are separate subprocesses, so a compute-in-loader design would wake Docker once per stage.

**Tech Stack:** Python 3.11, pandas, pyarrow, pydantic v2, Docker (sandbox), pytest.

## Global Constraints

- Python interpreter for every command: `.venv/Scripts/python.exe` (repo root, Windows).
- **Three separate pytest scopes — never mix in one call.** Tasks 1–8 are research scope: `.venv/Scripts/python.exe -m pytest research/tests/ -q` from repo root. Task 9 touches `dashboard/server/` → run `cd dashboard/server && pytest -q`.
- **Production safety law:** `research/manifests/features_<sym>.parquet` and `factor_values_<sym>.parquet` are PRODUCTION (live trader reads them). `research/hermes/promote.py` is the ONLY writer, human-gated with `--confirm`. **No code in this plan may write them.** The bridge writes only to a per-run cache.
- Foundry candidate values live at `research/manifests/candidate_features/cand_<sym>.parquet`; evidence cards at `research/manifests/foundry_evidence/foundry_evidence_<sym>.json`.
- `oos_start` comes from `research/research_config.yaml` (currently `"2025-01-01"`). Never hardcode it in library code; read via `pipeline.config.load_config`.
- All factor/feature DatetimeIndexes are **tz-aware UTC** (`datetime64[us, UTC]`). Any new index must match.
- Foundry factor columns/entries are prefixed `foundry_` to avoid colliding with library feature names.
- Reconciliation comparisons MUST pass `equal_nan=True` (factors have rolling burn-in NaN; the default would kill valid factors).

## Existing types you will use (do not redefine)

```python
# dashboard/server/schemas.py  (pipeline bootstraps dashboard/server onto sys.path)
class FactorVerdict(str, Enum):   # 'single_use' | 'ensemble_only' | 'reject' | 'data_unavailable'
class FactorStability(str, Enum): # 'regime_stable' | 'conditional'
class FactorEntry(BaseModel):
    name: str
    ic_by_horizon: Dict[int, Optional[float]]
    ir: float
    sample_size: int
    cross_regime_ic: Optional[Dict[str, Optional[float]]] = None
    stability: Optional[FactorStability] = None
    verdict: FactorVerdict

# research/factor_regime.py
def classify_stability(cross_regime_ic: dict[str, float]) -> FactorStability
def refine_verdict(stability: FactorStability, base_verdict: FactorVerdict) -> FactorVerdict

# research/hermes/forge.py
@dataclass
class ForgeResult:
    success: bool; attempts: int
    code: Optional[str] = None; series: object = None; death_reason: Optional[str] = None

# research/hermes/evidence_card.py / evidence_store.py
VERDICT_CANDIDATE = "candidate"
def load_cards(symbol, manifests_dir) -> list[EvidenceCard]
# EvidenceCard fields used here: factor_id, code_sha256, interval, verdict,
#   gross_ic, ir, n_samples, regime_ic, dsr

# research/hermes/candidate_store.py
CANDIDATE_SUBDIR = "candidate_features"
def _candidate_path(symbol, manifests_dir) -> Path      # cand_<sym>.parquet

# research/hermes/sandbox.py
class DockerSandbox:  def run(self, source, input_parquet, output_dir) -> str
# research/hermes/orchestrator.py
def make_run_sandbox(sandbox, scratch_dir) -> Callable[[str, pd.DataFrame], pd.Series]
```

## File Structure

| File | Responsibility |
|---|---|
| `research/hermes/candidate_store.py` (modify) | + code store: write/read forged code + metadata, isolated under `candidate_features/code/` |
| `research/hermes/orchestrator.py` (modify) | `process_hypothesis` persists code for `candidate` verdicts |
| `research/hermes/foundry_bridge.py` (create) | The bridge: recompute → reconcile → adapt → materialise overlay. CLI entry. |
| `research/lib/factor_io.py` (modify) | `load_features(..., include_foundry)` + `load_manifest(..., include_foundry)`; read-only union, never computes |
| `research/pipeline/stage2b_compile_signal.py`, `stage3_backtest.py` (modify) | Route feature reads through `load_features` |
| `research/hermes/promote.py` (modify) | Refuse promoting a strategy that depends on a `foundry_` factor whose code is not in the production library |
| `dashboard/server/pipeline_manager.py` (modify) | Job teardown deletes overlay cache; subprocess env inherits `os.environ` |

---

### Task 1: Persist the forged factor code (code store)

Foundry throws `ForgeResult.code` away after the sandbox run, keeping only `code_sha256`. Without the code text the bridge has nothing to re-run. This task persists it for `candidate` factors only.

**Files:**
- Modify: `research/hermes/candidate_store.py`
- Modify: `research/hermes/orchestrator.py` (in `process_hypothesis`, after the verdict is known)
- Test: `research/tests/test_hermes_candidate_store.py`

**Interfaces:**
- Consumes: `CANDIDATE_SUBDIR`, `_symbol_short` (already in `candidate_store.py`).
- Produces (later tasks rely on these exact names):
  - `code_dir(symbol, manifests_dir) -> Path` → `<manifests>/candidate_features/code/<sym>/`
  - `write_candidate_code(factor_id, symbol, manifests_dir, code: str, meta: dict) -> Path`
  - `load_candidate_code(factor_id, symbol, manifests_dir) -> tuple[str, dict]` — raises `FileNotFoundError` if absent.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_hermes_candidate_store.py` (append if it exists):

```python
import json
import pytest
from research.hermes.candidate_store import (
    code_dir, write_candidate_code, load_candidate_code,
)


def test_candidate_code_roundtrips_with_metadata(tmp_path):
    code = "def compute(panel):\n    return panel['close'].pct_change()\n"
    meta = {"code_sha256": "abc123", "base_image_id": "sha256:deadbeef",
            "interval": "1H", "horizon_h": 24}

    write_candidate_code("zoo_mom", "eth", tmp_path, code, meta)
    got_code, got_meta = load_candidate_code("zoo_mom", "eth", tmp_path)

    assert got_code == code
    assert got_meta["code_sha256"] == "abc123"
    assert got_meta["base_image_id"] == "sha256:deadbeef"


def test_code_store_lives_under_candidate_features(tmp_path):
    # must never escape the isolated candidate store (same law as write_candidate)
    d = code_dir("eth", tmp_path)
    assert d.parent.parent.name == "candidate_features"


def test_load_candidate_code_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_candidate_code("nope", "eth", tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_candidate_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'code_dir' from 'research.hermes.candidate_store'`

- [ ] **Step 3: Write minimal implementation**

Add to `research/hermes/candidate_store.py` (after `_candidate_path`):

```python
import json


def code_dir(symbol: str, manifests_dir) -> Path:
    """Isolated code store for forged candidate factors. Mirrors the candidate
    parquet's containment law: everything stays under candidate_features/."""
    return Path(manifests_dir) / CANDIDATE_SUBDIR / "code" / _symbol_short(symbol)


def write_candidate_code(factor_id: str, symbol: str, manifests_dir,
                         code: str, meta: dict) -> Path:
    """Persist a forged factor's SOURCE (not just its sha) so the bridge can
    re-run it over the full span later. Foundry otherwise discards it."""
    d = code_dir(symbol, manifests_dir)
    d.mkdir(parents=True, exist_ok=True)
    py = d / f"{factor_id}.py"
    py.write_text(code, encoding="utf-8")
    (d / f"{factor_id}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    return py


def load_candidate_code(factor_id: str, symbol: str, manifests_dir) -> tuple:
    """(code, meta). Raises FileNotFoundError when the factor was never stored."""
    d = code_dir(symbol, manifests_dir)
    py = d / f"{factor_id}.py"
    if not py.exists():
        raise FileNotFoundError(f"no stored code for {symbol}:{factor_id} at {py}")
    meta_path = d / f"{factor_id}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return py.read_text(encoding="utf-8"), meta
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_candidate_store.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Write the failing wiring test**

Append to `research/tests/test_hermes_orchestrator.py`:

```python
def test_process_hypothesis_persists_code_for_candidate_only(tmp_path, monkeypatch):
    # A candidate's forged source must be retrievable afterwards (the bridge
    # re-runs it over the full span); a graveyard factor's need not be.
    import numpy as np, pandas as pd
    from research.hermes import orchestrator as orch
    from research.hermes.candidate_store import load_candidate_code
    from research.hermes.forge import ForgeResult
    from research.hermes.gatekeeper import GateConfig, GatekeeperResult
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

    idx = pd.date_range("2022-01-01", periods=48, freq="1h", tz="UTC")
    panel = pd.DataFrame({"close": np.arange(48.0)}, index=idx)
    series = pd.Series(np.arange(48.0), index=idx)
    code = "def compute(panel):\n    return panel['close']\n"

    monkeypatch.setattr(orch, "forge", lambda *a, **k: ForgeResult(
        success=True, attempts=1, code=code, series=series))
    monkeypatch.setattr(orch, "evaluate", lambda *a, **k: GatekeeperResult(
        True, {"gross_ic": 0.1, "ic_nonoverlap": 0.1, "ir": 0.5, "dsr": 1.0,
               "pbo": None, "turnover": 0.1, "n_samples": 48,
               "regime_ic": {}, "yearly_ic": {}, "nearest_factor": None,
               "nearest_abs_spearman": 0.0}, ""))
    monkeypatch.setattr(orch, "upsert_card", lambda *a, **k: None)
    monkeypatch.setattr(orch, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_merge_into_candidates", lambda *a, **k: None)

    hyp = Hypothesis("zoo_mom", "momentum", SOURCE_ZOO)
    cfg = GateConfig(interval="1H", horizon_h=24)
    out = orch.process_hypothesis(hyp, panel, panel, None, panel, "eth",
                                  tmp_path, cfg, object(), lambda c, p: series)

    assert out == orch.OUTCOME_CANDIDATE
    got_code, meta = load_candidate_code("zoo_mom", "eth", tmp_path)
    assert got_code == code
    assert meta["code_sha256"] == __import__("hashlib").sha256(code.encode()).hexdigest()
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_orchestrator.py::test_process_hypothesis_persists_code_for_candidate_only -q`
Expected: FAIL — `FileNotFoundError: no stored code for eth:zoo_mom`

- [ ] **Step 7: Expose the sandbox image id on the run closure**

`process_hypothesis` only receives the `run_sandbox` *callable*, not the sandbox object, so it cannot see the image id. Attach it in `make_run_sandbox` (`research/hermes/orchestrator.py`) — without this the `base_image_id` we record would silently always be `None`:

```python
def make_run_sandbox(sandbox: SandboxExecutor, scratch_dir: str | Path):
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    def run(code: str, panel: pd.DataFrame) -> pd.Series:
        ...unchanged body...

    # Provenance for the code store: the bridge compares this against the image
    # it re-runs under, and a drifted image shows up as a reconciliation failure.
    run.image_id = getattr(sandbox, "image", None)
    return run
```

- [ ] **Step 8: Wire persistence into `process_hypothesis`**

In `research/hermes/orchestrator.py`, add the import at the top:

```python
from research.hermes.candidate_store import write_candidate_code
```

In `process_hypothesis`, replace the `if res.passed:` block with:

```python
    if res.passed:
        _merge_into_candidates(symbol, manifests_dir, hyp.id, fr.series)
        # Persist the SOURCE, not just its sha: the bridge must re-run this exact
        # code over the full span (Foundry only ever computed it pre-oos).
        # horizon_h is recorded per factor because gross_ic on the card was
        # measured at THIS horizon — the bridge must reuse it, not the config's first.
        write_candidate_code(hyp.id, symbol, manifests_dir, fr.code or "", {
            "code_sha256": code_sha,
            "base_image_id": getattr(run_sandbox, "image_id", None),
            "interval": cfg.interval,
            "horizon_h": cfg.horizon_h,
        })
    else:
        _merge_into_graveyard(symbol, manifests_dir, hyp.id, fr.series)
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_orchestrator.py research/tests/test_hermes_candidate_store.py -q`
Expected: PASS (all)

- [ ] **Step 10: Commit**

```bash
git add research/hermes/candidate_store.py research/hermes/orchestrator.py research/tests/test_hermes_candidate_store.py research/tests/test_hermes_orchestrator.py
git commit -m "feat(hermes): persist forged candidate code so the bridge can re-run it"
```

---

### Task 2: Bridge — recompute a candidate over the full span

**Files:**
- Create: `research/hermes/foundry_bridge.py`
- Test: `research/tests/test_hermes_foundry_bridge.py`

**Interfaces:**
- Consumes: `load_candidate_code` (Task 1), `make_run_sandbox` (orchestrator).
- Produces:
  - `FOUNDRY_PREFIX = "foundry_"`
  - `recompute_full_span(code: str, panel: pd.DataFrame, run_sandbox) -> pd.Series` — runs the code on the FULL panel; returns a series on `panel.index`.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_hermes_foundry_bridge.py`:

```python
import numpy as np
import pandas as pd
from research.hermes.foundry_bridge import FOUNDRY_PREFIX, recompute_full_span


def _panel(n=100, start="2024-11-01"):
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.arange(float(n))}, index=idx)


def test_recompute_covers_the_full_span_including_oos():
    # Foundry only ever computed pre-oos values; the bridge must produce values
    # across the WHOLE panel or stage3's OOS window would be all-NaN.
    panel = _panel(100)
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    out = recompute_full_span("code", panel, run)

    assert out.index.equals(panel.index)
    assert out.notna().all()
    assert FOUNDRY_PREFIX == "foundry_"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.hermes.foundry_bridge'`

- [ ] **Step 3: Write minimal implementation**

Create `research/hermes/foundry_bridge.py`:

```python
"""Foundry -> pipeline bridge.

Foundry vets factors but stores only PRE-OOS values (the OOS window is
deliberately reserved), so its frozen values cannot back a walk-forward
backtest. The bridge therefore re-runs each candidate's PERSISTED CODE over the
full span, reconciles the pre-oos slice against what Foundry stored, and hands
the result to the pipeline as an overlay.

It NEVER writes production features_<sym>.parquet — promote.py stays the only
path into what the live trader reads.
"""
from __future__ import annotations

import pandas as pd

FOUNDRY_PREFIX = "foundry_"


def recompute_full_span(code: str, panel: pd.DataFrame, run_sandbox) -> pd.Series:
    """Re-run a forged factor over the WHOLE panel (train + OOS).

    `run_sandbox(code, panel) -> pd.Series` is injected (real DockerSandbox in
    production, a fake in tests) so the bridge never imports docker directly.
    """
    series = run_sandbox(code, panel)
    series = pd.Series(series, index=panel.index) if not isinstance(series, pd.Series) else series
    if not series.index.equals(panel.index):
        series = series.reindex(panel.index)
    return series
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_bridge.py research/tests/test_hermes_foundry_bridge.py
git commit -m "feat(hermes): bridge recomputes a forged factor over the full span"
```

---

### Task 3: Bridge — reconcile the recomputed pre-oos slice

If the recomputed pre-oos values do not match what Foundry stored, the code is not reproducible here (non-causal residue, or the sandbox image drifted). Such a factor must be dropped, not silently trusted.

**Files:**
- Modify: `research/hermes/foundry_bridge.py`
- Test: `research/tests/test_hermes_foundry_bridge.py`

**Interfaces:**
- Produces: `reconciles_pre_oos(recomputed: pd.Series, stored: pd.Series, oos_start: str) -> bool`

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_foundry_bridge.py`:

```python
from research.hermes.foundry_bridge import reconciles_pre_oos

_OOS = "2024-11-03"


def test_reconciles_when_pre_oos_matches():
    panel = _panel(100)
    rec = pd.Series(np.arange(100.0), index=panel.index)
    stored = rec[rec.index < pd.Timestamp(_OOS, tz="UTC")]
    assert reconciles_pre_oos(rec, stored, _OOS) is True


def test_reconciliation_tolerates_burn_in_nan():
    # rolling factors are NaN for their warm-up window; np.allclose defaults to
    # equal_nan=False, which would kill a perfectly valid factor.
    panel = _panel(100)
    rec = pd.Series(np.arange(100.0), index=panel.index)
    rec.iloc[:5] = np.nan
    stored = rec[rec.index < pd.Timestamp(_OOS, tz="UTC")]
    assert reconciles_pre_oos(rec, stored, _OOS) is True


def test_reconciliation_fails_when_pre_oos_drifts():
    panel = _panel(100)
    rec = pd.Series(np.arange(100.0), index=panel.index)
    stored = rec[rec.index < pd.Timestamp(_OOS, tz="UTC")] + 1.0   # drift
    assert reconciles_pre_oos(rec, stored, _OOS) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: FAIL — `ImportError: cannot import name 'reconciles_pre_oos'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/hermes/foundry_bridge.py`:

```python
import numpy as np


def reconciles_pre_oos(recomputed: pd.Series, stored: pd.Series,
                       oos_start: str, atol: float = 1e-8) -> bool:
    """Does the re-run reproduce what Foundry stored on the pre-oos window?

    A mismatch means the code is not reproducible in this environment — a
    non-causal residue that only shows up once future rows exist, or a drifted
    sandbox image. Either way the factor must be dropped, not trusted.

    equal_nan=True is REQUIRED: a rolling factor is NaN through its warm-up
    window, and the numpy default (equal_nan=False) would report every such
    factor as a mismatch and silently kill it. (forge's own pit_check_via_sandbox
    compares with equal_nan=True for exactly this reason.)
    """
    cutoff = pd.Timestamp(oos_start)
    if cutoff.tz is None:
        cutoff = cutoff.tz_localize(recomputed.index.tz)
    left = recomputed[recomputed.index < cutoff]
    right = stored.reindex(left.index)
    if left.empty:
        return False
    return bool(np.allclose(left.to_numpy(dtype="float64"),
                            right.to_numpy(dtype="float64"),
                            atol=atol, rtol=0.0, equal_nan=True))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_bridge.py research/tests/test_hermes_foundry_bridge.py
git commit -m "feat(hermes): bridge reconciles recomputed pre-oos against Foundry's stored values"
```

---

### Task 4: Bridge — adapt an evidence card into a FactorEntry

**Files:**
- Modify: `research/hermes/foundry_bridge.py`
- Test: `research/tests/test_hermes_foundry_bridge.py`

**Interfaces:**
- Consumes: `FactorEntry`, `FactorVerdict` (from `schemas`), `classify_stability`, `refine_verdict` (from `research.factor_regime`).
- Produces: `card_to_entry(card) -> FactorEntry` — name is `FOUNDRY_PREFIX + card.factor_id`.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_foundry_bridge.py`:

```python
def test_card_to_entry_maps_fields_and_uses_real_classify_stability():
    from research.hermes.foundry_bridge import card_to_entry
    from research.factor_regime import classify_stability
    from schemas import FactorVerdict

    class Card:                      # duck-typed EvidenceCard
        factor_id = "zoo_mom"
        gross_ic = 0.06
        ir = 0.4
        n_samples = 20000
        interval = "1H"
        regime_ic = {"bull": 0.05, "bear": 0.05, "neutral": 0.05}

    entry = card_to_entry(Card(), horizon_h=24)

    assert entry.name == "foundry_zoo_mom"
    assert entry.ic_by_horizon == {24: 0.06}
    assert entry.ir == 0.4
    assert entry.sample_size == 20000
    assert entry.cross_regime_ic == Card.regime_ic
    # stability must come from the REAL pipeline function, not an invented metric
    assert entry.stability == classify_stability(Card.regime_ic)
    assert entry.verdict != FactorVerdict.REJECT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py::test_card_to_entry_maps_fields_and_uses_real_classify_stability -q`
Expected: FAIL — `ImportError: cannot import name 'card_to_entry'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/hermes/foundry_bridge.py` (add the path bootstrap at the top of the file, mirroring `stage1_factors.py`, so `schemas` resolves):

```python
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT / "research", _REPO_ROOT / "dashboard" / "server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from schemas import FactorEntry, FactorVerdict          # noqa: E402
from research.factor_regime import classify_stability, refine_verdict   # noqa: E402


def card_to_entry(card, horizon_h: int) -> FactorEntry:
    """Foundry evidence card -> pipeline FactorEntry.

    Every statistic is taken from the card, i.e. from Foundry's PRE-OOS
    evaluation. Nothing is recomputed from the full-span series — that would
    leak OOS information into the selection stage2 performs.

    `stability` reuses the pipeline's own classify_stability(cross_regime_ic)
    (a regime_stable/conditional classification) rather than inventing a
    different metric, so stage2's filters read it with the same meaning as a
    library factor's.
    """
    regime_ic = dict(card.regime_ic or {})
    stability = classify_stability(regime_ic) if regime_ic else None
    verdict = FactorVerdict.SINGLE_USE
    if stability is not None:
        verdict = refine_verdict(stability, verdict)   # conditional -> ensemble_only
    return FactorEntry(
        name=f"{FOUNDRY_PREFIX}{card.factor_id}",
        ic_by_horizon={int(horizon_h): card.gross_ic},
        ir=float(card.ir),
        sample_size=int(card.n_samples),
        cross_regime_ic=regime_ic or None,
        stability=stability,
        verdict=verdict,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/foundry_bridge.py research/tests/test_hermes_foundry_bridge.py
git commit -m "feat(hermes): map Foundry evidence cards onto pipeline FactorEntry"
```

---

### Task 5: Bridge — build the per-run overlay (cap, collision, alignment) and its CLI

Ties Tasks 1–4 together: select candidates, recompute, reconcile, adapt, and materialise the per-run overlay cache.

**Files:**
- Modify: `research/hermes/foundry_bridge.py`
- Test: `research/tests/test_hermes_foundry_bridge.py`

**Interfaces:**
- Produces:
  - `build_overlay(symbol, manifests_dir, panel, run_sandbox, oos_start, horizon_h, cap=50) -> tuple[pd.DataFrame, list[FactorEntry]]`
  - `write_overlay(overlay_dir, symbol, df, entries) -> None` — writes `foundry_overlay_<sym>.parquet` + `foundry_manifest_<sym>.json`
  - `main(argv=None) -> int` — CLI: `python -m research.hermes.foundry_bridge --symbol eth --overlay-dir <dir> --image <tag>`

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_foundry_bridge.py`:

```python
import json
from research.hermes.foundry_bridge import build_overlay, write_overlay
from research.hermes.candidate_store import write_candidate_code, _candidate_path


class _Card:
    def __init__(self, fid, verdict="candidate", dsr=1.0):
        self.factor_id = fid; self.verdict = verdict
        self.gross_ic = 0.06; self.ir = 0.4; self.n_samples = 90
        self.interval = "1H"; self.dsr = dsr
        self.regime_ic = {"bull": 0.05, "bear": 0.05, "neutral": 0.05}
        self.code_sha256 = ""


def _seed(tmp_path, panel, fids):
    """Write cand parquet (pre-oos values) + stored code for each factor id."""
    pre = panel.index < pd.Timestamp(_OOS, tz="UTC")
    cand = pd.DataFrame(
        {f: pd.Series(np.arange(float(len(panel))), index=panel.index)[pre] for f in fids})
    p = _candidate_path("eth", tmp_path); p.parent.mkdir(parents=True, exist_ok=True)
    cand.to_parquet(p)
    for f in fids:
        write_candidate_code(f, "eth", tmp_path, "code", {"code_sha256": ""})


def test_build_overlay_recomputes_reconciles_and_prefixes(tmp_path, monkeypatch):
    panel = _panel(100)
    _seed(tmp_path, panel, ["zoo_mom"])
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards",
                        lambda s, d: [_Card("zoo_mom")])
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24)

    assert list(df.columns) == ["foundry_zoo_mom"]
    assert df.index.equals(panel.index)          # full span, OOS included
    assert df["foundry_zoo_mom"].notna().all()
    assert [e.name for e in entries] == ["foundry_zoo_mom"]


def test_build_overlay_drops_a_factor_whose_pre_oos_does_not_reconcile(tmp_path, monkeypatch):
    panel = _panel(100)
    _seed(tmp_path, panel, ["zoo_bad"])
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards",
                        lambda s, d: [_Card("zoo_bad")])
    run = lambda code, p: pd.Series(np.arange(float(len(p))) + 99.0, index=p.index)  # drift

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24)

    assert df.empty and entries == []


def test_build_overlay_skips_graveyard_and_missing_code(tmp_path, monkeypatch):
    panel = _panel(100)
    _seed(tmp_path, panel, ["zoo_ok"])                      # only zoo_ok has code
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards",
                        lambda s, d: [_Card("zoo_ok"), _Card("zoo_dead", verdict="graveyard"),
                                      _Card("zoo_nocode")])
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24)

    assert list(df.columns) == ["foundry_zoo_ok"]           # graveyard + no-code dropped


def test_build_overlay_caps_candidate_count(tmp_path, monkeypatch):
    panel = _panel(100)
    fids = [f"f{i}" for i in range(5)]
    _seed(tmp_path, panel, fids)
    cards = [_Card(f, dsr=float(i)) for i, f in enumerate(fids)]   # f4 best
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards", lambda s, d: cards)
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24, cap=2)

    assert len(df.columns) == 2                              # OOM guard
    assert "foundry_f4" in df.columns                        # top-K by dsr


def test_build_overlay_skips_a_name_that_collides_with_production(tmp_path, monkeypatch):
    panel = _panel(100).assign(foundry_zoo_mom=1.0)          # production already has the name
    _seed(tmp_path, panel[["close"]], ["zoo_mom"])
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards",
                        lambda s, d: [_Card("zoo_mom")])
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24)

    assert df.empty and entries == []                        # never shadow a real feature


def test_write_overlay_emits_parquet_and_manifest(tmp_path):
    panel = _panel(10)
    df = pd.DataFrame({"foundry_x": np.arange(10.0)}, index=panel.index)
    from research.hermes.foundry_bridge import card_to_entry
    entries = [card_to_entry(_Card("x"), horizon_h=24)]

    write_overlay(tmp_path, "eth", df, entries)

    assert (tmp_path / "foundry_overlay_eth.parquet").exists()
    man = json.loads((tmp_path / "foundry_manifest_eth.json").read_text(encoding="utf-8"))
    assert man["factors"][0]["name"] == "foundry_x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_overlay'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/hermes/foundry_bridge.py`:

```python
import argparse
import json
import logging

from research.hermes.candidate_store import _candidate_path, load_candidate_code
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.lib.factor_io import _symbol_short

log = logging.getLogger(__name__)

DEFAULT_CAP = 50


def build_overlay(symbol, manifests_dir, panel, run_sandbox, oos_start,
                  horizon_h: int, cap: int = DEFAULT_CAP):
    """Recompute every reconciling candidate over `panel` and return
    (overlay_df, entries). Any factor that cannot be trusted is DROPPED with a
    log line — a bad factor must never silently poison a backtest, and the
    bridge must never crash the pipeline."""
    cards = [c for c in load_cards(symbol, manifests_dir)
             if getattr(c, "verdict", None) == VERDICT_CANDIDATE]
    # OOM guard: Foundry accumulates candidates over time; an unbounded
    # full-span concat would blow up the stage subprocesses.
    cards.sort(key=lambda c: (getattr(c, "dsr", 0.0) or 0.0), reverse=True)
    cards = cards[:cap]

    cand_path = _candidate_path(symbol, manifests_dir)
    stored = pd.read_parquet(cand_path) if Path(cand_path).exists() else pd.DataFrame()

    cols: dict = {}
    entries: list = []
    for card in cards:
        fid = card.factor_id
        name = f"{FOUNDRY_PREFIX}{fid}"
        if name in panel.columns:
            log.warning("bridge: %s collides with an existing feature; skipping", name)
            continue
        try:
            code, meta = load_candidate_code(fid, symbol, manifests_dir)
        except FileNotFoundError:
            log.warning("bridge: no stored code for %s; skipping", fid)
            continue
        # The card's gross_ic was measured at the horizon Foundry ran with, so
        # reuse THAT per-factor horizon. (research_config's horizons_h is
        # (8, 24, 72, 168) — taking its first element would mislabel the IC as 8h.)
        h = int(meta.get("horizon_h") or horizon_h)
        try:
            series = recompute_full_span(code, panel, run_sandbox)
        except Exception as exc:                     # noqa: BLE001 - degrade, never crash
            log.warning("bridge: recompute failed for %s (%s); skipping", fid, exc)
            continue
        if fid not in stored.columns:
            log.warning("bridge: %s absent from candidate parquet; skipping", fid)
            continue
        if not reconciles_pre_oos(series, stored[fid], oos_start):
            log.warning("bridge: %s pre-oos does not reconcile (non-causal or image "
                        "drift); skipping", fid)
            continue
        cols[name] = series
        entries.append(card_to_entry(card, horizon_h=h))

    if not cols:
        return pd.DataFrame(index=panel.index).iloc[:, :0], []
    df = pd.DataFrame(cols, index=panel.index)
    # Index-alignment defence: a one-tick difference would misalign the whole
    # concat downstream and flood the panel with NaN.
    df = df.reindex(panel.index)
    assert len(df) == len(panel), "overlay lost rows against the production panel"
    return df, entries


def write_overlay(overlay_dir, symbol: str, df: pd.DataFrame, entries: list) -> None:
    """Materialise the per-run overlay. This is a CACHE for one pipeline run —
    it is NOT production. It exists because the stages are separate subprocesses
    and cannot share an in-memory frame."""
    d = Path(overlay_dir)
    d.mkdir(parents=True, exist_ok=True)
    sym = _symbol_short(symbol)
    df.to_parquet(d / f"foundry_overlay_{sym}.parquet")
    (d / f"foundry_manifest_{sym}.json").write_text(
        json.dumps({"factors": [e.model_dump(mode="json") for e in entries]}, indent=2),
        encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_foundry_bridge.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Add the CLI entry point**

Append to `research/hermes/foundry_bridge.py`:

```python
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="foundry-bridge",
        description="Recompute Foundry candidates over the full span into a per-run overlay.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--overlay-dir", required=True, help="per-run cache dir (NOT production)")
    ap.add_argument("--image", required=True, help="sandbox image tag")
    ap.add_argument("--manifests-dir", default=None)
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--timeout-s", type=int, default=120)
    ap.add_argument("--horizon-h", type=int, default=24,
                    help="fallback horizon when a factor's code meta lacks one")
    args = ap.parse_args(argv)

    from research.hermes.foundry_runner import resolve_image_id, load_ohlcv
    from research.hermes.orchestrator import make_run_sandbox
    from research.hermes.sandbox import DockerSandbox
    from research.lib.factor_io import _default_manifests_dir, load_features
    from pipeline.config import load_config

    mdir = Path(args.manifests_dir) if args.manifests_dir else _default_manifests_dir()
    cfg = load_config()
    oos_start = cfg.oos_start

    features = load_features(args.symbol, manifests_dir=mdir)
    ohlcv = load_ohlcv(mdir / f"ohlcv_{_symbol_short(args.symbol)}.parquet")
    panel = features.join(ohlcv, how="left")

    sandbox = DockerSandbox(image=resolve_image_id(args.image),
                            timeout_s=args.timeout_s, allow_unpinned=False)
    run_sandbox = make_run_sandbox(sandbox, mdir / "_foundry_scratch")

    df, entries = build_overlay(args.symbol, mdir, panel, run_sandbox, oos_start,
                                horizon_h=args.horizon_h, cap=args.cap)
    write_overlay(args.overlay_dir, args.symbol, df, entries)
    print(f"foundry bridge: {len(entries)} candidate(s) -> {args.overlay_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Verify the CLI parses and refuses cleanly with no candidates**

Run: `.venv/Scripts/python.exe -m research.hermes.foundry_bridge --help`
Expected: usage text listing `--symbol`, `--overlay-dir`, `--image`

- [ ] **Step 7: Commit**

```bash
git add research/hermes/foundry_bridge.py research/tests/test_hermes_foundry_bridge.py
git commit -m "feat(hermes): build and materialise the per-run Foundry overlay"
```

---

### Task 6: Loader — `include_foundry` union (never computes)

**Files:**
- Modify: `research/lib/factor_io.py`
- Test: `research/tests/test_factor_io_foundry.py`

**Interfaces:**
- Produces:
  - `foundry_enabled() -> bool` — strict `os.getenv("RESEARCH_INCLUDE_FOUNDRY") == "1"`
  - `load_features(symbol, manifests_dir=None, include_foundry=None) -> pd.DataFrame`
  - `load_manifest(symbol, manifests_dir=None, include_foundry=None) -> dict`
  - Overlay dir from `RESEARCH_FOUNDRY_OVERLAY_DIR`.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_factor_io_foundry.py`:

```python
import json
import numpy as np
import pandas as pd
import pytest
from research.lib.factor_io import foundry_enabled, load_features


@pytest.fixture
def prod(tmp_path):
    idx = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame({"funding_z": np.arange(10.0)}, index=idx)
    df.to_parquet(tmp_path / "features_eth.parquet")
    return tmp_path, idx


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("0", False), ("", False), ("false", False), (None, False),
])
def test_foundry_enabled_requires_the_exact_string_1(monkeypatch, val, expected):
    # bool(os.getenv(...)) would make "0" and "false" TRUE — i.e. on by default.
    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    if val is not None:
        monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", val)
    assert foundry_enabled() is expected


def test_load_features_default_ignores_the_overlay(prod, tmp_path, monkeypatch):
    mdir, idx = prod
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_x": np.ones(10)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.delenv("RESEARCH_INCLUDE_FOUNDRY", raising=False)
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_features("eth", manifests_dir=mdir)

    assert list(out.columns) == ["funding_z"]      # production path untouched


def test_load_features_unions_the_overlay_when_enabled(prod, tmp_path, monkeypatch):
    mdir, idx = prod
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_x": np.ones(10)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_features("eth", manifests_dir=mdir)

    assert set(out.columns) == {"funding_z", "foundry_x"}
    assert out.index.equals(idx)


def test_overlay_never_overwrites_a_production_column(prod, tmp_path, monkeypatch):
    mdir, idx = prod
    ov = tmp_path / "ov"; ov.mkdir()
    # a hostile/buggy overlay carrying a production name must not shadow it
    pd.DataFrame({"funding_z": np.full(10, -999.0)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    out = load_features("eth", manifests_dir=mdir)

    assert (out["funding_z"].to_numpy() == np.arange(10.0)).all()   # production wins
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_factor_io_foundry.py -q`
Expected: FAIL — `ImportError: cannot import name 'foundry_enabled'`

- [ ] **Step 3: Write minimal implementation**

In `research/lib/factor_io.py`, add near the top:

```python
import os

ENV_INCLUDE_FOUNDRY = "RESEARCH_INCLUDE_FOUNDRY"
ENV_FOUNDRY_OVERLAY_DIR = "RESEARCH_FOUNDRY_OVERLAY_DIR"


def foundry_enabled() -> bool:
    """Strict '1' comparison. bool(os.getenv(...)) would treat "0" and "false"
    as True — i.e. the overlay would be ON by default, which would leak research
    factors into every read. Off unless explicitly '1'."""
    return os.getenv(ENV_INCLUDE_FOUNDRY) == "1"


def _overlay_dir():
    d = os.getenv(ENV_FOUNDRY_OVERLAY_DIR)
    return Path(d) if d else None
```

Then rewrite `load_features` (keep the existing body as `_load_production_features`):

```python
def load_features(symbol: str, manifests_dir: Path | None = None,
                  include_foundry: bool | None = None) -> pd.DataFrame:
    """Production features, optionally unioned with a per-run Foundry overlay.

    This is a READ path only: it never runs the sandbox and never computes a
    factor. The bridge materialises the overlay ONCE per pipeline run; the
    stages are separate subprocesses, so computing here would wake Docker once
    per stage and could yield different values to different stages.
    """
    prod = _load_production_features(symbol, manifests_dir=manifests_dir)
    use = foundry_enabled() if include_foundry is None else include_foundry
    if not use:
        return prod
    d = _overlay_dir()
    if d is None:
        return prod
    path = d / f"foundry_overlay_{_symbol_short(symbol)}.parquet"
    if not path.exists():
        return prod
    overlay = pd.read_parquet(path)
    # production always wins a name clash — an overlay must never shadow a
    # feature the rest of the system (and the trader) treats as ground truth.
    overlay = overlay.drop(columns=[c for c in overlay.columns if c in prod.columns],
                           errors="ignore")
    if overlay.empty:
        return prod
    return prod.join(overlay.reindex(prod.index), how="left")


def load_manifest(symbol: str, manifests_dir: Path | None = None,
                  include_foundry: bool | None = None) -> dict:
    """stage1's factor_<sym>.json, optionally with the per-run Foundry entries
    appended. Foundry entries carry Foundry's own (pre-oos) statistics."""
    mdir = Path(manifests_dir) if manifests_dir is not None else _default_manifests_dir()
    sym = _symbol_short(symbol)
    manifest = json.loads((mdir / f"factor_{sym}.json").read_text(encoding="utf-8"))
    use = foundry_enabled() if include_foundry is None else include_foundry
    d = _overlay_dir()
    if not use or d is None:
        return manifest
    path = d / f"foundry_manifest_{sym}.json"
    if not path.exists():
        return manifest
    extra = json.loads(path.read_text(encoding="utf-8")).get("factors", [])
    existing = {f["name"] for f in manifest.get("factors", [])}
    manifest["factors"] = manifest.get("factors", []) + [
        f for f in extra if f["name"] not in existing]
    return manifest
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_factor_io_foundry.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Verify nothing else regressed**

Run: `.venv/Scripts/python.exe -m pytest research/tests/ -q`
Expected: PASS, no failures

- [ ] **Step 6: Commit**

```bash
git add research/lib/factor_io.py research/tests/test_factor_io_foundry.py
git commit -m "feat(research): loader unions a per-run Foundry overlay behind an explicit flag"
```

---

### Task 7: Route stage2b / stage3 feature reads through `load_features`

`stage3_backtest.py:268` reads `features_<short>.parquet` directly, so the overlay would be invisible to the backtest. Converge the read path.

**Files:**
- Modify: `research/pipeline/stage3_backtest.py` (around line 268)
- Modify: `research/pipeline/stage2b_compile_signal.py` (its feature read)
- Test: `research/tests/test_stage3_foundry_overlay.py`

**Interfaces:**
- Consumes: `load_features` (Task 6).

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_stage3_foundry_overlay.py`:

```python
import numpy as np
import pandas as pd
from research.lib.factor_io import load_features


def test_stage3_feature_read_sees_the_foundry_overlay(tmp_path, monkeypatch):
    # stage3 must resolve a foundry_ column across the FULL span (train + OOS),
    # otherwise a Foundry-derived strategy can never be OOS-validated.
    idx = pd.date_range("2024-12-30", periods=100, freq="1h", tz="UTC")   # crosses 2025-01-01
    pd.DataFrame({"funding_z": np.arange(100.0)}, index=idx).to_parquet(
        tmp_path / "features_eth.parquet")
    ov = tmp_path / "ov"; ov.mkdir()
    pd.DataFrame({"foundry_zoo_mom": np.arange(100.0)}, index=idx).to_parquet(
        ov / "foundry_overlay_eth.parquet")
    monkeypatch.setenv("RESEARCH_INCLUDE_FOUNDRY", "1")
    monkeypatch.setenv("RESEARCH_FOUNDRY_OVERLAY_DIR", str(ov))

    panel = load_features("eth", manifests_dir=tmp_path)

    assert "foundry_zoo_mom" in panel.columns
    oos = panel[panel.index >= pd.Timestamp("2025-01-01", tz="UTC")]
    assert not oos.empty
    assert oos["foundry_zoo_mom"].notna().all()      # OOS window is populated
```

- [ ] **Step 2: Run the test — expect PASS, and understand why**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_stage3_foundry_overlay.py -q`
Expected: **PASS immediately.** This is a *contract* test, not a red-green test: it pins the loader behaviour stage3 depends on (a `foundry_` column, populated across the OOS window). It passes because Task 6 already built that. The actual stage3 edit in Step 3 is a read-path refactor whose safety net is the existing stage3 suite staying green in Step 4 — the default path must not change, because the flag is off.

- [ ] **Step 3: Converge stage3's read path**

In `research/pipeline/stage3_backtest.py`, find the direct read near line 268:

```python
            _REPO_ROOT / "research" / "manifests" / f"features_{short}.parquet"
```

Replace that direct `pd.read_parquet(...)` call with the loader (add the import beside the other `lib` imports):

```python
from lib.factor_io import load_features
```

```python
    # Route through load_features so a per-run Foundry overlay is visible here.
    # The loader NEVER computes; the bridge materialised the overlay already.
    features = load_features(symbol, manifests_dir=_REPO_ROOT / "research" / "manifests")
```

Do the same for the feature read in `research/pipeline/stage2b_compile_signal.py`.

- [ ] **Step 4: Run the research suite**

Run: `.venv/Scripts/python.exe -m pytest research/tests/ -q`
Expected: PASS, no failures (stage3's existing tests must still pass — the default path is unchanged because the flag is off)

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/stage3_backtest.py research/pipeline/stage2b_compile_signal.py research/tests/test_stage3_foundry_overlay.py
git commit -m "refactor(pipeline): route stage2b/stage3 feature reads through load_features"
```

---

### Task 8: Promote guard — refuse the production dead-end

A strategy selected on a `foundry_` factor **cannot go live**: the trader reads `factor_values_<sym>.parquet`, which `scripts/refresh_factors.sh` recomputes from the *library* code — and a Foundry factor's code is not in the library, so it would never refresh and the trader's staleness guard would pause it. Refuse loudly instead of letting the user hit that wall.

**Files:**
- Modify: `research/hermes/promote.py`
- Test: `research/tests/test_hermes_promote.py`

**Interfaces:**
- Consumes: `FOUNDRY_PREFIX` (Task 2).
- Produces: `assert_promotable_factor_names(names: list[str]) -> None` — raises `PromoteRefused`.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_promote.py`:

```python
def test_promote_refuses_a_strategy_depending_on_a_foundry_factor():
    # The bridge only feeds RESEARCH. Production recomputes factors from the
    # library code, which has no Foundry factor in it -> the factor would go
    # stale immediately and pause the trader. Refuse, don't dead-end.
    import pytest
    from research.hermes.promote import assert_promotable_factor_names, PromoteRefused

    assert_promotable_factor_names(["funding_z", "basis_rel"])      # library-only: fine

    with pytest.raises(PromoteRefused, match="foundry_zoo_mom"):
        assert_promotable_factor_names(["funding_z", "foundry_zoo_mom"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_promote.py -q`
Expected: FAIL — `ImportError: cannot import name 'assert_promotable_factor_names'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/hermes/promote.py`:

```python
from research.hermes.foundry_bridge import FOUNDRY_PREFIX


def assert_promotable_factor_names(names) -> None:
    """Refuse a strategy that depends on a Foundry factor.

    The bridge makes Foundry factors available to RESEARCH only. Production
    factor values are recomputed periodically from the FACTOR LIBRARY's code
    (scripts/refresh_factors.sh -> factor_values_<sym>.parquet, read by the live
    trader). A forged Foundry factor's code is not in that library, so once
    promoted its values would never refresh: the trader would trip its
    `factor data stale:` guard and pause.

    Promoting the factor's CODE into the production library is a separate piece
    of work; until it exists, this refuses loudly instead of letting a selected
    strategy dead-end at deployment.
    """
    offenders = [n for n in names if str(n).startswith(FOUNDRY_PREFIX)]
    if offenders:
        raise PromoteRefused(
            f"strategy depends on Foundry factor(s) {offenders}: their code is not in "
            "the production factor library, so production could never refresh them "
            "(the trader would pause on stale factor data). Promote the factor CODE "
            "into the production feature path first."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_promote.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/promote.py research/tests/test_hermes_promote.py
git commit -m "feat(hermes): refuse promoting a strategy that depends on a Foundry factor"
```

---

### Task 9: Overlay cache lifecycle + subprocess env (dashboard scope)

**Files:**
- Modify: `dashboard/server/pipeline_manager.py`
- Test: `dashboard/server/test_pipeline_manager_overlay.py`

> **pytest scope change:** this task runs `cd dashboard/server && pytest -q`. Do NOT run it from repo root.

**Interfaces:**
- Produces: `cleanup_overlay(job_dir) -> None` on the manager; env passed to stages as `{**os.environ, **overrides}`.

- [ ] **Step 1: Write the failing test**

Create `dashboard/server/test_pipeline_manager_overlay.py`:

```python
import os
from pathlib import Path
import pipeline_manager as pm


def test_cleanup_overlay_removes_the_per_run_cache(tmp_path):
    # the overlay is a per-run cache; leaving it behind accumulates orphan
    # parquet files that fill the disk.
    (tmp_path / "foundry_overlay_eth.parquet").write_bytes(b"x")
    (tmp_path / "foundry_manifest_eth.json").write_text("{}", encoding="utf-8")

    pm.cleanup_overlay(tmp_path)

    assert not (tmp_path / "foundry_overlay_eth.parquet").exists()
    assert not (tmp_path / "foundry_manifest_eth.json").exists()


def test_stage_env_inherits_the_parent_environment(monkeypatch):
    # passing only overrides to subprocess would wipe PATH and break the stage.
    monkeypatch.setenv("PATH", "/usr/bin")
    env = pm.stage_env({"RESEARCH_INCLUDE_FOUNDRY": "1"})
    assert env["PATH"] == "/usr/bin"
    assert env["RESEARCH_INCLUDE_FOUNDRY"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard/server && pytest test_pipeline_manager_overlay.py -q`
Expected: FAIL — `AttributeError: module 'pipeline_manager' has no attribute 'cleanup_overlay'`

- [ ] **Step 3: Write minimal implementation**

Add to `dashboard/server/pipeline_manager.py`:

```python
import os


def stage_env(overrides: dict) -> dict:
    """Environment for a stage subprocess. MUST inherit the parent env: passing
    only overrides would drop PATH and the stage would fail to start."""
    return {**os.environ, **{k: str(v) for k, v in overrides.items()}}


def cleanup_overlay(job_dir) -> None:
    """Delete the per-run Foundry overlay cache. It belongs to one pipeline run;
    left behind, these parquet files accumulate as orphans and fill the disk."""
    d = Path(job_dir)
    if not d.exists():
        return
    for p in list(d.glob("foundry_overlay_*.parquet")) + list(d.glob("foundry_manifest_*.json")):
        try:
            p.unlink()
        except OSError as exc:                       # noqa: BLE001 - teardown must not crash
            logger.warning("could not remove overlay cache %s: %s", p, exc)
```

Then call `cleanup_overlay(<job dir>)` at the end of `PipelineManager.execute_job`, in a `finally` so it runs on failure too.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard/server && pytest test_pipeline_manager_overlay.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the dashboard suite**

Run: `cd dashboard/server && pytest -q`
Expected: PASS, no failures

- [ ] **Step 6: Commit**

```bash
git add dashboard/server/pipeline_manager.py dashboard/server/test_pipeline_manager_overlay.py
git commit -m "feat(dashboard): clean the per-run Foundry overlay and inherit stage env"
```

---

## Final verification

- [ ] **Run all three scopes** (never in one call):

```bash
.venv/Scripts/python.exe -m pytest research/tests/ -q
cd dashboard/server && pytest -q && cd ../..
.venv/Scripts/python.exe -m pytest agent/tests/ -q
```

Expected: all green.

- [ ] **End-to-end smoke against real manifests** (costs nothing — no LLM; the bridge only re-runs stored code):

```bash
.venv/Scripts/python.exe -m research.hermes.foundry_bridge \
  --symbol eth \
  --overlay-dir C:/Users/cool6/Vibe-Trading/runs/bridge_smoke \
  --image talos-sandbox:test \
  --manifests-dir C:/Users/cool6/Vibe-Trading/research/manifests
```

Expected: `foundry bridge: 0 candidate(s) -> ...` today (the last real Foundry run produced 4 graveyard, 0 candidates). To exercise a non-zero path, first run Foundry with a larger batch until at least one factor passes the gate.

**Absolute paths are required for `--manifests-dir`**: a relative path makes Docker read the bind-mount source as a *volume name* and the sandbox fails to start.

- [ ] **Confirm production was never touched:**

```bash
git status --short research/manifests/
```

Expected: no modification to `features_*.parquet` or `factor_values_*.parquet`.
