# L/S Positioning Factors — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the long/short positioning factor family (`global_ls_acct_z`, `toptrader_ls_z`, `ls_divergence`) into stage-0a so the pipeline discovers it per symbol from the cached Binance OI/L-S parquet.

**Architecture:** Mirror the existing `orderflow_df` graceful-absent load + the `oi_factors`/`funding_factors` transform pattern. A new `positioning_factors()` in `derived_factors.py` z-scores the raw L/S columns and reindexes (no ffill) to the candle grid; stage-0a loads the parquet (absent ⇒ factors simply not present ⇒ 1H runs unchanged) and feeds it to `build_feature_dict`. Per-symbol selection + sign are left to stage-0/1 (univariate IC).

**Tech Stack:** Python, pandas, pytest. Research suite only (`research/tests`), run separately from dashboard/agent.

**Spec:** `docs/superpowers/specs/2026-06-17-ls-positioning-factors-design.md`

**Note on commits:** Per project policy, commits await explicit user approval. The commit steps below are the intended rhythm; at execution, checkpoint for approval (or batch at the end) rather than committing unprompted.

---

## File Structure

- **Modify** `research/pipeline/config.py` — add `binance_usdt` property to `SymbolConfig` (single source of truth for the `btc`→`BTCUSDT` map).
- **Modify** `research/lib/derived_factors.py` — add `positioning_factors(oi, candle_idx)`.
- **Modify** `research/pipeline/stage0a_features.py` — `_FACTOR_SOURCE` entries; `build_feature_dict` gains `oi_ls_df` param + wiring; run-loop loads the parquet graceful-absent.
- **Test** `research/tests/test_config.py`, `research/tests/test_derived_factors.py`, `research/tests/test_stage0a_features.py`.

---

## Task 1: `SymbolConfig.binance_usdt` symbol map

**Files:**
- Modify: `research/pipeline/config.py:44-47` (add property after `prefix`)
- Test: `research/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
def test_symbolconfig_binance_usdt():
    from pipeline.config import SymbolConfig
    s = SymbolConfig(name="btc", okx_swap="BTC-USDT-SWAP", ccxt_bybit="BTC/USDT:USDT")
    assert s.binance_usdt == "BTCUSDT"
    s2 = SymbolConfig(name="eth", okx_swap="ETH-USDT-SWAP", ccxt_bybit="ETH/USDT:USDT")
    assert s2.binance_usdt == "ETHUSDT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=research python -m pytest research/tests/test_config.py::test_symbolconfig_binance_usdt -v`
Expected: FAIL with `AttributeError: 'SymbolConfig' object has no attribute 'binance_usdt'`

- [ ] **Step 3: Add the property**

In `research/pipeline/config.py`, after the `prefix` property (line ~47):

```python
    @property
    def binance_usdt(self) -> str:
        """Binance USDT-perp ticker (e.g. 'BTCUSDT') for the daily-metrics archive."""
        return f"{self.name.upper()}USDT"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=research python -m pytest research/tests/test_config.py::test_symbolconfig_binance_usdt -v`
Expected: PASS

- [ ] **Step 5: Commit** (await approval)

```bash
git add research/pipeline/config.py research/tests/test_config.py
git commit -m "feat(config): SymbolConfig.binance_usdt ticker map"
```

---

## Task 2: `positioning_factors()` in derived_factors.py

**Files:**
- Modify: `research/lib/derived_factors.py` (append function)
- Test: `research/tests/test_derived_factors.py`

- [ ] **Step 1: Write the failing tests**

```python
import numpy as np
import pandas as pd
from lib.derived_factors import positioning_factors


def _oi_frame(n=800):
    idx = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "global_ls_accounts": np.linspace(1.0, 2.0, n),
            "toptrader_ls_positions": np.linspace(2.0, 1.0, n),
        },
        index=idx,
    )


def test_positioning_factors_emits_three_columns():
    oi = _oi_frame()
    out = positioning_factors(oi, oi.index)
    assert set(out) == {"global_ls_acct_z", "toptrader_ls_z", "ls_divergence"}


def test_positioning_factors_divergence_is_difference_of_zs():
    oi = _oi_frame()
    out = positioning_factors(oi, oi.index)
    expected = out["global_ls_acct_z"] - out["toptrader_ls_z"]
    pd.testing.assert_series_equal(out["ls_divergence"], expected, check_names=False)


def test_positioning_factors_missing_column_guard():
    oi = _oi_frame()[["global_ls_accounts"]]  # no toptrader column
    out = positioning_factors(oi, oi.index)
    assert set(out) == {"global_ls_acct_z"}  # no toptrader, hence no divergence


def test_positioning_factors_reindex_no_ffill():
    oi = _oi_frame()
    # candle grid extends 24h past the OI data → those hours must stay NaN
    candle_idx = pd.date_range("2022-01-01", periods=824, freq="h", tz="UTC")
    out = positioning_factors(oi, candle_idx)
    assert out["global_ls_acct_z"].iloc[800:].isna().all()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=research python -m pytest research/tests/test_derived_factors.py -k positioning -v`
Expected: FAIL with `ImportError: cannot import name 'positioning_factors'`

- [ ] **Step 3: Implement the function**

Append to `research/lib/derived_factors.py`:

```python
def positioning_factors(oi: pd.DataFrame, candle_idx: pd.Index) -> dict[str, pd.Series]:
    """Long/short positioning factors from the Binance OI/L-S archive columns.

    Each ratio is 30-day rolling z-scored on its native 1H index, then reindexed
    to the candle grid with NO ffill — gap hours stay NaN so screening IC isn't
    inflated. ``ls_divergence`` = retail z − smart-money z (rank-2 with the two
    z's; downstream ensembles must not combine all three — see spec).
    """
    out: dict[str, pd.Series] = {}
    if "global_ls_accounts" in oi.columns:
        out["global_ls_acct_z"] = _rolling_z(
            oi["global_ls_accounts"], SCREEN_ZSCORE_DAYS * 24
        ).reindex(candle_idx)
    if "toptrader_ls_positions" in oi.columns:
        out["toptrader_ls_z"] = _rolling_z(
            oi["toptrader_ls_positions"], SCREEN_ZSCORE_DAYS * 24
        ).reindex(candle_idx)
    if "global_ls_acct_z" in out and "toptrader_ls_z" in out:
        out["ls_divergence"] = out["global_ls_acct_z"] - out["toptrader_ls_z"]
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=research python -m pytest research/tests/test_derived_factors.py -k positioning -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit** (await approval)

```bash
git add research/lib/derived_factors.py research/tests/test_derived_factors.py
git commit -m "feat(factors): positioning_factors (global_ls/toptrader/divergence)"
```

---

## Task 3: Wire into `build_feature_dict` + `_FACTOR_SOURCE`

**Files:**
- Modify: `research/pipeline/stage0a_features.py` — `_FACTOR_SOURCE` (line ~126), `build_feature_dict` signature (line 200) + body (after line 273)
- Test: `research/tests/test_stage0a_features.py`

- [ ] **Step 1: Write the failing tests**

```python
import numpy as np
import pandas as pd
from pipeline.config import load_config
from pipeline.stage0a_features import build_feature_dict, _FACTOR_SOURCE


def _candles(n=800):
    idx = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    base = np.linspace(100.0, 200.0, n)
    return pd.DataFrame(
        {"open": base, "high": base * 1.01, "low": base * 0.99,
         "close": base, "volume": np.full(n, 10.0)},
        index=idx,
    )


def _oi_ls(n=800):
    idx = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"global_ls_accounts": np.linspace(1.0, 2.0, n),
         "toptrader_ls_positions": np.linspace(2.0, 1.0, n)},
        index=idx,
    )


def test_factor_source_has_positioning():
    for f in ("global_ls_acct_z", "toptrader_ls_z", "ls_divergence"):
        assert _FACTOR_SOURCE[f] == "positioning"


def test_build_feature_dict_includes_positioning_when_present():
    cfg = load_config()
    feats = build_feature_dict(_candles(), cfg, oi_ls_df=_oi_ls())
    assert "global_ls_acct_z" in feats
    assert "toptrader_ls_z" in feats
    assert "ls_divergence" in feats


def test_build_feature_dict_omits_positioning_when_absent():
    cfg = load_config()
    feats = build_feature_dict(_candles(), cfg)  # no oi_ls_df → 1H regression guard
    assert "global_ls_acct_z" not in feats
    assert "ls_divergence" not in feats
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=research python -m pytest research/tests/test_stage0a_features.py -k "positioning or factor_source" -v`
Expected: FAIL — `KeyError: 'global_ls_acct_z'` (source map) and `TypeError: ... unexpected keyword argument 'oi_ls_df'`

- [ ] **Step 3a: Add `_FACTOR_SOURCE` entries**

In `research/pipeline/stage0a_features.py`, inside the `_FACTOR_SOURCE` dict after the order-flow block (line ~126):

```python
    # positioning (Binance OI/L-S archive)
    "global_ls_acct_z": "positioning",
    "toptrader_ls_z": "positioning",
    "ls_divergence": "positioning",
```

- [ ] **Step 3b: Add the `positioning_factors` import**

Ensure the top-of-file import (line ~68) includes it:

```python
from lib.derived_factors import basis_factors, funding_factors, oi_factors, positioning_factors
```

- [ ] **Step 3c: Add the `oi_ls_df` parameter**

In `build_feature_dict` signature (line 200), after `orderflow_df`:

```python
    orderflow_df: pd.DataFrame | None = None,
    oi_ls_df: pd.DataFrame | None = None,
```

- [ ] **Step 3d: Wire the factor family**

In `build_feature_dict`, after the order-flow block (line ~273), before `return features`:

```python
    # ── Positioning factor family (Binance OI/L-S archive, 1H-native, no ffill) ─
    if oi_ls_df is not None and not oi_ls_df.empty:
        features.update(positioning_factors(oi_ls_df, candle_idx))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=research python -m pytest research/tests/test_stage0a_features.py -k "positioning or factor_source" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit** (await approval)

```bash
git add research/pipeline/stage0a_features.py research/tests/test_stage0a_features.py
git commit -m "feat(stage0a): wire positioning_factors into build_feature_dict"
```

---

## Task 4: Load the OI/L-S parquet in the stage-0a run loop

**Files:**
- Modify: `research/pipeline/stage0a_features.py:510-532` (load site, mirror `orderflow_df`)

- [ ] **Step 1: Add the graceful-absent load**

In the per-symbol run loop, after the order-flow cache block (line ~520) and before `build_feature_dict(...)`:

```python
        # ── 3c. Binance OI/L-S positioning cache (multi-year archive) ────────
        # research/data/oi/oi_<SYM>USDT_1H.parquet (dump_oi.py). Absent ⇒
        # positioning factors simply not present ⇒ 1H runs unaffected.
        oi_ls_df = None
        try:
            from lib import oi_metrics
            oi_dir = _REPO_ROOT / "research" / "data" / "oi"
            oi_ls_df = oi_metrics.load_oi_parquet(sym_cfg.binance_usdt, oi_dir)
            log.info("%s: loaded OI/L-S parquet (%d rows)", sym, len(oi_ls_df))
        except FileNotFoundError:
            oi_ls_df = None  # absent cache -> 1H runs unaffected
```

- [ ] **Step 2: Pass it to `build_feature_dict`**

Update the `build_feature_dict(...)` call (line ~524) to add the argument:

```python
            orderflow_df=orderflow_df,
            oi_ls_df=oi_ls_df,
        )
```

- [ ] **Step 3: Acceptance run (BTC) — real data**

The OI/L-S parquet already exists locally (`research/data/oi/oi_BTCUSDT_1H.parquet`).

Run:
```bash
RESEARCH_ONLY_SYMBOL=btc PYTHONPATH=research python -m research.pipeline.stage0a_features
RESEARCH_ONLY_SYMBOL=btc PYTHONPATH=research python -m research.pipeline.stage1_factors
```
Expected: stage-0a log shows `loaded OI/L-S parquet`; stage-1 evidence/manifest includes `global_ls_acct_z` with **negative** IC and **|IC| ≥ 0.03** at the 72h or 168h horizon (matches the screen: BTC `global_ls_acct_z` −0.048@72h).

- [ ] **Step 4: Full research-suite regression**

Run: `PYTHONPATH=research python -m pytest research/tests -q`
Expected: PASS (no regressions; dashboard/agent suites run separately).

- [ ] **Step 5: Commit** (await approval)

```bash
git add research/pipeline/stage0a_features.py
git commit -m "feat(stage0a): load Binance OI/L-S parquet graceful-absent"
```

---

## Self-Review

**Spec coverage:**
- 3 positioning factors + drop taker → Tasks 2–3 ✓
- `ls_divergence = global − toptrader`, rank-2 note → Task 2 (math test + docstring) ✓
- Graceful-absent / 1H zero-regression → Task 3 (omit-when-absent test) + Task 4 (FileNotFoundError→None) ✓
- Centralized symbol map → Task 1 ✓
- `_FACTOR_SOURCE` "positioning" → Task 3 ✓
- No IC-eval transform entry (1H-native) → nothing added to `_IC_NATIVE_FREQ`, correct ✓
- Quantified acceptance (|IC|≥0.03, screened sign, 72/168h) → Task 4 Step 3 ✓
- OI revival OUT of scope → `oi_df`/`oi_factors` untouched; new `oi_ls_df` is a separate input ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `positioning_factors(oi, candle_idx)` signature identical in Task 2 (def), Task 3 (import + call). Factor names `global_ls_acct_z` / `toptrader_ls_z` / `ls_divergence` identical across config-source map, function, tests. `oi_ls_df` param name consistent Task 3 ↔ Task 4. `sym_cfg.binance_usdt` (Task 1 property) used in Task 4.

**Note:** ETH/SOL acceptance (toptrader −/divergence −) is a fast-follow once BTC passes — same code path, just re-run with `RESEARCH_ONLY_SYMBOL=eth|sol`.
