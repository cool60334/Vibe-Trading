# A1 Cost-Model Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three backtest cost-model distortions (fixed funding rate, maker-charged close leg, unwired config fees) behind two-layer gating so the research pipeline defaults to realistic costs while the upstream engine stays byte-for-byte backward-compatible.

**Architecture:** Engine layer (`agent/backtest`) keeps legacy defaults; new behaviour activates only when new config keys are present (`taker_both_legs`, `funding_series_path`). Research pipeline (`build_run_config`) injects realistic-cost keys by default; `legacy_costs: true` suppresses them. A `cost_model_version` tag rides on every run and a same-pool guard blocks mixing v1/v2 in cross-strategy comparisons.

**Tech Stack:** Python, pandas, pyarrow, pytest. Three separate pytest scopes: `pytest agent/tests/` (engine), `python -m pytest research/tests/` (pipeline), both run from repo root.

**Design spec:** `docs/talos/specs/2026-07-07-a1-cost-model-design.md` (arbitrated across two agy rounds).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `agent/backtest/engines/crypto.py` | `taker_both_legs` flag; load funding parquet → O(1) dict in `__init__`; pass lookup to hook | Modify |
| `agent/backtest/engines/_market_hooks.py` | `calc_crypto_funding_fee` optional `funding_lookup`; fail-loud on missing/NaN at settlement | Modify |
| `agent/tests/test_crypto_cost_model.py` | Engine unit tests (commission, funding series, PIT, fail-loud, init cache) | Create |
| `research/tests/test_golden_run.py` | Add realistic-path frozen snapshot alongside existing legacy GOLDEN | Modify |
| `research/pipeline/config.py` | Parse `legacy_costs` bool | Modify |
| `research/pipeline/stage3_backtest.py` | `build_run_config` realistic default + legacy suppression + `cost_model_version` | Modify |
| `research/research_config.yaml` | Document `legacy_costs` | Modify |
| `research/pipeline/stage5_select.py` | Same-pool `cost_model_version` guard | Modify |
| `research/emit_manifest.py` | Write `cost_model_version` into manifest; guard on aggregation | Modify |
| `research/tests/test_cost_model_gating.py` | Pipeline gating + guard tests | Create |

**Constitutional constraints (every task):** three pytest scopes run separately; server stays read-only (this item is local-only, not deployed this round); checkpoint after each task — stop for approval.

---

## Task 0: Business-impact PoC (throwaway, checkpoint BEFORE engineering)

**Rationale (agy #6):** estimate the Sharpe cliff before building. If a representative live strategy collapses catastrophically under realistic costs, escalate to re-evaluate alpha before spending effort on Tasks 1–5.

**Files:**
- Reuse/adapt: `research/diagnostics/eth_s5_real_funding_recompute.py` (existing precedent — pairs trades.csv, accrues real funding at settlements)

**Note:** The precedent uses `funding_series.asof(ts)` (forward-fill) — acceptable for a rough throwaway estimate only. The PRODUCTION path (Task 2) must NOT ffill; it fails loud on missing values.

- [ ] **Step 1: Run the existing real-funding diagnostic for a deployed strategy**

Run: `python -m research.diagnostics.eth_s5_real_funding_recompute`
Expected: prints per-run (train/oos) real-vs-fixed funding cost delta and adjusted sharpe.

- [ ] **Step 2: Extend the estimate to add both-legs-taker + config fees**

In a scratch copy, add to the per-round-trip cost: an extra close-leg taker delta `qty * exit_price * (0.00055 - 0.0002)` (maker→taker on close) and the open/close taker bump `(0.00055 - 0.0005)` both legs. Recompute annualised sharpe delta.

- [ ] **Step 3: CHECKPOINT — report the Sharpe delta to the user**

Present: for the representative strategy, legacy sharpe vs realistic sharpe (funding + fees combined). State whether the cliff looks within acceptable range. **STOP. Do not proceed to Task 1 without approval.**

---

## Task 1: Engine — both-legs-taker flag

**Files:**
- Modify: `agent/backtest/engines/crypto.py:34-59`
- Test: `agent/tests/test_crypto_cost_model.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_crypto_cost_model.py
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from backtest.engines.crypto import CryptoEngine  # noqa: E402


def test_close_leg_uses_maker_by_default():
    """Legacy: open hits taker, close hits maker (unchanged upstream behaviour)."""
    eng = CryptoEngine({"taker_rate": 0.00055, "maker_rate": 0.0002})
    open_fee = eng.calc_commission(size=1.0, price=100.0, _direction=1, is_open=True)
    close_fee = eng.calc_commission(size=1.0, price=100.0, _direction=-1, is_open=False)
    assert open_fee == 1.0 * 100.0 * 0.00055
    assert close_fee == 1.0 * 100.0 * 0.0002


def test_close_leg_uses_taker_when_flag_set():
    """taker_both_legs=True: both legs charged taker (matches live market orders)."""
    eng = CryptoEngine({"taker_rate": 0.00055, "maker_rate": 0.0002, "taker_both_legs": True})
    close_fee = eng.calc_commission(size=1.0, price=100.0, _direction=-1, is_open=False)
    assert close_fee == 1.0 * 100.0 * 0.00055
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest agent/tests/test_crypto_cost_model.py -v`
Expected: `test_close_leg_uses_taker_when_flag_set` FAILS (close_fee uses maker 0.0002, not taker).

- [ ] **Step 3: Implement the flag**

In `agent/backtest/engines/crypto.py`, in `__init__` after line 39 add:
```python
        self.taker_both_legs: bool = config.get("taker_both_legs", False)
```
Change `calc_commission` (line 58) from:
```python
        rate = self.taker_rate if is_open else self.maker_rate
```
to:
```python
        rate = self.taker_rate if (is_open or self.taker_both_legs) else self.maker_rate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest agent/tests/test_crypto_cost_model.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/backtest/engines/crypto.py agent/tests/test_crypto_cost_model.py
git commit -m "feat(engine): opt-in taker_both_legs for crypto close leg"
```

---

## Task 2: Engine — funding series with init cache, PIT lookup, fail-loud

**Files:**
- Modify: `agent/backtest/engines/_market_hooks.py:149-211` (add `funding_lookup` param)
- Modify: `agent/backtest/engines/crypto.py:34-72` (load parquet in `__init__`, pass lookup)
- Test: `agent/tests/test_crypto_cost_model.py` (append)

- [ ] **Step 1: Write the failing tests (hook-level funding_lookup)**

Append to `agent/tests/test_crypto_cost_model.py`:
```python
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from backtest.engines._market_hooks import calc_crypto_funding_fee  # noqa: E402
from backtest.models import Position  # noqa: E402


def _long_pos(symbol="BTC-USDT-SWAP", size=1.0, price=100.0):
    return {symbol: Position(symbol=symbol, direction=1, size=size,
                             entry_price=price, leverage=1.0)}


def test_funding_lookup_uses_settlement_timestamp_value():
    """At settlement bar T, charge funding_rate_raw[T] — NOT a future value (PIT)."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")            # settlement hour
    future_ts = pd.Timestamp("2024-01-01 16:00")
    lookup = {ts: 0.0003, future_ts: 0.0009}         # T value 0.0003, future 0.0009
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(
        sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
        interval="1H", funding_lookup=lookup,
    )
    # notional 100 * rate 0.0003 * direction +1 — uses T's value, not 0.0009
    assert fee == pytest.approx(100.0 * 0.0003 * 1)


def test_funding_lookup_missing_value_fails_loud():
    """Path provided but settlement timestamp absent → raise, never silently fallback."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")
    lookup = {pd.Timestamp("2024-01-01 00:00"): 0.0002}   # 08:00 missing
    bar = pd.Series({"close": 100.0})
    with pytest.raises(ValueError, match="funding value missing"):
        calc_crypto_funding_fee(
            sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
            interval="1H", funding_lookup=lookup,
        )


def test_funding_lookup_nan_value_fails_loud():
    """NaN at a settlement point is a data gap → fail loud, not fallback."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")
    lookup = {ts: float("nan")}
    bar = pd.Series({"close": 100.0})
    with pytest.raises(ValueError, match="funding value missing"):
        calc_crypto_funding_fee(
            sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
            interval="1H", funding_lookup=lookup,
        )


def test_no_funding_lookup_uses_fixed_rate():
    """No lookup (legacy / path absent) → fixed scalar, unchanged behaviour."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 08:00")
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(
        sym, bar, ts, _long_pos(sym), 0.0001, set(), set(), interval="1H",
    )
    assert fee == pytest.approx(100.0 * 0.0001 * 1)


def test_non_settlement_bar_charges_nothing():
    """Bar outside {0,8,16} never settles even with a lookup present."""
    sym = "BTC-USDT-SWAP"
    ts = pd.Timestamp("2024-01-01 09:00")            # not a settlement hour
    lookup = {ts: 0.0003}
    bar = pd.Series({"close": 100.0})
    fee = calc_crypto_funding_fee(
        sym, bar, ts, _long_pos(sym), 0.0001, set(), set(),
        interval="1H", funding_lookup=lookup,
    )
    assert fee == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest agent/tests/test_crypto_cost_model.py -v -k funding`
Expected: FAIL — `calc_crypto_funding_fee` has no `funding_lookup` parameter (TypeError).

- [ ] **Step 3: Add `funding_lookup` to the hook**

In `agent/backtest/engines/_market_hooks.py`, change the `calc_crypto_funding_fee` signature (line 149) to add a keyword-only param at the end:
```python
def calc_crypto_funding_fee(
    symbol: str,
    bar: pd.Series,
    timestamp: pd.Timestamp,
    positions: Dict[str, Position],
    funding_rate: float,
    applied_set: set,
    daily_done_set: set,
    interval: str = "1D",
    funding_lookup: "Dict[pd.Timestamp, float] | None" = None,
) -> float:
```
Replace the fee computation tail (currently lines 205-211):
```python
    pos = positions.get(symbol)
    if pos is None:
        return 0.0

    mark_price = float(bar.get("close", pos.entry_price))
    notional = pos.size * mark_price
    return notional * funding_rate * pos.direction
```
with:
```python
    pos = positions.get(symbol)
    if pos is None:
        return 0.0

    mark_price = float(bar.get("close", pos.entry_price))
    notional = pos.size * mark_price

    # Realistic funding: look up the rate settled AT this timestamp (PIT-safe —
    # the value published at settlement T, never a future T+8h value). A provided
    # lookup with a missing/NaN value at a settlement point is a data gap, not a
    # licence to silently fall back — fail loud (agy #3).
    rate = funding_rate
    if funding_lookup is not None:
        looked_up = funding_lookup.get(timestamp)
        if looked_up is None or pd.isna(looked_up):
            raise ValueError(
                f"funding value missing for {symbol} at settlement {timestamp} "
                f"(funding_series provided but value absent/NaN — patch the feature file)"
            )
        rate = float(looked_up)

    return notional * rate * pos.direction
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest agent/tests/test_crypto_cost_model.py -v -k funding`
Expected: all 5 funding tests PASS.

- [ ] **Step 5: Write the failing test for engine init cache**

Append to `agent/tests/test_crypto_cost_model.py`:
```python
def test_engine_loads_funding_series_into_dict(tmp_path):
    """__init__ loads funding_series_path parquet → timestamp-keyed dict once."""
    idx = pd.date_range("2024-01-01", periods=24, freq="h")
    df = pd.DataFrame({"funding_rate_raw": [0.0001] * 24}, index=idx)
    p = tmp_path / "features_btc.parquet"
    df.to_parquet(p)
    eng = CryptoEngine({"funding_series_path": str(p)})
    assert eng._funding_lookup is not None
    assert eng._funding_lookup[idx[8]] == pytest.approx(0.0001)


def test_engine_no_funding_path_leaves_lookup_none():
    eng = CryptoEngine({})
    assert eng._funding_lookup is None
```

- [ ] **Step 6: Run to verify it fails**

Run: `pytest agent/tests/test_crypto_cost_model.py -v -k engine_loads`
Expected: FAIL — `CryptoEngine` has no `_funding_lookup`.

- [ ] **Step 7: Load parquet in engine `__init__` and pass lookup in `on_bar`**

In `agent/backtest/engines/crypto.py`, add at the top of the file with the other imports:
```python
from pathlib import Path
```
In `__init__` after the `taker_both_legs` line add:
```python
        self._funding_lookup: "dict | None" = None
        fsp = config.get("funding_series_path")
        if fsp:
            fdf = pd.read_parquet(Path(fsp), columns=["funding_rate_raw"])
            self._funding_lookup = fdf["funding_rate_raw"].to_dict()
```
In `on_bar`, change the `calc_crypto_funding_fee(...)` call (line 67) to pass the lookup:
```python
        fee = calc_crypto_funding_fee(
            symbol, bar, timestamp, self.positions,
            self.funding_rate, self._funding_applied, self._funding_daily_done,
            interval=self.interval, funding_lookup=self._funding_lookup,
        )
```

- [ ] **Step 8: Run to verify all engine tests pass**

Run: `pytest agent/tests/test_crypto_cost_model.py -v`
Expected: all tests PASS.

- [ ] **Step 9: Verify the existing golden run is still green (legacy untouched)**

Run: `python -m pytest research/tests/test_golden_run.py -v`
Expected: PASS — no new keys were passed, so engine defaults are byte-identical.

- [ ] **Step 10: Commit**

```bash
git add agent/backtest/engines/crypto.py agent/backtest/engines/_market_hooks.py agent/tests/test_crypto_cost_model.py
git commit -m "feat(engine): opt-in funding_series_path with PIT lookup and fail-loud on gaps"
```

---

## Task 3: Pipeline — legacy_costs config, realistic default, config-fee wiring, cost_model_version

**Files:**
- Modify: `research/pipeline/config.py:74-100` (add `legacy_costs` field), `:280-306` (parse)
- Modify: `research/pipeline/stage3_backtest.py:166-217` (`build_run_config`)
- Modify: `research/research_config.yaml`
- Test: `research/tests/test_cost_model_gating.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# research/tests/test_cost_model_gating.py
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from research.pipeline.config import load_config
from research.pipeline.stage3_backtest import build_run_config
from datetime import date

_TODAY = date(2024, 12, 31)


def test_realistic_default_wires_config_fees_and_funding():
    cfg = load_config()  # legacy_costs unset → realistic
    rc = build_run_config("BTC-USDT-SWAP", cfg, today=_TODAY)
    assert rc["taker_rate"] == cfg.fees.taker_rate       # config fees wired
    assert rc["maker_rate"] == cfg.fees.maker_rate
    assert rc["taker_both_legs"] is True
    assert rc["funding_series_path"].endswith("features_btc.parquet")
    assert rc["cost_model_version"] == "v2_realistic"


def test_legacy_costs_suppresses_realistic_keys(monkeypatch):
    cfg = load_config()
    object.__setattr__(cfg, "legacy_costs", True)
    rc = build_run_config("BTC-USDT-SWAP", cfg, today=_TODAY)
    assert "taker_both_legs" not in rc                    # engine keeps hardcoded default
    assert "funding_series_path" not in rc
    assert "taker_rate" not in rc                          # no config-fee injection
    assert rc["cost_model_version"] == "v1_legacy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest research/tests/test_cost_model_gating.py -v`
Expected: FAIL — `ResearchConfig` has no `legacy_costs`; `build_run_config` emits no realistic keys.

- [ ] **Step 3: Add `legacy_costs` to ResearchConfig**

In `research/pipeline/config.py`, in the `ResearchConfig` dataclass (after `oos_start`, ~line 90) add:
```python
    # When True, the pipeline suppresses realistic-cost keys so the engine uses
    # its hardcoded legacy defaults (byte-for-byte reproduction of old runs).
    # Default False = realistic costs (agy Option B). Debug backdoor only.
    legacy_costs: bool = False
```
In the loader (after the fees block, ~line 306) add parsing before the `ResearchConfig(...)` construction:
```python
    legacy_costs = bool(raw.get("legacy_costs", False))
```
and pass `legacy_costs=legacy_costs` into the `ResearchConfig(...)` constructor call.

- [ ] **Step 4: Wire realistic defaults + version tag in build_run_config**

In `research/pipeline/stage3_backtest.py`, at the end of `build_run_config` (before `return config`, ~line 216) replace the trailing `return config` region with:
```python
    if fee_multiplier is not None:
        for key, base_rate in DEFAULT_FEES.items():
            config[key] = base_rate * fee_multiplier

    if cfg.legacy_costs:
        config["cost_model_version"] = "v1_legacy"
    else:
        # Realistic cost model (agy Option B): wire config fees, charge taker on
        # both legs, and inject the real funding series. Engine stays back-compat
        # because these keys are opt-in; the pipeline opts in by default.
        config["maker_rate"] = cfg.fees.maker_rate
        config["taker_rate"] = cfg.fees.taker_rate
        config["slippage"] = cfg.fees.slippage
        config["taker_both_legs"] = True
        short = symbol_to_short(symbol)
        config["funding_series_path"] = str(
            _REPO_ROOT / "research" / "manifests" / f"features_{short}.parquet"
        )
        config["cost_model_version"] = "v2_realistic"
    return config
```
(Note: `fee_multiplier` stress path is unchanged and coexists; when both apply, the realistic block still tags the version.)

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest research/tests/test_cost_model_gating.py -v`
Expected: both tests PASS.

- [ ] **Step 6: Document legacy_costs in research_config.yaml**

Add near the fees block in `research/research_config.yaml`:
```yaml
# ─── 成本模型 gating（A1，default = realistic） ──────────────────────────────
# 預設（不設此欄）= 真實成本：config fees 雙邊 taker + 真 funding 序列。
# legacy_costs: true   # 除錯後門：退回引擎硬編碼舊費率，重現舊回測數字。日常勿開。
```

- [ ] **Step 7: Commit**

```bash
git add research/pipeline/config.py research/pipeline/stage3_backtest.py research/research_config.yaml research/tests/test_cost_model_gating.py
git commit -m "feat(pipeline): realistic-cost default with legacy_costs backdoor and cost_model_version tag"
```

---

## Task 4: Guard — block mixed cost_model_version in cross-strategy comparison

**Files:**
- Modify: `research/emit_manifest.py` (write tag into manifest; helper to read it)
- Modify: `research/pipeline/stage5_select.py` (assert same version across the compared pool)
- Test: `research/tests/test_cost_model_gating.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `research/tests/test_cost_model_gating.py`:
```python
from research.pipeline.stage5_select import assert_uniform_cost_model


def test_guard_passes_on_uniform_version():
    assert_uniform_cost_model([
        {"id": "a", "cost_model_version": "v2_realistic"},
        {"id": "b", "cost_model_version": "v2_realistic"},
    ])  # no raise


def test_guard_fails_on_mixed_version():
    import pytest
    with pytest.raises(ValueError, match="mixed cost_model_version"):
        assert_uniform_cost_model([
            {"id": "a", "cost_model_version": "v2_realistic"},
            {"id": "b", "cost_model_version": "v1_legacy"},
        ])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest research/tests/test_cost_model_gating.py -v -k guard`
Expected: FAIL — `assert_uniform_cost_model` does not exist.

- [ ] **Step 3: Implement the guard helper**

In `research/pipeline/stage5_select.py`, add near the top-level helpers:
```python
def assert_uniform_cost_model(entries: "list[dict]") -> None:
    """Fail loud if a cross-strategy pool mixes cost_model_version (agy #4).

    Comparing v1_legacy (understated costs → inflated sharpe) against
    v2_realistic in one leaderboard would systematically favour legacy
    strategies. Any comparison pool must be single-version.
    """
    versions = {
        e.get("cost_model_version", "v1_legacy")
        for e in entries
    }
    if len(versions) > 1:
        raise ValueError(
            f"mixed cost_model_version in comparison pool: {sorted(versions)} — "
            "re-run stale strategies under the current cost model before comparing"
        )
```

- [ ] **Step 4: Call the guard at the cross-strategy selection point**

In `research/pipeline/stage5_select.py`, immediately before the code that ranks/compares strategies across the pool (the selection aggregation), call `assert_uniform_cost_model(<the list of per-strategy manifest dicts>)`. Read each strategy's `cost_model_version` from its manifest (written in Step 5 below); default missing to `"v1_legacy"`.

- [ ] **Step 5: Write cost_model_version into the manifest**

In `research/emit_manifest.py`, when assembling the manifest dict for a strategy, read the run's `config.json` `cost_model_version` (default `"v1_legacy"` if absent) and set `manifest["cost_model_version"]`. This is the field the guard reads.

- [ ] **Step 6: Run to verify guard tests pass**

Run: `python -m pytest research/tests/test_cost_model_gating.py -v -k guard`
Expected: both guard tests PASS.

- [ ] **Step 7: Run the full research scope to confirm no regression**

Run: `python -m pytest research/tests/ -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add research/pipeline/stage5_select.py research/emit_manifest.py research/tests/test_cost_model_gating.py
git commit -m "feat(pipeline): same-pool cost_model_version guard against v1/v2 leaderboard contamination"
```

---

## Task 5: Re-freeze golden realistic path + baseline rerun (CHECKPOINT)

**Files:**
- Modify: `research/tests/test_golden_run.py`

- [ ] **Step 1: Add a realistic-path golden test**

In `research/tests/test_golden_run.py`, add a second test that constructs the same synthetic OHLCV, writes a synthetic funding parquet (constant + a couple of sign flips) to a tmp file, and runs `CryptoEngine` with `{"taker_both_legs": True, "taker_rate": 0.00055, "funding_series_path": <tmp>}`. Capture the metrics dict.

- [ ] **Step 2: Capture and freeze the realistic snapshot**

Run the test once with a print of the metrics, paste the values into a `GOLDEN_REALISTIC` dict, and assert with `pytest.approx(expected, abs=1e-9)` — mirroring the existing legacy `GOLDEN` block. The two snapshots MUST differ (proves the gating actually changes behaviour).

- [ ] **Step 3: Run both golden tests**

Run: `python -m pytest research/tests/test_golden_run.py -v`
Expected: both legacy and realistic frozen tests PASS.

- [ ] **Step 4: Rerun a representative real baseline and record the delta**

Run stage 3 for one deployed strategy under the new default (realistic) and compare its sharpe against the archived legacy number. Record btc/eth/sol deltas.

- [ ] **Step 5: CHECKPOINT — human review of the cliff**

Present the sharpe deltas and confirm they match the Task 0 PoC estimate within reason. **STOP for approval before any promote/re-selection decisions.**

- [ ] **Step 6: Commit**

```bash
git add research/tests/test_golden_run.py
git commit -m "test(golden): freeze realistic cost-model snapshot alongside legacy"
```

---

## Task 6: agy adversarial review of the full diff (real-money engine change)

- [ ] **Step 1: Produce the diff**

Run: `git diff main...HEAD -- agent/backtest research/pipeline research/emit_manifest.py`

- [ ] **Step 2: Send to agy-verify**

Use the `agy-verify` skill on the diff, focused on: PIT correctness of the funding lookup, that legacy path is provably byte-identical, and that the guard has no bypass.

- [ ] **Step 3: CHECKPOINT — triage findings**

Adopt/reject each finding point-by-point with rationale (per user preference). Fix adopted findings inline, then re-run all three pytest scopes.

---

## Self-Review Notes

- **Spec coverage:** §2 gating → Tasks 1/2/3; §3 PIT/fail-loud → Task 2 (tests a–d); §4 tag+guard → Tasks 3/4; §5 tests → all tasks; §6 footprint → File Structure; §7 order incl. step-0 PoC → Tasks 0–6. Covered.
- **Type consistency:** `cost_model_version` values `"v1_legacy"` / `"v2_realistic"` used identically in Tasks 3/4/5; `funding_lookup` param name consistent across hook + engine; `taker_both_legs` / `funding_series_path` config keys consistent across engine + pipeline.
- **agy #7 (multi-asset)** dismissed: `build_run_config` is crypto-only (`source` always okx) — no asset-class guard needed.
