# Factor Induction (因子轉正) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move a vetted Foundry factor's `compute()` code into the production factor library (committed, importable) so `stage0a` computes it deterministically every day and the trader can read it — unblocking deployment of Foundry-derived strategies.

**Architecture:** A human-gated `induct` CLI re-validates a stored Foundry factor (verdict/sha, AST allowlist, full-span PIT, determinism, path-consistency reconcile) then writes it as a committed importable module + meta + golden test. `stage0a.build_feature_dict` scans `research/lib/inducted/<sym>/`, imports each under a unique name, and computes it (soft-fail + hot kill-switch). B's promote guard consults the same scan.

**Tech Stack:** Python 3.11, pytest, importlib, pandas, Docker (PIT re-check sandbox).

## Global Constraints

- Python for every command: `.venv/Scripts/python.exe` from repo root. All tasks are **research scope**: `.venv/Scripts/python.exe -m pytest research/tests/ -q`.
- Foundry code contract: exactly one `compute(df) -> pd.Series` preserving `df`'s tz-aware UTC DatetimeIndex.
- Inducted code is **committed** (version-controlled production code), NOT left in the gitignored `candidate_features/`.
- Discovery is a **directory scan** — a factor is inducted iff `research/lib/inducted/<sym>/<id>.py` AND `<id>.meta.json` exist. No central registry.
- `compute_inducted` imports each module under a **globally-unique name** `inducted_{symbol}_{factor_id}` and **soft-fails per factor** (a raise → NaN + log, never breaks the batch). It honours a hot kill-switch blacklist (`INDUCTED_BLACKLIST_FILE`, default `runs/inducted_blacklist.txt`).
- Golden tests assert with tolerance: `pandas.testing.assert_series_equal(got, expected, rtol=1e-5, atol=1e-8)`.
- Never write production `features_<sym>.parquet` / `factor_values_<sym>.parquet` directly; induction only writes committed source + a golden fixture.

## Existing code you will use

```python
# research/hermes/candidate_store.py
def load_candidate_code(factor_id, symbol, manifests_dir) -> tuple[str, dict]   # (code, meta{code_sha256,...}); FileNotFoundError if absent
def _candidate_path(symbol, manifests_dir) -> Path                              # cand_<sym>.parquet
# research/hermes/evidence_store.py / evidence_card.py
def load_cards(symbol, manifests_dir) -> list[EvidenceCard]                     # card.factor_id, card.verdict
VERDICT_CANDIDATE = "candidate"
# research/hermes/sandbox_ast.py
def check_source(src: str) -> None                                             # raises UnsafeCodeError
class UnsafeCodeError(HermesGuardError, ValueError)
# research/hermes/forge.py / pit.py
def pit_check_via_sandbox(code, panel, baseline, run, atol=1e-9, rtol=1e-9) -> None   # raises LookaheadError
class LookaheadError
# research/hermes/foundry_bridge.py
FOUNDRY_PREFIX = "foundry_"
def recompute_full_span(code, panel, run_sandbox) -> pd.Series
def reconciles_pre_oos(recomputed, stored, oos_start, atol=1e-8) -> bool
# research/hermes/orchestrator.py
def make_run_sandbox(sandbox, scratch_dir) -> Callable[[str, pd.DataFrame], pd.Series]
# research/hermes/promote.py
def assert_promotable_factor_names(names) -> None    # (A) refuses any foundry_ name; to gain a `symbol` param
# research/pipeline/stage0a_features.py
def build_feature_dict(candles, config, ..., oi_ls_df=None) -> dict[str, pd.Series]   # ends `return features` (line ~323)
```

## File structure

| File | Responsibility |
|---|---|
| `research/lib/inducted_factors.py` (create) | `inducted_dir`, `inducted_names`, `compute_inducted` (scan + import + soft-fail + kill-switch) |
| `research/lib/inducted/` (create) | committed inducted modules + `<id>.meta.json` land here (empty at first, with a `.gitkeep`) |
| `research/pipeline/stage0a_features.py` (modify) | `build_feature_dict` gains `symbol=None`; merges `compute_inducted` before return; caller passes symbol |
| `research/hermes/promote.py` (modify) | `assert_promotable_factor_names(names, symbol)` allows factors inducted for that symbol |
| `research/hermes/induct.py` (create) | the induct CLI: 6 gates + `--confirm` write |
| `research/tests/inducted/` (create) | committed golden fixtures + generated tests |

---

### Task 1: `inducted_names` — directory-scan discovery

**Files:**
- Create: `research/lib/inducted_factors.py`
- Create: `research/lib/inducted/.gitkeep` (empty)
- Test: `research/tests/test_inducted_factors.py`

**Interfaces:**
- Produces:
  - `inducted_dir(symbol, root=None) -> Path` → `<repo>/research/lib/inducted/<short>/`
  - `inducted_names(symbol, root=None) -> set[str]` — names with BOTH `<id>.py` and `<id>.meta.json`.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_inducted_factors.py`:

```python
from research.lib.inducted_factors import inducted_dir, inducted_names


def test_inducted_names_needs_both_py_and_meta(tmp_path):
    d = inducted_dir("eth", root=tmp_path)
    d.mkdir(parents=True)
    (d / "foundry_a.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")
    (d / "foundry_a.meta.json").write_text("{}", encoding="utf-8")
    (d / "foundry_b.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")  # no meta

    assert inducted_names("eth", root=tmp_path) == {"foundry_a"}


def test_inducted_names_empty_when_dir_absent(tmp_path):
    assert inducted_names("eth", root=tmp_path) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_inducted_factors.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.lib.inducted_factors'`

- [ ] **Step 3: Write minimal implementation**

Create `research/lib/inducted_factors.py`:

```python
"""Inducted factors: vetted Foundry factor code that has been promoted into the
production factor library (committed, importable). stage0a computes these daily.

Discovery is a directory scan (no central registry -> no merge conflicts):
a factor is inducted iff research/lib/inducted/<sym>/<id>.py AND <id>.meta.json
both exist.
"""
from __future__ import annotations

from pathlib import Path

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[2]                      # research/lib/inducted_factors.py -> repo root


def _symbol_short(symbol: str) -> str:
    s = str(symbol or "").strip()
    if "/" in s:
        s = s.split("/")[0]
    if "-" in s:
        s = s.split("-")[0]
    return s.lower()


def inducted_dir(symbol: str, root=None) -> Path:
    base = Path(root) if root is not None else (_REPO_ROOT / "research" / "lib")
    return base / "inducted" / _symbol_short(symbol)


def inducted_names(symbol: str, root=None) -> set:
    d = inducted_dir(symbol, root=root)
    if not d.is_dir():
        return set()
    return {p.stem for p in d.glob("*.py")
            if p.stem != "__init__" and (d / f"{p.stem}.meta.json").exists()}
```

Create an empty `research/lib/inducted/.gitkeep`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_inducted_factors.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/lib/inducted_factors.py research/lib/inducted/.gitkeep research/tests/test_inducted_factors.py
git commit -m "feat(research): inducted factor directory-scan discovery"
```

---

### Task 2: `compute_inducted` — import, soft-fail, kill-switch

**Files:**
- Modify: `research/lib/inducted_factors.py`
- Test: `research/tests/test_inducted_factors.py` (append)

**Interfaces:**
- Consumes: `inducted_dir`, `inducted_names`.
- Produces: `compute_inducted(panel, symbol, root=None) -> dict[str, pd.Series]`.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_inducted_factors.py`:

```python
import numpy as np
import pandas as pd
from research.lib.inducted_factors import compute_inducted


def _seed(tmp_path, sym, fid, body):
    d = inducted_dir(sym, root=tmp_path); d.mkdir(parents=True, exist_ok=True)
    (d / f"{fid}.py").write_text(body, encoding="utf-8")
    (d / f"{fid}.meta.json").write_text("{}", encoding="utf-8")


def _panel(n=10):
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.arange(float(n))}, index=idx)


def test_compute_inducted_imports_and_computes(tmp_path):
    _seed(tmp_path, "eth", "foundry_x", "def compute(df):\n    return df['close'] * 2\n")
    out = compute_inducted(_panel(), "eth", root=tmp_path)
    assert list(out) == ["foundry_x"]
    assert (out["foundry_x"].to_numpy() == np.arange(10.0) * 2).all()


def test_compute_inducted_soft_fails_a_raising_factor(tmp_path):
    _seed(tmp_path, "eth", "foundry_ok", "def compute(df):\n    return df['close']\n")
    _seed(tmp_path, "eth", "foundry_bad", "def compute(df):\n    return df['missing_col']\n")
    out = compute_inducted(_panel(), "eth", root=tmp_path)
    assert "foundry_ok" in out and "foundry_bad" not in out       # bad one dropped, good one survived


def test_compute_inducted_honours_kill_switch(tmp_path, monkeypatch):
    _seed(tmp_path, "eth", "foundry_x", "def compute(df):\n    return df['close']\n")
    bl = tmp_path / "bl.txt"; bl.write_text("foundry_x\n", encoding="utf-8")
    monkeypatch.setenv("INDUCTED_BLACKLIST_FILE", str(bl))
    assert compute_inducted(_panel(), "eth", root=tmp_path) == {}   # blacklisted -> skipped


def test_same_named_helpers_dont_clobber_across_factors(tmp_path):
    _seed(tmp_path, "eth", "foundry_a",
          "def helper(df):\n    return df['close']\ndef compute(df):\n    return helper(df) + 1\n")
    _seed(tmp_path, "eth", "foundry_b",
          "def helper(df):\n    return df['close']\ndef compute(df):\n    return helper(df) + 100\n")
    out = compute_inducted(_panel(), "eth", root=tmp_path)
    assert (out["foundry_a"].to_numpy() == np.arange(10.0) + 1).all()
    assert (out["foundry_b"].to_numpy() == np.arange(10.0) + 100).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_inducted_factors.py -q`
Expected: FAIL — `ImportError: cannot import name 'compute_inducted'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/lib/inducted_factors.py`:

```python
import importlib.util
import logging
import os

log = logging.getLogger(__name__)


def _blacklisted() -> set:
    p = os.environ.get("INDUCTED_BLACKLIST_FILE", str(_REPO_ROOT / "runs" / "inducted_blacklist.txt"))
    path = Path(p)
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _load_compute(symbol: str, factor_id: str, path: Path):
    # A globally-unique module name -> no sys.modules cache clobber across
    # symbols, and each module is its own namespace (helpers can't collide).
    name = f"inducted_{_symbol_short(symbol)}_{factor_id}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.compute


def compute_inducted(panel, symbol: str, root=None) -> dict:
    """Compute every inducted factor for `symbol` over the full-library `panel`.

    Soft-fails per factor: a factor that raises (e.g. a missing column) is
    dropped with a log line -- it must never break stage0a's daily batch. A
    blacklisted factor (hot kill-switch) is skipped entirely.
    """
    d = inducted_dir(symbol, root=root)
    black = _blacklisted()
    out: dict = {}
    for fid in sorted(inducted_names(symbol, root=root)):
        if fid in black:
            log.warning("inducted factor %s is blacklisted (kill-switch); skipping", fid)
            continue
        try:
            compute = _load_compute(symbol, fid, d / f"{fid}.py")
            series = compute(panel)
            out[fid] = series.reindex(panel.index) if hasattr(series, "reindex") else series
        except Exception as exc:                    # noqa: BLE001 - soft-fail, never break the batch
            log.error("inducted factor %s failed (%s); dropping for this run", fid, exc)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_inducted_factors.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add research/lib/inducted_factors.py research/tests/test_inducted_factors.py
git commit -m "feat(research): compute_inducted with unique-name import, soft-fail, kill-switch"
```

---

### Task 3: `stage0a` integration

**Files:**
- Modify: `research/pipeline/stage0a_features.py` (`build_feature_dict` + its caller ~line 640)
- Test: `research/tests/test_stage0a_inducted.py`

**Interfaces:**
- Consumes: `compute_inducted`.
- Produces: `build_feature_dict(..., oi_ls_df=None, symbol=None)` merges inducted factors over the full-library panel when `symbol` is set.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_stage0a_inducted.py`:

```python
import numpy as np
import pandas as pd


def test_build_feature_dict_merges_inducted_over_full_library(tmp_path, monkeypatch):
    # an inducted factor may reference a library column; it must see the fully
    # built library panel and appear in the output.
    import research.pipeline.stage0a_features as s0
    from research.lib.inducted_factors import inducted_dir

    d = inducted_dir("eth", root=tmp_path); d.mkdir(parents=True)
    # depends on a library feature 'atr_14' -> proves it sees the built library
    (d / "foundry_x.py").write_text("def compute(df):\n    return df['atr_14'] * 10\n", encoding="utf-8")
    (d / "foundry_x.meta.json").write_text("{}", encoding="utf-8")

    idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
    monkeypatch.setattr(s0, "compute_inducted",
                        lambda panel, symbol, root=None: __import__("research.lib.inducted_factors",
                        fromlist=["compute_inducted"]).compute_inducted(panel, symbol, root=tmp_path))

    features = {"atr_14": pd.Series(np.arange(5.0), index=idx)}
    panel = pd.DataFrame({"close": np.arange(5.0)}, index=idx).assign(**features)
    out = s0._merge_inducted(features, panel, "eth")

    assert "foundry_x" in out
    assert (out["foundry_x"].to_numpy() == np.arange(5.0) * 10).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_stage0a_inducted.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_merge_inducted'`

- [ ] **Step 3: Write minimal implementation**

In `research/pipeline/stage0a_features.py`, add the import near the other `lib` imports:

```python
from lib.inducted_factors import compute_inducted
```

Add a small helper (testable in isolation) above `build_feature_dict`:

```python
def _merge_inducted(features: dict, panel: pd.DataFrame, symbol) -> dict:
    """Merge inducted factors into the feature dict, computed over the FULL
    library panel (so an inducted factor may reference any library column)."""
    if symbol:
        features.update(compute_inducted(panel, symbol))
    return features
```

Add `symbol=None` to `build_feature_dict`'s signature (after `oi_ls_df`). Replace `return features` (line ~323) with:

```python
    # Inducted factors LAST: they may reference any library column, so build the
    # full-library panel first, then compute them over it.
    if symbol:
        panel = candles.copy()
        for _name, _series in features.items():
            panel[_name] = _series
        _merge_inducted(features, panel, symbol)
    return features
```

At the caller (~line 640), pass the symbol: `feature_dict = build_feature_dict(candles, config, ..., oi_ls_df=oi_ls_df, symbol=sym)` — use whatever local holds the current symbol (e.g. `sym`/`symbol` in that loop).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_stage0a_inducted.py -q`
Expected: PASS

- [ ] **Step 5: Run the research suite (no regression — default has no inducted factors)**

Run: `.venv/Scripts/python.exe -m pytest research/tests/ -q`
Expected: PASS (existing stage0a tests unaffected — `inducted/` is empty, `compute_inducted` returns `{}`)

- [ ] **Step 6: Commit**

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_inducted.py
git commit -m "feat(pipeline): stage0a computes inducted factors over the full-library panel"
```

---

### Task 4: promote-guard consults the induction scan

**Files:**
- Modify: `research/hermes/promote.py`
- Test: `research/tests/test_hermes_promote.py` (append)

**Interfaces:**
- Consumes: `inducted_names`, `FOUNDRY_PREFIX`.
- Produces: `assert_promotable_factor_names(names, symbol)` — allows a `foundry_` factor that is inducted for `symbol`.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_promote.py`:

```python
def test_promote_guard_allows_an_inducted_factor_for_that_symbol(tmp_path, monkeypatch):
    import pytest
    from research.hermes import promote as pm
    from research.hermes.promote import assert_promotable_factor_names, PromoteRefused

    # eth has foundry_x inducted; btc does not
    from research.lib.inducted_factors import inducted_dir
    d = inducted_dir("eth", root=tmp_path); d.mkdir(parents=True)
    (d / "foundry_x.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")
    (d / "foundry_x.meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pm, "inducted_names",
                        lambda symbol: __import__("research.lib.inducted_factors",
                        fromlist=["inducted_names"]).inducted_names(symbol, root=tmp_path))

    assert_promotable_factor_names(["funding_z", "foundry_x"], "eth")     # inducted -> allowed

    with pytest.raises(PromoteRefused, match="foundry_x"):
        assert_promotable_factor_names(["foundry_x"], "btc")             # not inducted for btc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_promote.py::test_promote_guard_allows_an_inducted_factor_for_that_symbol -q`
Expected: FAIL — `TypeError: assert_promotable_factor_names() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Write minimal implementation**

In `research/hermes/promote.py`, add the import:

```python
from research.lib.inducted_factors import inducted_names
```

Replace `assert_promotable_factor_names`:

```python
def assert_promotable_factor_names(names, symbol) -> None:
    """Refuse a strategy that depends on a Foundry factor NOT yet inducted for
    this symbol. An inducted factor's code is in the production library, so the
    trader can recompute it -> the strategy is safe to deploy."""
    inducted = inducted_names(symbol)
    offenders = [n for n in names
                 if str(n).startswith(FOUNDRY_PREFIX) and n not in inducted]
    if offenders:
        raise PromoteRefused(
            f"strategy depends on non-inducted Foundry factor(s) {offenders} for "
            f"{symbol}: induct them first (research.hermes.induct) so production "
            "can recompute them, or the trader would pause on stale factor data."
        )
```

Update the call site in `promote_candidate` (and any caller) to pass the symbol it already has: `assert_promotable_factor_names(names, symbol)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_promote.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/promote.py research/tests/test_hermes_promote.py
git commit -m "feat(hermes): promote guard allows a Foundry factor inducted for the symbol"
```

---

### Task 5: `induct` CLI — load + verify gates (verdict/sha/AST)

**Files:**
- Create: `research/hermes/induct.py`
- Test: `research/tests/test_hermes_induct.py`

**Interfaces:**
- Consumes: `load_candidate_code`, `load_cards`, `VERDICT_CANDIDATE`, `check_source`, `UnsafeCodeError`.
- Produces:
  - `class InductRefused(HermesGuardError, RuntimeError)`
  - `verify_gates(factor_id, symbol, manifests_dir) -> str` — returns the code text; raises `InductRefused` on a bad verdict / sha mismatch / AST violation.

- [ ] **Step 1: Write the failing test**

Create `research/tests/test_hermes_induct.py`:

```python
import hashlib, json
import pytest
from research.hermes import induct as ind
from research.hermes.induct import verify_gates, InductRefused
from research.hermes.candidate_store import write_candidate_code


def _card(fid, verdict="candidate"):
    class C:  # duck-typed EvidenceCard
        factor_id = fid; 
    C.verdict = verdict
    return C


def test_verify_gates_returns_code_for_a_clean_candidate(tmp_path, monkeypatch):
    code = "def compute(df):\n    return df['close'].rolling(3).mean()\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code,
                         {"code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x")])
    assert verify_gates("foundry_x", "eth", tmp_path) == code


def test_verify_gates_refuses_non_candidate(tmp_path, monkeypatch):
    code = "def compute(df):\n    return df['close']\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code,
                         {"code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x", verdict="graveyard")])
    with pytest.raises(InductRefused, match="verdict"):
        verify_gates("foundry_x", "eth", tmp_path)


def test_verify_gates_refuses_sha_mismatch(tmp_path, monkeypatch):
    write_candidate_code("foundry_x", "eth", tmp_path,
                         "def compute(df):\n    return df['close']\n", {"code_sha256": "deadbeef"})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x")])
    with pytest.raises(InductRefused, match="sha"):
        verify_gates("foundry_x", "eth", tmp_path)


def test_verify_gates_refuses_unsafe_ast(tmp_path, monkeypatch):
    code = "import os\ndef compute(df):\n    return df['close']\n"
    write_candidate_code("foundry_x", "eth", tmp_path, code,
                         {"code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    monkeypatch.setattr(ind, "load_cards", lambda s, d: [_card("foundry_x")])
    with pytest.raises(InductRefused, match="AST|import"):
        verify_gates("foundry_x", "eth", tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_induct.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.hermes.induct'`

- [ ] **Step 3: Write minimal implementation**

Create `research/hermes/induct.py`:

```python
"""Factor induction (轉正): move a vetted Foundry factor's compute() code into
the production factor library. Human-gated (--confirm), re-validated hard.

Like promote.py, this is a privileged production write and is NOT exported from
research.hermes.__init__; it runs only when a human invokes it.
"""
from __future__ import annotations

import hashlib

from research.hermes.candidate_store import load_candidate_code
from research.hermes.errors import HermesGuardError
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.hermes.sandbox_ast import check_source, UnsafeCodeError


class InductRefused(HermesGuardError, RuntimeError):
    """Induction is not allowed (bad verdict / sha / AST / PIT / determinism / divergence / no confirm)."""


def verify_gates(factor_id: str, symbol: str, manifests_dir) -> str:
    """Load the stored code and run the cheap gates (verdict, sha, AST). Returns
    the code text. Raises InductRefused on any violation."""
    cards = {c.factor_id: c for c in load_cards(symbol, manifests_dir)}
    card = cards.get(factor_id)
    if card is None:
        raise InductRefused(f"no evidence card for {symbol}:{factor_id}")
    if card.verdict != VERDICT_CANDIDATE:
        raise InductRefused(f"{symbol}:{factor_id} verdict={card.verdict!r}, not a candidate")

    code, meta = load_candidate_code(factor_id, symbol, manifests_dir)
    actual = hashlib.sha256(code.encode()).hexdigest()
    if meta.get("code_sha256") and meta["code_sha256"] != actual:
        raise InductRefused(f"{symbol}:{factor_id} code sha mismatch (tamper?)")
    try:
        check_source(code)
    except UnsafeCodeError as exc:
        raise InductRefused(f"{symbol}:{factor_id} failed AST allowlist: {exc}") from exc
    return code
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_induct.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/induct.py research/tests/test_hermes_induct.py
git commit -m "feat(hermes): induct verify_gates (verdict/sha/AST)"
```

---

### Task 6: `induct` CLI — re-validation gates (PIT, determinism, path reconcile)

**Files:**
- Modify: `research/hermes/induct.py`
- Test: `research/tests/test_hermes_induct.py` (append)

**Interfaces:**
- Consumes: `recompute_full_span`, `reconciles_pre_oos` (bridge), `pit_check_via_sandbox`, `LookaheadError`, `_candidate_path`.
- Produces: `revalidate(code, factor_id, symbol, panel, run_sandbox, oos_start, manifests_dir) -> None` — raises `InductRefused` on a PIT leak / non-determinism / bridge-value divergence.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_induct.py`:

```python
import numpy as np, pandas as pd
from research.hermes.induct import revalidate


def _panel(n=120, start="2024-11-01"):
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.arange(float(n))}, index=idx)


def test_revalidate_refuses_non_deterministic(tmp_path, monkeypatch):
    panel = _panel()
    # run_sandbox returns a DIFFERENT series each call -> non-deterministic
    seq = iter([pd.Series(np.arange(120.0), index=panel.index),
                pd.Series(np.arange(120.0) + 1, index=panel.index)])
    monkeypatch.setattr("research.hermes.induct.pit_check_via_sandbox", lambda *a, **k: None)
    with pytest.raises(InductRefused, match="determinism|deterministic"):
        revalidate("code", "foundry_x", "eth", panel,
                   lambda c, p: next(seq), "2024-11-03", tmp_path)


def test_revalidate_refuses_bridge_divergence(tmp_path, monkeypatch):
    panel = _panel()
    monkeypatch.setattr("research.hermes.induct.pit_check_via_sandbox", lambda *a, **k: None)
    # stored cand values differ from the recompute -> diverges
    oos = "2024-11-03"
    pre = panel.index < pd.Timestamp(oos, tz="UTC")
    cand = pd.DataFrame({"foundry_x": pd.Series(np.arange(120.0) + 999, index=panel.index)[pre]})
    from research.hermes.candidate_store import _candidate_path
    p = _candidate_path("eth", tmp_path); p.parent.mkdir(parents=True, exist_ok=True); cand.to_parquet(p)
    with pytest.raises(InductRefused, match="diverge|reconcile"):
        revalidate("code", "foundry_x", "eth", panel,
                   lambda c, p2: pd.Series(np.arange(120.0), index=panel.index), oos, tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_induct.py -k revalidate -q`
Expected: FAIL — `ImportError: cannot import name 'revalidate'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/hermes/induct.py`:

```python
import pandas as pd

from research.hermes.candidate_store import _candidate_path
from research.hermes.foundry_bridge import recompute_full_span, reconciles_pre_oos
from research.hermes.forge import pit_check_via_sandbox
from research.hermes.pit import LookaheadError


def revalidate(code, factor_id, symbol, panel, run_sandbox, oos_start, manifests_dir) -> None:
    """The heavy re-validation gates. Raises InductRefused on any failure.
      - full-span PIT re-check (no future leak in the non-sandboxed prod path)
      - determinism (compute twice -> identical; catches leaked RNG/global state)
      - path-consistency (recompute's pre-oos == the values Foundry stored, i.e.
        what the strategy was backtested on -> live matches the backtest)."""
    first = recompute_full_span(code, panel, run_sandbox)
    try:
        pit_check_via_sandbox(code, panel, first, run_sandbox)
    except LookaheadError as exc:
        raise InductRefused(f"{symbol}:{factor_id} peeks into the future: {exc}") from exc

    second = recompute_full_span(code, panel, run_sandbox)
    if not first.equals(second):
        raise InductRefused(f"{symbol}:{factor_id} is non-deterministic (compute twice differs)")

    cand_path = _candidate_path(symbol, manifests_dir)
    if cand_path.exists():
        stored = pd.read_parquet(cand_path)
        if factor_id in stored.columns and not reconciles_pre_oos(first, stored[factor_id], oos_start):
            raise InductRefused(
                f"{symbol}:{factor_id} recompute diverges from the stored Foundry values "
                "(the strategy's backtest would not match live); refusing to induct")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_induct.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add research/hermes/induct.py research/tests/test_hermes_induct.py
git commit -m "feat(hermes): induct revalidate (PIT + determinism + bridge reconcile)"
```

---

### Task 7: `induct` CLI — golden generation + `--confirm` write + main

**Files:**
- Modify: `research/hermes/induct.py`
- Test: `research/tests/test_hermes_induct.py` (append)

**Interfaces:**
- Consumes: `verify_gates`, `revalidate`, `inducted_dir`.
- Produces: `write_induction(code, factor_id, symbol, fixture, expected, root=None) -> None`; `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_hermes_induct.py`:

```python
from research.hermes.induct import write_induction
from research.lib.inducted_factors import inducted_dir, inducted_names


def test_write_induction_writes_module_meta_and_golden(tmp_path):
    code = "def compute(df):\n    return df['close']\n"
    fixture = _panel(6)
    expected = fixture["close"]
    write_induction(code, "foundry_x", "eth", fixture, expected,
                    root=tmp_path, tests_root=tmp_path / "t")

    d = inducted_dir("eth", root=tmp_path)
    assert (d / "foundry_x.py").read_text(encoding="utf-8") == code
    assert (d / "foundry_x.meta.json").exists()
    assert inducted_names("eth", root=tmp_path) == {"foundry_x"}
    assert (tmp_path / "t" / "foundry_x_fixture.parquet").exists()
    assert (tmp_path / "t" / "test_foundry_x.py").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_induct.py::test_write_induction_writes_module_meta_and_golden -q`
Expected: FAIL — `ImportError: cannot import name 'write_induction'`

- [ ] **Step 3: Write minimal implementation**

Append to `research/hermes/induct.py`:

```python
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from research.lib.inducted_factors import inducted_dir


def write_induction(code, factor_id, symbol, fixture, expected, root=None, tests_root=None) -> None:
    d = inducted_dir(symbol, root=root); d.mkdir(parents=True, exist_ok=True)
    (d / f"{factor_id}.py").write_text(code, encoding="utf-8")
    (d / f"{factor_id}.meta.json").write_text(json.dumps({
        "factor_id": factor_id, "symbol": symbol,
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "inducted_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    tr = Path(tests_root) if tests_root is not None else \
        (Path(__file__).resolve().parents[2] / "research" / "tests" / "inducted")
    tr.mkdir(parents=True, exist_ok=True)
    fixture.to_parquet(tr / f"{factor_id}_fixture.parquet")
    expected.to_frame("expected").to_parquet(tr / f"{factor_id}_expected.parquet")
    (tr / f"test_{factor_id}.py").write_text(
        "import pandas as pd\n"
        "from pathlib import Path\n"
        "import importlib.util\n\n"
        f"_ID = {factor_id!r}\n_SYM = {symbol!r}\n"
        "_HERE = Path(__file__).resolve().parent\n\n"
        "def test_inducted_logic_unchanged():\n"
        "    fx = pd.read_parquet(_HERE / f'{_ID}_fixture.parquet')\n"
        "    exp = pd.read_parquet(_HERE / f'{_ID}_expected.parquet')['expected']\n"
        "    mod_path = Path(__file__).resolve().parents[2] / 'lib' / 'inducted' / _SYM / f'{_ID}.py'\n"
        "    spec = importlib.util.spec_from_file_location(f'ind_{_SYM}_{_ID}', mod_path)\n"
        "    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "    got = m.compute(fx)\n"
        "    pd.testing.assert_series_equal(got, exp, rtol=1e-5, atol=1e-8, check_names=False)\n",
        encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Induct a vetted Foundry factor into the production library.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--factor", required=True)
    ap.add_argument("--manifests-dir", default=None)
    ap.add_argument("--image", default="talos-sandbox:test")
    ap.add_argument("--oos-start", default=None)
    ap.add_argument("--confirm", action="store_true", help="required — without it, refuses")
    args = ap.parse_args(argv)

    from research.hermes.foundry_runner import resolve_image_id, load_ohlcv
    from research.hermes.orchestrator import make_run_sandbox
    from research.hermes.sandbox import DockerSandbox
    from research.lib.factor_io import _default_manifests_dir, load_features, _symbol_short
    from pipeline.config import load_config

    mdir = Path(args.manifests_dir) if args.manifests_dir else _default_manifests_dir()
    cfg = load_config()
    oos = args.oos_start or cfg.oos_start
    try:
        code = verify_gates(args.factor, args.symbol, mdir)
        features = load_features(args.symbol, manifests_dir=mdir)
        ohlcv = load_ohlcv(mdir / f"ohlcv_{_symbol_short(args.symbol)}.parquet")
        panel = features.join(ohlcv, how="left")
        sandbox = DockerSandbox(image=resolve_image_id(args.image), timeout_s=120, allow_unpinned=False)
        run_sandbox = make_run_sandbox(sandbox, mdir / "_foundry_scratch")
        revalidate(code, args.factor, args.symbol, panel, run_sandbox, oos, mdir)

        if not args.confirm:
            print(f"[induct] all gates pass for {args.symbol}:{args.factor}. "
                  "Re-run with --confirm to write it into the production library.")
            return 2
        fixture = panel.head(200)
        expected = run_sandbox(code, fixture)
        write_induction(code, args.factor, args.symbol, fixture, expected)
        print(f"[induct] {args.symbol}:{args.factor} inducted. COMMIT the new files; "
              "then stage0a will compute it and the strategy can deploy.")
        return 0
    except InductRefused as exc:
        print(f"[induct] REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest research/tests/test_hermes_induct.py -q`
Expected: PASS

- [ ] **Step 5: Verify the CLI refuses without --confirm**

Run: `.venv/Scripts/python.exe -m research.hermes.induct --help`
Expected: usage listing `--symbol`, `--factor`, `--confirm`.

- [ ] **Step 6: Commit**

```bash
git add research/hermes/induct.py research/tests/test_hermes_induct.py
git commit -m "feat(hermes): induct golden generation + --confirm write + CLI"
```

---

## Final verification

- [ ] **Full research suite green**:

```bash
.venv/Scripts/python.exe -m pytest research/tests/ -q
```

Expected: all pass (no inducted factors yet → stage0a unchanged; guard/induct covered).

- [ ] **Confirm the guard chain**: a `foundry_` strategy is refused until its factor is inducted, then allowed — covered by `test_hermes_promote.py` + `test_hermes_induct.py`.

- [ ] **Confirm nothing writes production parquet**:

```bash
git status --short research/manifests/
```

Expected: no `features_*.parquet` / `factor_values_*.parquet` changes (induction writes only committed source + a golden fixture under `research/lib/inducted/` and `research/tests/inducted/`).
