# Talos Phase 1B — Hypothesis Queue + Static Pre-filter Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 建 Foundry 的假設佇列——從 4 來源收集因子**假設描述子**，去重，並在**字串層**過濾死因子墓地與已封盤死類（省 token，讓 1C 不浪費 LLM 呼叫在已知失敗/已封盤邏輯）。**數值/語意去重是 1A 的職責**（`nearest_correlate`）；1B 只做便宜、誠實的靜態預過濾。

**Architecture:** 兩個純確定性模組。`Hypothesis` 是輕量描述子（id/描述/來源/字串指紋/死類標籤），**不含計算**。來源 adapter 把 zoo `__alpha_meta__`、高-IC evidence、學術清單、LLM slot 轉成 `Hypothesis`。靜態過濾比對字串指紋 vs 墓地卡片（1E graveyard verdict）+ 憲法死類 ban（zoo 由 `theme` 映射）。

> **⚠️ v2 = agy 二審後重寫。** 關鍵：**移除 AST 正規化**（把所有變數名抹成 `_` 會誤併 `close/…` 與 `volume/…`，且對 zoo LaTeX / LLM 多行函式全失效）→ 退回**正規化字串 hash**。死類 ban 對**所有**來源生效（zoo 由 theme 映射，非只 LLM）。

**Tech Stack:** Python 3.11、`re`、`ast`（僅 zoo meta 靜態抽取）、`hashlib`、既有 `evidence_store`（1E）+ `factor_io.load_evidence`。pytest research scope。

**設計來源：** overview（1B）+ `talos-design.md` §3.6 4 來源 + §5 死因子墓地憲法 + agy 1B 二審。

---

## 設計原則（agy 二審 v2）

1. **1B 產描述子，不算數**：計算/IC 在 1C→1A。
2. **靜態過濾 = 便宜且誠實**：正規化**字串** hash 抓「同表示層的字面重提」。**不做 AST 正規化**（誤併不同輸入欄 + 對 LaTeX/多行失效）。跨表示層（zoo LaTeX vs LLM Python）字串永配不上——**這是 1B 的已知限制**，權威去重＝1A 數值 `nearest_correlate` + 1C 產碼後 `code_sha256`。1B 不假裝能抓語意重複。
3. **死類 ban 對所有來源生效**：`Hypothesis.dead_classes` 由**各 adapter 填**。zoo 由 `__alpha_meta__.theme` 映射（`microstructure`→`intraday_ohlcv_price_derived`）。
4. **撞名保留可執行者**：dedupe 撞 hash 時，保留 description 可 parse 成 Python 的（LLM/衍生），丟不可執行的（zoo LaTeX）——避免把 1C 要用的 Python 丟掉。

---

## File Structure

| 檔案 | 責任 |
|------|------|
| `research/hermes/hypothesis.py` | `Hypothesis` 描述子 + `string_fingerprint()`（正規化字串 hash）+ `is_python_expr()` |
| `research/hermes/hypothesis_queue.py` | 4 來源 adapter + `dedupe` + `filter_static` + `build_queue` + theme→死類映射 |
| `research/tests/test_hermes_hypothesis.py` | 指紋 + dataclass 單測 |
| `research/tests/test_hermes_hypothesis_queue.py` | 去重 + 過濾 + build 單測 |

---

## Task 1: `Hypothesis` 描述子 + 正規化字串指紋

**Files:** Create `research/hermes/hypothesis.py`; Test `research/tests/test_hermes_hypothesis.py`.

`string_fingerprint`：去空白 + 小寫 + collapse whitespace → sha256。**無 AST 正規化**（agy 二審：AST 誤併不同輸入欄且對 LaTeX/多行失效）。`is_python_expr`：能否 `ast.parse(mode="exec")`（供 dedupe 撞名優先保留可執行者）。

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_hypothesis.py
import pytest
from research.hermes.hypothesis import (
    Hypothesis, string_fingerprint, is_python_expr, SOURCE_ZOO,
)


def test_fingerprint_normalises_whitespace_and_case():
    assert string_fingerprint("close.shift(5)") == string_fingerprint("  CLOSE.shift(5)  ")


def test_fingerprint_distinguishes_different_input_columns():
    # agy 二審: AST-normalising vars WRONGLY merged these; string hash keeps them apart
    assert string_fingerprint("close / close.shift(5) - 1") != \
           string_fingerprint("volume / volume.shift(5) - 1")


def test_is_python_expr_detects_executable():
    assert is_python_expr("close / close.shift(5) - 1") is True
    assert is_python_expr(r"\mathrm{close}_t / \mathrm{close}_{t-5} - 1") is False   # LaTeX


def test_hypothesis_autofills_fingerprint_and_rejects_bad_source():
    h = Hypothesis(id="zoo_roc5", description="close/close.shift(5)-1", source=SOURCE_ZOO)
    assert len(h.fingerprint) == 64
    with pytest.raises(ValueError):
        Hypothesis(id="x", description="y", source="bogus")
```

- [ ] **Step 2: Run — fails (ModuleNotFoundError).**

- [ ] **Step 3: Implement**

```python
# research/hermes/hypothesis.py
"""Foundry hypothesis descriptor + normalised string fingerprint (Phase 1B, v2).

A Hypothesis says WHAT to try; it carries no computation (codegen + IC live in
1C/1A). The fingerprint is a whitespace/case-normalised STRING hash — NOT an AST
hash: agy's 1B review showed AST variable-normalisation wrongly merges factors
with different input columns (close/… vs volume/…) and fails entirely on zoo
LaTeX and multi-line LLM code. Cross-representation and semantic dedup are 1A's
numerical job (nearest_correlate) + code_sha256 after 1C codegen."""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field

SOURCE_ZOO = "zoo"
SOURCE_LLM = "llm"
SOURCE_DERIVED = "derived"
SOURCE_ACADEMIC = "academic"
_SOURCES = {SOURCE_ZOO, SOURCE_LLM, SOURCE_DERIVED, SOURCE_ACADEMIC}


def string_fingerprint(text: str) -> str:
    """sha256 of the case-folded, whitespace-collapsed string."""
    key = " ".join(text.split()).lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def is_python_expr(text: str) -> bool:
    """True if text parses as Python (an executable descriptor, not LaTeX/NL)."""
    try:
        ast.parse(text, mode="exec")
        return True
    except SyntaxError:
        return False


@dataclass(frozen=True)
class Hypothesis:
    id: str
    description: str            # Python expr, LaTeX, or NL description
    source: str                # zoo | llm | derived | academic
    dead_classes: tuple = ()    # constitution class tags (filled by adapters, Task 3/4)
    fingerprint: str = field(default="", compare=False)

    def __post_init__(self):
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {_SOURCES}, got {self.source!r}")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", string_fingerprint(self.description))
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): Hypothesis descriptor + normalised string fingerprint (Phase 1B)`

---

## Task 2: 去重佇列（撞名保留可執行者）

**Files:** Create `research/hermes/hypothesis_queue.py`; Test `research/tests/test_hermes_hypothesis_queue.py`.

依 `fingerprint` 去重。撞名時**保留 description 可 parse 成 Python 的**（agy 5b：別把 1C 要用的 Python 丟給 zoo LaTeX）。

- [ ] **Step 1: Failing test**

```python
# research/tests/test_hermes_hypothesis_queue.py
import pytest
from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO, SOURCE_LLM
from research.hermes.hypothesis_queue import dedupe


def test_dedupe_keeps_executable_on_collision():
    latex = Hypothesis("zoo_a", "close.shift(5)", SOURCE_ZOO)
    py = Hypothesis("llm_a", "close.shift(5)", SOURCE_LLM)        # same fingerprint
    # both same fingerprint; keep the executable one regardless of order
    out = dedupe([latex, py])
    assert len(out) == 1 and out[0].id == "llm_a"
    out2 = dedupe([py, latex])
    assert len(out2) == 1 and out2[0].id == "llm_a"


def test_dedupe_distinct_fingerprints_all_kept():
    a = Hypothesis("a", "close.shift(5)", SOURCE_ZOO)
    b = Hypothesis("b", "close.shift(9)", SOURCE_ZOO)
    assert len(dedupe([a, b])) == 2
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
# research/hermes/hypothesis_queue.py
"""Foundry hypothesis queue: collect 4 sources, dedupe, static pre-filter.

Only cheap string-level filtering here so 1C does not burn LLM calls on
literally-repeated or already-buried/dead-class ideas. Numerical/semantic dedup
is 1A (nearest_correlate)."""
from __future__ import annotations

from research.hermes.hypothesis import Hypothesis, is_python_expr


def dedupe(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Drop duplicate fingerprints; on collision keep the executable-Python one
    (agy 5b) so 1C receives runnable code, not LaTeX/NL."""
    best: dict = {}
    order: list = []
    for h in hypotheses:
        fp = h.fingerprint
        if fp not in best:
            best[fp] = h
            order.append(fp)
        else:
            # replace only if incumbent is non-executable and challenger is
            if not is_python_expr(best[fp].description) and is_python_expr(h.description):
                best[fp] = h
    return [best[fp] for fp in order]
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): hypothesis dedupe preferring executable on collision (Phase 1B)`

---

## Task 3: 墓地 + 死類靜態過濾

**Files:** Modify `hypothesis_queue.py` + test. Reference: `research/hermes/evidence_store.load_cards`, `research/hermes/evidence_card.VERDICT_GRAVEYARD`（欄位 `formula`）。

- **墓地過濾**（best-effort，同表示層）：讀 symbol 的 evidence store，收 `verdict=graveyard` 卡片的 `formula` 字串指紋，比對剔除。**已知限制**（agy 3）：跨表示層（zoo LaTeX vs LLM Python）配不上——權威攔截在 1A 數值 + 1C 後 `code_sha256`。
- **死類 ban**：`Hypothesis.dead_classes` 命中憲法 `DEAD_CLASSES` 即剔。

- [ ] **Step 1: Failing test**

```python
def test_filter_removes_graveyard_and_dead_classes(tmp_path, monkeypatch):
    from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO, SOURCE_LLM, string_fingerprint
    from research.hermes import hypothesis_queue as hq
    from research.hermes.hypothesis_queue import filter_static, DEAD_CLASSES

    buried = Hypothesis("z1", "close / close.shift(3) - 1", SOURCE_ZOO)
    fresh = Hypothesis("z2", "volume / volume.shift(3) - 1", SOURCE_ZOO)
    banned = Hypothesis("z3", "some microstructure thing", SOURCE_LLM,
                        dead_classes=("intraday_ohlcv_price_derived",))

    monkeypatch.setattr(hq, "_graveyard_fingerprints",
                        lambda sym, md: {string_fingerprint("close / close.shift(3) - 1")})
    out = filter_static([buried, fresh, banned], symbol="eth", manifests_dir=tmp_path)
    assert [h.id for h in out] == ["z2"]           # buried + dead-class removed


def test_dead_classes_constant_covers_constitution():
    from research.hermes.hypothesis_queue import DEAD_CLASSES
    assert "intraday_ohlcv_price_derived" in DEAD_CLASSES
    assert "binance_orderflow" in DEAD_CLASSES
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
# append to hypothesis_queue.py
from research.hermes.evidence_card import VERDICT_GRAVEYARD
from research.hermes.evidence_store import load_cards
from research.hermes.hypothesis import string_fingerprint

# Constitution dead classes (already-buried families; never re-propose).
# See talos-design.md §5 + memory project_intraday_ohlcv_class_dead / orderflow_poc.
DEAD_CLASSES = frozenset({"intraday_ohlcv_price_derived", "binance_orderflow"})


def _graveyard_fingerprints(symbol: str, manifests_dir) -> set:
    """String fingerprints of buried factors from the symbol's evidence store.
    Best-effort, same-representation only (agy 3): cross-representation dedup is
    1A numerical + post-1C code_sha256."""
    return {
        string_fingerprint(c.formula)
        for c in load_cards(symbol, manifests_dir)
        if c.verdict == VERDICT_GRAVEYARD
    }


def filter_static(hypotheses: list[Hypothesis], symbol: str, manifests_dir) -> list[Hypothesis]:
    dead_fp = _graveyard_fingerprints(symbol, manifests_dir)
    out: list = []
    for h in hypotheses:
        if h.fingerprint in dead_fp:
            continue
        if set(h.dead_classes) & DEAD_CLASSES:
            continue
        out.append(h)
    return out
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): graveyard fingerprint + dead-class static filter (Phase 1B)`

---

## Task 4: zoo 來源 adapter（含 theme→死類映射）

**Files:** Modify `hypothesis_queue.py` + test. Reference: `agent/src/factors/zoo/**/*.py`（`__alpha_meta__`：`id`/`formula_latex`/`theme`）。

- 只讀 meta 不 exec：**regex 粗抽 `__alpha_meta__ = {...}` 區段**（agy 2c：避免對 452 檔全樹 parse）再 `ast.literal_eval`。
- **theme→死類映射**（agy 4）：`microstructure`→`intraday_ohlcv_price_derived`，填 `Hypothesis.dead_classes`，讓死類過濾對 zoo 生效。

- [ ] **Step 1: Failing test**

```python
def test_zoo_adapter_reads_meta_and_maps_dead_class(tmp_path):
    from research.hermes.hypothesis_queue import hypotheses_from_zoo
    zoo = tmp_path / "zoo"; zoo.mkdir()
    (zoo / "micro.py").write_text(
        "__alpha_meta__ = {'id': 'gtja_micro', 'theme': ['microstructure'],\n"
        " 'formula_latex': 'foo'}\n"
        "raise RuntimeError('compute must NOT run')\n", encoding="utf-8")
    (zoo / "mom.py").write_text(
        "__alpha_meta__ = {'id': 'q_roc5', 'theme': ['momentum'], 'formula_latex': 'bar'}\n",
        encoding="utf-8")
    hyps = {h.id: h for h in hypotheses_from_zoo(zoo)}
    assert set(hyps) == {"zoo_gtja_micro", "zoo_q_roc5"}
    assert "intraday_ohlcv_price_derived" in hyps["zoo_gtja_micro"].dead_classes  # theme mapped
    assert hyps["zoo_q_roc5"].dead_classes == ()
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
import ast as _ast
import re as _re
from pathlib import Path

from research.hermes.hypothesis import Hypothesis, SOURCE_ZOO

# zoo theme -> constitution dead class (agy 4)
_THEME_DEAD_CLASS = {"microstructure": "intraday_ohlcv_price_derived"}
_META_RE = _re.compile(r"__alpha_meta__\s*=\s*(\{.*?\})", _re.DOTALL)


def _extract_alpha_meta(source: str) -> dict | None:
    """Regex-scope the __alpha_meta__ dict then literal_eval it (no full-tree
    parse of 452 files, no exec — agy 2c)."""
    m = _META_RE.search(source)
    if not m:
        return None
    try:
        meta = _ast.literal_eval(m.group(1))
        return meta if isinstance(meta, dict) else None
    except (ValueError, SyntaxError):
        return None


def _dead_classes_for_themes(themes) -> tuple:
    return tuple(sorted({_THEME_DEAD_CLASS[t] for t in (themes or []) if t in _THEME_DEAD_CLASS}))


def hypotheses_from_zoo(zoo_dir) -> list[Hypothesis]:
    out: list = []
    for path in sorted(Path(zoo_dir).rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "__alpha_meta__" not in text:          # cheap skip before regex
            continue
        meta = _extract_alpha_meta(text)
        if not meta or "id" not in meta:
            continue
        out.append(Hypothesis(
            id=f"zoo_{meta['id']}",
            description=str(meta.get("formula_latex", meta["id"])),
            source=SOURCE_ZOO,
            dead_classes=_dead_classes_for_themes(meta.get("theme")),
        ))
    return out
```

> 注意：`_META_RE` 非貪婪 `\{.*?\}` 假設 meta dict 無巢狀 `}` 提前截斷。實作前抽查 zoo 幾個真檔（`gtja191/`、`academic/`）確認 meta 為單層 dict；若有巢狀，改回「找 `__alpha_meta__` 行起、括號配對」或 per-file `ast.parse` 只在含該字串的檔。勿臆測。

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: Commit** `feat(hermes): zoo meta adapter, regex-scoped + theme->dead-class (Phase 1B)`

---

## Task 5: 高-IC 衍生 + 學術 + LLM slot + `build_queue` + 匯出

**Files:** Modify `hypothesis_queue.py`, `research/hermes/__init__.py`; test. Reference: `research/lib/factor_io.load_evidence`。

- **高-IC 衍生**：讀 `evidence_<sym>.json`；每 row 欄位是 **`feature_key`**（agy 5a：非 `factor`/`name`），取前 K 高 IC 各衍生 N 變體。
- **學術/LLM slot/build_queue**：如前；`build_queue` 串 4 來源 → `dedupe` → `filter_static`。

- [ ] **Step 1: Failing test**

```python
def test_evidence_derivation_uses_feature_key(tmp_path, monkeypatch):
    from research.hermes import hypothesis_queue as hq
    monkeypatch.setattr(hq, "load_evidence", lambda symbol, manifests_dir=None: {
        "evidence": [{"feature_key": "funding_z", "ir": 0.5},
                     {"feature_key": "depeg", "ir": 0.4}]})
    out = hq.hypotheses_from_evidence_derivation("eth", tmp_path, top_k=1)
    assert out and all(h.source == "derived" for h in out)
    assert any("funding_z" in h.description for h in out)      # feature_key read, not None


def test_build_queue_assembles_dedupes_filters(tmp_path, monkeypatch):
    from research.hermes import hypothesis_queue as hq
    from research.hermes.hypothesis import Hypothesis, SOURCE_ACADEMIC
    monkeypatch.setattr(hq, "hypotheses_from_zoo", lambda d: [
        Hypothesis("zoo_a", "close / close.shift(5) - 1", "zoo")])
    monkeypatch.setattr(hq, "hypotheses_from_evidence_derivation", lambda s, m: [
        Hypothesis("der_a", "close / close.shift(5) - 1", "derived")])   # dupe of zoo_a
    monkeypatch.setattr(hq, "hypotheses_from_academic", lambda: [
        Hypothesis("acad_a", "close.rolling(20).mean()", SOURCE_ACADEMIC)])
    monkeypatch.setattr(hq, "_graveyard_fingerprints", lambda s, m: set())
    q = {h.id for h in hq.build_queue("eth", tmp_path, zoo_dir=tmp_path, llm_raw=[])}
    assert "acad_a" in q and len(q & {"zoo_a", "der_a"}) == 1    # one of the dupes kept
```

- [ ] **Step 2: Run — fails.**

- [ ] **Step 3: Implement**

```python
from research.lib.factor_io import load_evidence
from research.hermes.hypothesis import SOURCE_DERIVED, SOURCE_ACADEMIC, SOURCE_LLM

_ACADEMIC_SEED = [
    ("acad_ts_mom", "close / close.shift(20) - 1"),
    ("acad_lo_vol", "-1 * close.pct_change().rolling(20).std()"),
]
_DERIVE_TRANSFORMS = ("zscore", "rank")


def hypotheses_from_evidence_derivation(symbol: str, manifests_dir, top_k: int = 5) -> list[Hypothesis]:
    """Derive variant descriptors from the top-IR features in evidence_<sym>.json.
    Rows key on 'feature_key' (agy 5a)."""
    try:
        ev = load_evidence(symbol, manifests_dir=manifests_dir)
    except FileNotFoundError:
        return []
    rows = ev.get("evidence", []) if isinstance(ev, dict) else ev
    out: list = []
    for row in list(rows)[:top_k]:
        base = row.get("feature_key")
        if not base:
            continue
        for t in _DERIVE_TRANSFORMS:
            out.append(Hypothesis(id=f"der_{base}_{t}",
                                  description=f"{t}({base})", source=SOURCE_DERIVED))
    return out


def hypotheses_from_academic() -> list[Hypothesis]:
    return [Hypothesis(id=i, description=d, source=SOURCE_ACADEMIC) for i, d in _ACADEMIC_SEED]


def hypotheses_from_llm(raw: list) -> list[Hypothesis]:
    """Wrap LLM-proposed ideas (actual generation happens in 1C)."""
    return [Hypothesis(id=r["id"], description=r["description"], source=SOURCE_LLM,
                       dead_classes=tuple(r.get("dead_classes", ()))) for r in raw]


def build_queue(symbol: str, manifests_dir, zoo_dir, llm_raw: list | None = None) -> list[Hypothesis]:
    collected = (
        hypotheses_from_zoo(zoo_dir)
        + hypotheses_from_evidence_derivation(symbol, manifests_dir)
        + hypotheses_from_academic()
        + hypotheses_from_llm(llm_raw or [])
    )
    return filter_static(dedupe(collected), symbol, manifests_dir)
```

在 `research/hermes/__init__.py` 追加匯出 `Hypothesis`, `build_queue`（先 `Read` 現況再改）。

- [ ] **Step 4: Run hypothesis suite + 全 hermes 套件無回歸。**

- [ ] **Step 5: Commit** `feat(hermes): evidence(feature_key)/academic/llm sources + build_queue, export (Phase 1B)`

---

## Self-Review

**Spec coverage：** 4 來源 → Task 4+5；正規化字串去重 → Task 1+2；墓地過濾 → Task 3；死類 ban（**全來源**，zoo theme 映射）→ Task 3+4；build_queue → Task 5。

**agy 二審修正落點：** 拔 AST → Task 1（字串 hash）；撞名保留可執行 → Task 2；死類全來源（zoo theme 映射）→ Task 4；evidence `feature_key` → Task 5；墓地跨表示層限制誠實記錄 → Task 3；452 檔 regex 粗抽 → Task 4。

**已知界線（誠實）：**
- **1B 只出描述子**，不算 IC。**跨表示層字串去重配不上**（zoo LaTeX vs LLM Python）——權威去重＝1A 數值 + 1C 後 `code_sha256`。1B 不假裝。
- **死類映射非窮舉**：目前只 `microstructure→intraday_ohlcv_price_derived`；orderflow 類無對應 zoo theme，靠 LLM slot 自標。之後可擴 mapping。
- **學術 seed minimal**。

**Placeholder scan：** Task 3/4/5 明示「實作前 Read `evidence_store`/`load_evidence` + 抽查 zoo 真檔確認 meta 單層」。`_META_RE` 非貪婪的巢狀風險已明示 fallback。

**Type consistency：** `Hypothesis`（Task 1）欄位跨 task 一致；`string_fingerprint`/`is_python_expr` 定義於 hypothesis.py、queue 引用一致；`SOURCE_*` 跨檔一致；`build_queue` 串的 adapter 名 == Task 4/5 定義（測試 monkeypatch 依賴名一致）。

---

## Execution Handoff

計畫 v2 存 `docs/talos/plans/2026-07-08-talos-phase1b-hypothesis-queue.md`。已納 agy 二審全部修正。選執行：Subagent-Driven（推薦）或 Inline，每 Task 停等核准。
