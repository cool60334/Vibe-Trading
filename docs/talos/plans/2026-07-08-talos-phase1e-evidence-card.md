# Talos Phase 1E — Evidence Card + Promote Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 定義 Foundry 合格因子的產出契約（`EvidenceCard` schema）+ 唯一碰 production features 的**人工 promote 閘**——schema-first，決定 1A 吐什麼、`candidate_store` 存什麼。

**Architecture:** 三個純 Python 模組於 `research/hermes/`。`EvidenceCard` 是 frozen dataclass + 驗證 + nan-safe JSON round-trip。證據卡以 per-symbol JSON list 累積（複用 `factor_io.dump_evidence` 慣例 + atomic）。`promote.py` 比照 `final_holdout.py` 模式：手動 CLI、confirm-gated、大聲安全警告、**絕不被自動鏈 import**——是全 repo 除 stage 外唯一寫 production `features_<sym>.parquet` 的 Foundry 路徑。

**Tech Stack:** Python 3.11 dataclasses、json、pandas、既有 `factor_io`（`_atomic_write_text`/`append_feature_column`/`_symbol_short`）。pytest research scope（repo 根 `python -m pytest research/tests/`）。

**設計來源：** `docs/talos/plans/2026-07-08-talos-phase1-overview.md`（1E）+ agy 概覽二審（1E schema 三要素：公式/邏輯解說、統計雷達、相關性警示；human-in-loop 理由）。

---

## File Structure

| 檔案 | 責任 |
|------|------|
| `research/hermes/evidence_card.py` | `EvidenceCard` dataclass + 驗證 + nan-safe `to_dict`/`from_dict` |
| `research/hermes/evidence_store.py` | per-symbol 證據卡 JSON list 累積讀寫（atomic，foundry_evidence dir） |
| `research/hermes/promote.py` | 手動 promote 閘 CLI（confirm-gated，候選→production，大聲安全） |
| `research/tests/test_hermes_evidence_card.py` | schema + round-trip + 驗證 假資料單測 |
| `research/tests/test_hermes_evidence_store.py` | 累積讀寫 + atomic 單測 |
| `research/tests/test_hermes_promote.py` | 閘 confirm gate + merge + 拒自動 import 單測 |

**Schema 決策（schema-first，1A 尚未建但此契約先定）：** 證據卡欄位涵蓋所有 1A 守門將吐的指標，即使 1A 之後才填。

---

## Task 1: EvidenceCard schema + 驗證 + nan-safe round-trip

**Files:**
- Create: `research/hermes/evidence_card.py`
- Test: `research/tests/test_hermes_evidence_card.py`

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_evidence_card.py
import json
import math
import pytest
from research.hermes.evidence_card import EvidenceCard, CardValidationError, VERDICT_CANDIDATE, VERDICT_GRAVEYARD


def _candidate_kwargs(**over):
    base = dict(
        factor_id="eth_mom5_ll01", symbol="eth", source="llm",
        code_sha256="a" * 64, generated_at="2026-07-08T00:00:00+00:00", trial_step=1,
        interval="1H", formula="close.pct_change(5)", rationale="5-bar momentum",
        net_ic=0.031, ic_nonoverlap=0.028, ir=0.42, dsr=0.11, pbo=0.34,
        turnover=0.12, n_samples=8760,
        regime_ic={"bull": 0.05, "bear": 0.01, "chop": 0.02},
        yearly_ic={"2022": 0.04, "2023": 0.02},
        nearest_factor="funding_z", nearest_abs_spearman=0.41,
        verdict=VERDICT_CANDIDATE, death_reason=None,
    )
    base.update(over)
    return base


def test_candidate_card_round_trips_through_json():
    card = EvidenceCard(**_candidate_kwargs())
    blob = json.dumps(card.to_dict())          # must not raise (nan-safe)
    back = EvidenceCard.from_dict(json.loads(blob))
    assert back == card


def test_nan_metric_serialises_as_null_not_invalid_json():
    card = EvidenceCard(**_candidate_kwargs(dsr=float("nan")))
    blob = json.dumps(card.to_dict(), allow_nan=False)   # strict: bare NaN would raise
    assert '"dsr": null' in blob
    assert EvidenceCard.from_dict(json.loads(blob)).dsr is None


def test_nested_nan_in_regime_ic_is_sanitised():
    # agy 二審: top-level-only sanitising left nested NaN -> allow_nan=False crashed
    card = EvidenceCard(**_candidate_kwargs(regime_ic={"bull": float("nan"), "bear": 0.01}))
    blob = json.dumps(card.to_dict(), allow_nan=False)   # must NOT raise
    assert EvidenceCard.from_dict(json.loads(blob)).regime_ic == {"bull": None, "bear": 0.01}


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_inf_metric_serialises_as_null(bad):
    card = EvidenceCard(**_candidate_kwargs(ir=bad))
    blob = json.dumps(card.to_dict(), allow_nan=False)   # must NOT raise
    assert EvidenceCard.from_dict(json.loads(blob)).ir is None


def test_from_dict_tolerates_missing_new_field():
    # forward-compat: an old card lacking a later-added field must still load
    d = _candidate_kwargs()
    del d["turnover"]                                     # simulate pre-turnover card
    card = EvidenceCard.from_dict(d)
    assert card.turnover is None


def test_graveyard_card_requires_death_reason():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(verdict=VERDICT_GRAVEYARD, death_reason=None))


def test_candidate_card_requires_core_metrics():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(net_ic=None))


def test_unknown_verdict_rejected():
    with pytest.raises(CardValidationError):
        EvidenceCard(**_candidate_kwargs(verdict="maybe"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_evidence_card.py -v`
Expected: FAIL — `ModuleNotFoundError: research.hermes.evidence_card`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/evidence_card.py
"""EvidenceCard: the contract for one Foundry factor's verdict + evidence.

Schema-first (agy 1E): every metric the statistical gatekeeper (1A) will emit
is named here before 1A exists. A card is either a promotable CANDIDATE or a
GRAVEYARD entry with a death_reason. JSON is nan-safe: non-finite floats
serialise to null so json.dumps(..., allow_nan=False) never emits invalid JSON.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field, fields
from typing import Optional

from research.hermes.errors import HermesGuardError

VERDICT_CANDIDATE = "candidate"
VERDICT_GRAVEYARD = "graveyard"
_VERDICTS = {VERDICT_CANDIDATE, VERDICT_GRAVEYARD}
# metrics a promotable candidate must carry (None => rejected at construction)
_CORE_METRICS = ("net_ic", "ic_nonoverlap", "ir", "dsr", "pbo")


class CardValidationError(HermesGuardError, ValueError):
    """Raised when an EvidenceCard violates its schema invariants."""


def _sanitize(v):
    """Recursively map non-finite floats (nan/inf/-inf) to None so
    json.dumps(..., allow_nan=False) can never emit invalid JSON — including
    NaN nested inside regime_ic/yearly_ic dicts (agy 二審 finding #3)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize(x) for x in v]
    return v


@dataclass(frozen=True)
class EvidenceCard:
    # provenance + context (required — always known at construction)
    factor_id: str
    symbol: str
    source: str                 # zoo | llm | derived | academic
    code_sha256: str
    generated_at: str           # ISO-8601 UTC
    trial_step: int
    interval: str               # "1H" / "1D" — candle the factor was computed on
    formula: str
    rationale: str
    verdict: str
    # metrics + context (default None => forward-compatible: an older card
    # missing a later-added field still loads via from_dict, agy #7)
    net_ic: Optional[float] = None
    ic_nonoverlap: Optional[float] = None
    ir: Optional[float] = None
    dsr: Optional[float] = None
    pbo: Optional[float] = None
    turnover: Optional[float] = None        # raw turnover behind net_ic (agy #6)
    n_samples: Optional[int] = None         # sample count behind the IC (agy #6)
    regime_ic: dict = field(default_factory=dict)   # {"bull":..,"bear":..,"chop":..}
    yearly_ic: dict = field(default_factory=dict)   # {"2022":.., ...}
    nearest_factor: Optional[str] = None
    nearest_abs_spearman: Optional[float] = None
    death_reason: Optional[str] = None

    def __post_init__(self):
        if self.verdict not in _VERDICTS:
            raise CardValidationError(f"verdict must be one of {_VERDICTS}, got {self.verdict!r}")
        if self.verdict == VERDICT_GRAVEYARD and not self.death_reason:
            raise CardValidationError("graveyard card requires a death_reason")
        if self.verdict == VERDICT_CANDIDATE:
            missing = [m for m in _CORE_METRICS if getattr(self, m) is None]
            if missing:
                raise CardValidationError(f"candidate card missing core metrics: {missing}")

    def to_dict(self) -> dict:
        return {k: _sanitize(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceCard":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_evidence_card.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/evidence_card.py research/tests/test_hermes_evidence_card.py
git commit -m "feat(hermes): EvidenceCard schema, validation + nan-safe JSON (Phase 1E)"
```

---

## Task 2: per-symbol 證據卡累積讀寫

**Files:**
- Create: `research/hermes/evidence_store.py`
- Test: `research/tests/test_hermes_evidence_store.py`
- Reference: `research/lib/factor_io.py` (`_atomic_write_text`, `_symbol_short`)

概念：每 symbol 一個 `foundry_evidence/foundry_evidence_<sym>.json`，內容是 card list。寫入=load 既有→依 `factor_id` upsert→atomic 寫回（避免 nightly 併發覆寫 + parquet 無 append 教訓的 JSON 版）。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_evidence_store.py
import json
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE
from research.hermes.evidence_store import upsert_card, load_cards, EVIDENCE_SUBDIR


def _card(factor_id, net_ic=0.03):
    return EvidenceCard(
        factor_id=factor_id, symbol="eth", source="llm", code_sha256="b" * 64,
        generated_at="2026-07-08T00:00:00+00:00", trial_step=1, interval="1H",
        formula="f", rationale="r", net_ic=net_ic, ic_nonoverlap=0.02, ir=0.4,
        dsr=0.1, pbo=0.3, regime_ic={}, yearly_ic={}, nearest_factor=None,
        nearest_abs_spearman=None, verdict=VERDICT_CANDIDATE, death_reason=None,
    )


def test_upsert_appends_then_updates_by_factor_id(tmp_path):
    upsert_card(_card("a"), "eth", manifests_dir=tmp_path)
    upsert_card(_card("b"), "eth", manifests_dir=tmp_path)
    upsert_card(_card("a", net_ic=0.099), "eth", manifests_dir=tmp_path)  # update a
    cards = load_cards("eth", manifests_dir=tmp_path)
    assert {c.factor_id for c in cards} == {"a", "b"}                     # no dup
    assert next(c for c in cards if c.factor_id == "a").net_ic == 0.099   # updated


def test_written_json_is_valid_and_under_evidence_subdir(tmp_path):
    path = upsert_card(_card("a"), "eth", manifests_dir=tmp_path)
    assert EVIDENCE_SUBDIR in path.parts
    json.loads(path.read_text(encoding="utf-8"))          # valid JSON (nan-safe)
    assert not list(path.parent.glob("*.tmp"))            # atomic, no leftover


def test_load_missing_returns_empty(tmp_path):
    assert load_cards("eth", manifests_dir=tmp_path) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_evidence_store.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/evidence_store.py
"""Per-symbol Foundry evidence store: foundry_evidence/foundry_evidence_<sym>.json.

Holds a list of EvidenceCard dicts. upsert = load -> replace-by-factor_id ->
atomic write-back (JSON analogue of the parquet no-append lesson; nightly
iterations must not clobber). Isolated from production evidence_<sym>.json.
"""
from __future__ import annotations

import json
from pathlib import Path

from research.hermes.evidence_card import EvidenceCard
from research.lib.factor_io import _atomic_write_text, _symbol_short

EVIDENCE_SUBDIR = "foundry_evidence"


def _store_path(symbol: str, manifests_dir: Path) -> Path:
    return Path(manifests_dir) / EVIDENCE_SUBDIR / f"foundry_evidence_{_symbol_short(symbol)}.json"


def load_cards(symbol: str, manifests_dir) -> list[EvidenceCard]:
    path = _store_path(symbol, Path(manifests_dir))
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [EvidenceCard.from_dict(d) for d in raw]


def upsert_card(card: EvidenceCard, symbol: str, manifests_dir) -> Path:
    path = _store_path(symbol, Path(manifests_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    cards = {c.factor_id: c for c in load_cards(symbol, manifests_dir)}
    cards[card.factor_id] = card
    payload = [c.to_dict() for c in cards.values()]
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True,
                                        ensure_ascii=False, allow_nan=False))
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_evidence_store.py -v`
Expected: PASS (3 tests)

> 若 `_atomic_write_text`/`_symbol_short` 簽名與此不符，先 `Read research/lib/factor_io.py` 對齊真實簽名再實作——勿臆測。

- [ ] **Step 5: Commit**

```bash
git add research/hermes/evidence_store.py research/tests/test_hermes_evidence_store.py
git commit -m "feat(hermes): per-symbol evidence card store, atomic upsert (Phase 1E)"
```

---

## Task 3: 人工 promote 閘（唯一碰 production 的 Foundry 路徑）

**Files:**
- Create: `research/hermes/promote.py`
- Test: `research/tests/test_hermes_promote.py`
- Reference: `research/pipeline/final_holdout.py`（手動-only 安全模式範本）、`research/lib/factor_io.py:483`（`append_feature_column`）

概念：比照 `final_holdout.py`——這是全 repo **除 pipeline stage 外唯一**寫 production `features_<sym>.parquet` 的 Foundry 路徑，只由**人工**執行、`confirm=True` 才動、大聲印警告。**真防線是 `confirm=False` 預設 + 不進 `__init__` 匯出**（agy #5：import-time raise 會擋 package 合法 import，且 confirm gate 才是真攔截；比照 `final_holdout` 亦不被匯出）。把某候選特徵欄從 candidate parquet merge 進 production，要求該因子已有 CANDIDATE 證據卡、**撞名既有 production 欄則拒**（agy #8），成功後**記 ledger 稽核軌跡**（agy #9）。

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_hermes_promote.py
import pandas as pd
import pytest
from research.hermes.evidence_card import EvidenceCard, VERDICT_CANDIDATE, VERDICT_GRAVEYARD
from research.hermes.evidence_store import upsert_card
from research.hermes.candidate_store import write_candidate
from research.hermes.promote import promote_candidate, PromoteRefused


def _seed(tmp_path, verdict=VERDICT_CANDIDATE):
    idx = pd.date_range("2024-01-01", periods=5, freq="1h")
    write_candidate(pd.DataFrame({"mom5": [0.1, 0.2, 0.3, 0.4, 0.5]}, index=idx),
                    "eth", manifests_dir=tmp_path)
    card = EvidenceCard(
        factor_id="mom5", symbol="eth", source="llm", code_sha256="c" * 64,
        generated_at="2026-07-08T00:00:00+00:00", trial_step=1, interval="1H",
        formula="f", rationale="r", net_ic=0.03, ic_nonoverlap=0.02, ir=0.4,
        dsr=0.1, pbo=0.3, regime_ic={}, yearly_ic={}, nearest_factor=None,
        nearest_abs_spearman=None,
        verdict=verdict, death_reason=("dead" if verdict == VERDICT_GRAVEYARD else None),
    )
    upsert_card(card, "eth", manifests_dir=tmp_path)


def test_refuses_without_confirm(tmp_path):
    _seed(tmp_path)
    with pytest.raises(PromoteRefused):
        promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=False)


def test_refuses_graveyard_factor(tmp_path):
    _seed(tmp_path, verdict=VERDICT_GRAVEYARD)
    with pytest.raises(PromoteRefused):
        promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=True)


def test_refuses_existing_production_column_without_overwrite(tmp_path, monkeypatch):
    # agy #8: silent overwrite of a live feature is a real-money hazard
    _seed(tmp_path)
    import research.hermes.promote as promo
    monkeypatch.setattr(promo, "_production_feature_names", lambda s, m: {"mom5"})
    with pytest.raises(PromoteRefused):
        promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=True)


def test_confirmed_promote_writes_column_and_records_ledger(tmp_path, monkeypatch):
    _seed(tmp_path)
    calls, events = {}, []
    import research.hermes.promote as promo
    monkeypatch.setattr(promo, "_production_feature_names", lambda s, m: set())  # no clash
    def fake_append(symbol, key, series, manifests_dir=None, **kw):
        calls["symbol"], calls["key"], calls["n"] = symbol, key, len(series)
    def fake_event(manifests_dir, *, kind, symbol, strategy_id=None, detail=None):
        events.append((kind, symbol, detail))
    monkeypatch.setattr(promo, "append_feature_column", fake_append)
    monkeypatch.setattr(promo, "append_event", fake_event)
    promote_candidate("mom5", "eth", manifests_dir=tmp_path, confirm=True)
    assert calls == {"symbol": "eth", "key": "mom5", "n": 5}
    assert events and events[0][0] == "promote"          # ledger audit trail (agy #9)


def test_promote_not_exported_from_package():
    # agy #5: a production-mutating gate must not be in the package namespace
    import research.hermes as pkg
    assert not hasattr(pkg, "promote_candidate")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_hermes_promote.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# research/hermes/promote.py
"""Manual promote gate — the ONLY Foundry path that writes production features.

CRITICAL SAFETY CONSTRAINT
--------------------------
This module must NEVER be imported or called by any automatic chain — no stage,
no foundry runner, no cron, no dashboard button. Promotion moves a vetted
candidate feature into production features_<sym>.parquet (read by the live
trader). The real enforcement is two-fold: confirm defaults to False (nothing
happens on accidental call), and this module is deliberately NOT exported from
research/hermes/__init__ (so a stray `import research.hermes` cannot surface it).
It runs only when a human invokes it with confirm=True, exactly like
research/pipeline/final_holdout.py is the only path allowed to touch the final
holdout window.

    python -m research.hermes.promote --symbol eth --factor mom5 --confirm
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from research.hermes.candidate_store import _candidate_path
from research.hermes.errors import HermesGuardError
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.lib.factor_io import (
    append_feature_column, load_features_meta, _default_manifests_dir, _symbol_short,
)
from research.lib.research_ledger import append_event


class PromoteRefused(HermesGuardError, RuntimeError):
    """Raised when a promotion is not allowed (no confirm / not a candidate / clash / missing)."""


def _production_feature_names(symbol: str, manifests_dir: Path) -> set:
    """Existing production feature column names (empty if none yet)."""
    try:
        meta = load_features_meta(symbol, manifests_dir=manifests_dir)
    except FileNotFoundError:
        return set()
    return set(meta.get("feature_names", []))


def promote_candidate(factor_id: str, symbol: str, manifests_dir=None,
                      confirm: bool = False, overwrite: bool = False) -> None:
    if not confirm:
        raise PromoteRefused(
            f"promotion of {symbol}:{factor_id} refused: pass confirm=True "
            "(this writes PRODUCTION features read by the live trader)"
        )
    # agy #1: resolve default BEFORE any Path(manifests_dir) — Path(None) crashes.
    mdir = Path(manifests_dir) if manifests_dir is not None else _default_manifests_dir()

    cards = {c.factor_id: c for c in load_cards(symbol, mdir)}
    card = cards.get(factor_id)
    if card is None:
        raise PromoteRefused(f"no evidence card for {symbol}:{factor_id}")
    if card.verdict != VERDICT_CANDIDATE:
        raise PromoteRefused(f"{symbol}:{factor_id} verdict={card.verdict!r}, not promotable")

    # agy #8: refuse to silently overwrite a live production feature.
    if factor_id in _production_feature_names(symbol, mdir) and not overwrite:
        raise PromoteRefused(
            f"{symbol}:{factor_id} already a production feature; pass overwrite=True to replace"
        )

    cand_path = _candidate_path(symbol, mdir)
    if not cand_path.exists():
        raise PromoteRefused(f"candidate parquet missing: {cand_path}")
    df = pd.read_parquet(cand_path)
    if factor_id not in df.columns:
        raise PromoteRefused(f"column {factor_id!r} not in candidate parquet {cand_path}")

    print(f"[promote] WRITING PRODUCTION feature {symbol}:{factor_id} "
          f"({len(df)} rows) — live trader will read this.")
    append_feature_column(symbol, factor_id, df[factor_id], manifests_dir=mdir)
    # agy #9: durable audit trail for a privileged production write.
    append_event(mdir, kind="promote", symbol=_symbol_short(symbol),
                 detail={"factor_id": factor_id, "code_sha256": card.code_sha256,
                         "overwrite": overwrite})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Manually promote a Foundry candidate to production.")
    p.add_argument("--symbol", required=True)
    p.add_argument("--factor", required=True)
    p.add_argument("--confirm", action="store_true",
                   help="required — without it the promotion is refused")
    p.add_argument("--overwrite", action="store_true",
                   help="allow replacing an existing production feature of the same name")
    args = p.parse_args(argv)
    symbol = _symbol_short(args.symbol)          # agy #10: normalise case up front
    try:
        promote_candidate(args.factor, symbol, manifests_dir=None,
                           confirm=args.confirm, overwrite=args.overwrite)
    # agy #2: append_feature_column raises ValueError (low coverage/all-NaN) +
    # FileNotFoundError (no production parquet yet) — catch alongside guard errors.
    except (HermesGuardError, ValueError, FileNotFoundError) as exc:
        print(f"[promote] REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> 實作前 `Read research/lib/factor_io.py`（`load_features_meta`/`_default_manifests_dir`/`append_feature_column`）與 `research/lib/research_ledger.py`（`append_event` 簽名：`append_event(manifests_dir, *, kind, symbol, strategy_id=None, detail=None)`）對齊真實簽名——勿臆測。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest research/tests/test_hermes_promote.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add research/hermes/promote.py research/tests/test_hermes_promote.py
git commit -m "feat(hermes): manual promote gate, confirm-only candidate->production (Phase 1E)"
```

---

## Task 4: 匯出 + Phase 1E 全套驗證

**Files:**
- Modify: `research/hermes/__init__.py`（公開匯出 1E）
- Test: 跑全 hermes 套件確認無回歸

- [ ] **Step 1: Add exports**

在 `research/hermes/__init__.py` 追加（先 `Read` 現況再改，維持既有匯出）：

```python
from research.hermes.evidence_card import (
    EvidenceCard, CardValidationError, VERDICT_CANDIDATE, VERDICT_GRAVEYARD,
)
from research.hermes.evidence_store import upsert_card, load_cards
```
並把上述名稱加入 `__all__`。

> **不匯出 `promote`（agy #5）**：`promote_candidate` 是唯一寫 production 的路徑，刻意**不**進 package namespace——只能 `python -m research.hermes.promote` 或顯式 full-path import。比照 `final_holdout.py` 亦不被匯出。`test_promote_not_exported_from_package` 守此契約。

- [ ] **Step 2: Run full hermes suite**

Run: `python -m pytest research/tests/test_hermes_evidence_card.py research/tests/test_hermes_evidence_store.py research/tests/test_hermes_promote.py research/tests/test_hermes_pit.py research/tests/test_hermes_split.py research/tests/test_hermes_candidate_store.py research/tests/test_hermes_sandbox_ast.py research/tests/test_hermes_sandbox.py research/tests/test_hermes_phase0_integration.py -v`
Expected: ALL PASS（sandbox docker 容器測試 SKIP 若無 `TALOS_SANDBOX_TEST_IMAGE`/daemon）

- [ ] **Step 3: Commit**

```bash
git add research/hermes/__init__.py
git commit -m "feat(hermes): export Phase 1E evidence card + promote gate (Phase 1E)"
```

---

## Self-Review

**Spec coverage（對 overview 1E + agy 三要素）：**
- 證據卡 schema（公式/邏輯解說 → `formula`/`rationale`；統計雷達 → `net_ic`/`ic_nonoverlap`/`ir`/`dsr`/`pbo`/`regime_ic`/`yearly_ic`；相關性警示 → `nearest_factor`/`nearest_abs_spearman`）→ Task 1 ✅
- 生死判定 + 死因 → `verdict`/`death_reason` 驗證 ✅
- 累積讀寫 atomic（隔離 production evidence）→ Task 2 ✅
- 人工 promote 閘（confirm-only、拒墓地、撞名拒、ledger 稽核、不匯出、唯一碰 production）→ Task 3 ✅

**Placeholder scan：** Task 2/3 明示「實作前 Read 既有簽名對齊」（`_atomic_write_text`/`_symbol_short`/`append_feature_column`/`load_features_meta`/`_default_manifests_dir`/`append_event`）以防臆測——非隱藏 placeholder。

**Type consistency：** `EvidenceCard` 22 欄（10 必填 + 12 defaulted）在 Task 1 定義，Task 2/3 測試建構含 `interval` 一致；`VERDICT_CANDIDATE`/`VERDICT_GRAVEYARD` 常數跨檔一致；例外類 `CardValidationError`/`PromoteRefused` 皆繼承既有 `HermesGuardError`（Phase 0 已建），與 `ProductionWriteError`/`SandboxError` 同基底，Phase-1 統一錯誤處理。測試數：Task1 8、Task2 3、Task3 5。

**已知界線（留後續子計畫）：** 本計畫**只定契約 + 閘**，不算任何指標——`net_ic`/`dsr`/... 由 **1A 統計守門**填（含 agy P1 turnover→weights、P2 ledger 全域 trial、P3 矩陣化 Spearman）。1E schema 已預留欄位，1A 只需填值。

---

## 附錄：agy 二審加固（10 findings，9 採納 + 1 部分）

agy --dir 全 repo 二審此計畫並核對既有簽名（`_atomic_write_text`/`_symbol_short`/`_candidate_path`/`HermesGuardError`/`final_holdout` 模式 + atomic 無 read-while-write **皆查核無誤**）。折入：

- **#1 `Path(None)` TypeError** — `promote_candidate` 頂部先 `_default_manifests_dir()` 解析，再 `Path(...)`。
- **#2 例外捕捉漏 append 副作用** — `main` catch 擴 `(HermesGuardError, ValueError, FileNotFoundError)`（低 coverage/全 NaN/production parquet 不存在）。
- **#3 nan-safe 致命 bug（巢狀）** — `to_dict` 改**遞迴** `_sanitize`（清 regime_ic/yearly_ic 內層 nan）；加巢狀 nan 測試。
- **#4 inf/-inf 漏測** — 加 `float("inf")/-inf` parametrize。
- **#5 no-auto-import 契約弱** — **部分採納**：駁 agy 的 import-time raise（會擋 `__init__` 合法 import）；改真防線＝`confirm=False` 預設 + promote **不進 `__init__` 匯出**，加 `test_promote_not_exported_from_package`。
- **#6 schema 漏欄** — 加 `interval` / `turnover` / `n_samples`。
- **#7 from_dict 向前相容脆** — Optional 欄全 `default=None` + `regime_ic/yearly_ic` `default_factory=dict`；加缺欄載入測試。
- **#8 靜默覆寫 production 同名欄** — promote 前查 `_production_feature_names`，撞名 refuse 除非 `overwrite=True`。
- **#9 promote 無稽核軌跡** — 成功後 `append_event(kind="promote", detail={factor_id, code_sha256, overwrite})`（合 §3.4 全漏斗記帳憲法）。
- **#10 symbol 大小寫** — `main` 先 `_symbol_short` 標準化。

Phase 0 護欄無衝突 **agy 查核無誤**（不 inline 跑、唯一路徑寫入、手工確認）。

---

## Execution Handoff

概覽 + 1E 計畫存 `docs/talos/plans/`。1E 兩執行選項：

1. **Subagent-Driven（推薦）** — 每 Task 派新 subagent，Task 間審查。
2. **Inline Execution** — 本 session 逐 Task 跑，批次+檢查點。

依鐵律每 Task 停等核准。挑哪個？
