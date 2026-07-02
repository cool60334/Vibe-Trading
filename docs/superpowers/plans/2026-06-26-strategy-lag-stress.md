# Strategy-level lag-stress (#4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `stage3 --lag-stress`: re-run each strategy's backtest with signals shifted 1-2 bars later (compile-time), emit a `lag_stress` block, and gate on a non-fatal dual gate (absolute Sharpe floor + retention ratio).

**Architecture:** Mirror cost-stress. The lag is injected in `signal_compiler` (a `signal.shift(N)` on the position series — no `agent/backtest` change). stage3 recompiles+backtests per (window × lag); emit_manifest aggregates into `backtest.lag_stress` and adds `edge_survives_lag`.

**Tech Stack:** Python, pydantic, pytest.

**Spec:** `docs/superpowers/specs/2026-06-26-strategy-lag-stress-design.md`

---

## File Structure

- **Modify** `dashboard/server/schemas.py` — `LagStressLevel`, `LagStressBlock`, `BacktestBlock.lag_stress`, gate constants.
- **Modify** `dashboard/server/test_schemas.py`.
- **Modify** `research/lib/signal_compiler.py` — `compile_strategy(..., lag_bars=0)`.
- **Modify** `research/tests/test_signal_compiler.py`.
- **Modify** `research/pipeline/config.py` — `ResearchConfig.lag_stress_bars`.
- **Modify** `research/research_config.yaml` — `lag_stress_bars: [1, 2]`.
- **Modify** `research/pipeline/strategy_runs.py` — `update_lag_stress_runs` + `lag_stress_runs` on the entry.
- **Modify** `research/pipeline/stage3_backtest.py` — `lag_stress_run_plan`, `_run_lag_stress_for_strategy`, `--lag-stress`.
- **Modify** `research/tests/test_stage3_backtest.py`.
- **Modify** `research/emit_manifest.py` — build `lag_stress` block + `edge_survives_lag` gate.
- **Modify** `research/tests/test_emit_manifest.py`.

**⚠ Two pytest scopes:** research (`cd research && python -m pytest tests/<f> -v`), dashboard (`cd dashboard/server && python -m pytest <f> -v`). Never mix.

Order: 1 (schema) → 2 (compiler) → 3 (config) → 4 (stage3 runner) → 5 (emit gate). The runner (Task 4) reuses the compiler (Task 2) + config (Task 3); the gate (Task 5) reuses the schema (Task 1).

---

### Task 1: Schema — LagStress blocks + gate constants

**Files:**
- Modify: `dashboard/server/schemas.py` (near `CostStressLevel`/`CostStressBlock` ~line 351-368; `BacktestBlock` ~382; gate constants ~45)
- Test: `dashboard/server/test_schemas.py`

- [ ] **Step 1: Write the failing test**

Add to `dashboard/server/test_schemas.py` (add the new names to the schemas import; add constants to `test_canonical_gate_constants`):

```python
    assert GATE_MIN_LAG_SHARPE == 0.5
    assert GATE_MIN_LAG_RETENTION == 0.6
```

```python
def test_lag_stress_blocks():
    from schemas import LagStressLevel, LagStressBlock, BacktestBlock, BacktestMetrics
    lvl = LagStressLevel(label="lag1_train", source_run="r", lag_bars=1, window="train", sharpe=0.8)
    blk = LagStressBlock(source_run="r", levels=[lvl])
    assert blk.levels[0].lag_bars == 1 and blk.levels[0].window == "train"
    bt = BacktestBlock(in_sample=BacktestMetrics(source_run="b", sharpe=1.0), lag_stress=blk)
    assert bt.lag_stress.levels[0].sharpe == 0.8
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd dashboard/server && python -m pytest test_schemas.py::test_lag_stress_blocks test_schemas.py::test_canonical_gate_constants -v`
Expected: FAIL — names/constants missing.

- [ ] **Step 3: Add constants, blocks, field**

In `dashboard/server/schemas.py`, add near the other gate constants:

```python
GATE_MIN_LAG_SHARPE: float = 0.5       # absolute floor under entry-delay stress
GATE_MIN_LAG_RETENTION: float = 0.6    # lag_sharpe / base_sharpe must stay above this
```

Add near `CostStressLevel`/`CostStressBlock`:

```python
class LagStressLevel(_Manifest):
    """One entry-delay scenario (a shifted-signal backtest on one window)."""
    label: str = Field(..., description="e.g. 'lag1_train', 'lag2_oos'.")
    source_run: str
    lag_bars: int = Field(..., ge=1)
    window: str = Field(..., description="'train' | 'oos' | 'full'.")
    sharpe: Optional[float] = None


class LagStressBlock(_Manifest):
    source_run: Optional[str] = None
    levels: List[LagStressLevel] = Field(default_factory=list)
```

Add to `BacktestBlock` (next to `cost_stress`):

```python
    lag_stress: Optional[LagStressBlock] = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd dashboard/server && python -m pytest test_schemas.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/server/schemas.py dashboard/server/test_schemas.py
git commit -m "feat(schemas): LagStress blocks + edge_survives_lag gate constants"
```

---

### Task 2: `signal_compiler` lag_bars

**Files:**
- Modify: `research/lib/signal_compiler.py` (`compile_strategy` ~line 382; signal emission ~line 372)
- Test: `research/tests/test_signal_compiler.py`

- [ ] **Step 1: Write the failing test**

Add to `research/tests/test_signal_compiler.py` (reuse the module's existing spec fixture; if it has a `_spec()`/`make_spec` helper use that, else build a minimal valid `StrategySpec`):

```python
def test_lag_bars_appends_shift(_valid_spec):  # _valid_spec = an existing compilable StrategySpec fixture
    from lib.signal_compiler import compile_strategy
    assert ".shift(" not in compile_strategy(_valid_spec)                 # default lag_bars=0
    src = compile_strategy(_valid_spec, lag_bars=2)
    assert "signal = signal.shift(2).fillna(0.0)" in src
```

(If the test module has no ready spec fixture, construct one the same way the existing compile tests do.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd research && python -m pytest tests/test_signal_compiler.py::test_lag_bars_appends_shift -v`
Expected: FAIL — `compile_strategy()` has no `lag_bars` kwarg.

- [ ] **Step 3: Add `lag_bars` param + emit the shift**

In `research/lib/signal_compiler.py`, change the signature:

```python
def compile_strategy(spec: StrategySpec, yaml_hash: str = "", lag_bars: int = 0) -> str:
```

In the list of emitted body lines, right after the signal state-machine loop (the block ending with `signal.iloc[bar_i] = float(position)...`), append a shift line when `lag_bars > 0`. Locate where the loop body lines list is assembled and add:

```python
    if lag_bars > 0:
        body_lines.append(f"{i8}signal = signal.shift({int(lag_bars)}).fillna(0.0)")
```

(`i8` is the 8-space indent already used for top-level `generate()` body lines; `body_lines` is the list being joined into the function source — use the actual variable name in this file. The shift must be at the same indent as `signal = pd.Series(...)`, after the per-bar loop completes and before `signal` is returned/assigned out.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd research && python -m pytest tests/test_signal_compiler.py -v`
Expected: all PASS (new test + existing compile tests — `lag_bars` defaults 0, so existing output is unchanged).

- [ ] **Step 5: Commit**

```bash
git add research/lib/signal_compiler.py research/tests/test_signal_compiler.py
git commit -m "feat(signal_compiler): optional lag_bars shifts the position series"
```

---

### Task 3: Config `lag_stress_bars`

**Files:**
- Modify: `research/pipeline/config.py` (`ResearchConfig` dataclass ~line 74; loader)
- Modify: `research/research_config.yaml`
- Test: `research/tests/` (the config test module — search for the existing `load_config` test; if none, add a small one)

- [ ] **Step 1: Write the failing test**

Add a test where `load_config` is exercised (or construct a `ResearchConfig`); assert the new field:

```python
def test_lag_stress_bars_default_and_parse():
    from pipeline.config import ResearchConfig
    # dataclass has the field with a sensible default
    import dataclasses
    names = {f.name for f in dataclasses.fields(ResearchConfig)}
    assert "lag_stress_bars" in names
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd research && python -m pytest tests/ -k lag_stress_bars -v`
Expected: FAIL — field absent.

- [ ] **Step 3: Add the field + loader + yaml**

In `research/pipeline/config.py`, add to the `ResearchConfig` dataclass:

```python
    lag_stress_bars: list[int] = dataclasses.field(default_factory=lambda: [1, 2])
```

In the config-building code (where `ResearchConfig(...)` is constructed from the parsed yaml), read it:

```python
        lag_stress_bars=[int(x) for x in raw.get("lag_stress_bars", [1, 2])],
```

In `research/research_config.yaml`, add:

```yaml
lag_stress_bars: [1, 2]   # entry-delay stress in bars (stage3 --lag-stress)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd research && python -m pytest tests/ -k lag_stress_bars -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add research/pipeline/config.py research/research_config.yaml research/tests/
git commit -m "feat(config): lag_stress_bars (default [1,2])"
```

---

### Task 4: stage3 lag-stress runner

**Files:**
- Modify: `research/pipeline/strategy_runs.py` (`update_lag_stress_runs` + `lag_stress_runs` field, mirror `update_stress_runs`)
- Modify: `research/pipeline/stage3_backtest.py` (`lag_stress_run_plan`, `_run_lag_stress_for_strategy`, `--lag-stress`)
- Test: `research/tests/test_stage3_backtest.py`

- [ ] **Step 1: Write the failing test (pure plan function)**

Add to `research/tests/test_stage3_backtest.py`:

```python
def test_lag_stress_run_plan(monkeypatch):
    from pipeline.stage3_backtest import lag_stress_run_plan
    from pipeline.config import load_config
    cfg = load_config()
    # force a walk-forward split so windows = [train, oos]
    plan = lag_stress_run_plan("eth_s5_half_size", cfg)
    # windows (train+oos when oos_start set) x lags
    lags = cfg.lag_stress_bars
    assert len(plan) == 2 * len(lags) or len(plan) == 1 * len(lags)  # split vs full
    names, labels, windows, lag_vals = zip(*plan)
    assert set(lag_vals) == set(lags)
    assert all("lagstress" in n for n in names)
    assert all(w in ("train", "oos", "full") for w in windows)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd research && python -m pytest tests/test_stage3_backtest.py::test_lag_stress_run_plan -v`
Expected: FAIL — `lag_stress_run_plan` not defined.

- [ ] **Step 3: Add `lag_stress_run_plan`**

In `research/pipeline/stage3_backtest.py`, mirror `stress_run_plan`:

```python
def lag_stress_run_plan(
    strategy_id: str, cfg: ResearchConfig, today: date | None = None,
) -> list[tuple[str, str, str, int]]:
    """(run_name, label, window, lag_bars) for train+oos x cfg.lag_stress_bars."""
    if train_window(cfg, today) is not None and oos_window(cfg, today) is not None:
        windows = ["train", "oos"]
    else:
        windows = ["full"]
    plan: list[tuple[str, str, str, int]] = []
    for window in windows:
        for lag in cfg.lag_stress_bars:
            run_name = f"{strategy_id}_lagstress_{window}_lag{int(lag)}"
            label = f"lag{int(lag)}_{window}"
            plan.append((run_name, label, window, int(lag)))
    return plan
```

- [ ] **Step 4: Add `update_lag_stress_runs` + the runner + CLI flag**

In `research/pipeline/strategy_runs.py`, mirror `update_stress_runs`: add a `lag_stress_runs: list[str] = field(default_factory=list)` to the entry model and an `update_lag_stress_runs(strategy_id, run_names)` writer.

In `research/pipeline/stage3_backtest.py`, add `_run_lag_stress_for_strategy` mirroring `_run_stress_for_strategy`, but instead of `build_run_config(fee_multiplier=...)`, use the base config and a **recompiled lagged signal engine**:

```python
def _run_lag_stress_for_strategy(strategy_id, symbol, cfg, runs_root, strategies_dir, today=None):
    import yaml as _yaml
    from lib.signal_compiler import compile_strategy
    from schemas import StrategySpec
    spec = StrategySpec.model_validate(
        _yaml.safe_load((strategies_dir / f"strategy_{strategy_id}.yaml").read_text(encoding="utf-8"))
    )
    registered: list[str] = []
    for run_name, label, window, lag in lag_stress_run_plan(strategy_id, cfg, today):
        config = build_run_config(symbol, cfg, today)
        win = train_window(cfg, today) if window == "train" else oos_window(cfg, today)
        if win is not None:
            config["start_date"], config["end_date"] = win
        code = compile_strategy(spec, lag_bars=lag)
        run_dir = runs_root / run_name
        (run_dir / "code").mkdir(parents=True, exist_ok=True)
        (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        (run_dir / "code" / "signal_engine.py").write_text(code, encoding="utf-8")
        proc = subprocess.run([sys.executable, "-m", "backtest.runner", str(run_dir)],
                              cwd=str(_REPO_ROOT), capture_output=True, text=True)
        if proc.returncode == 0:
            registered.append(run_name)
            print(f"  [lag-stress] {run_name} (lag{lag}, {window})")
        else:
            print(f"  [lag-stress] {run_name} FAILED\n{proc.stderr[:400]}", file=sys.stderr)
    if registered:
        update_lag_stress_runs(strategy_id, registered)
```

Add a `--lag-stress` argparse flag and, in `main()`, gather lag-stress-eligible strategies exactly like `--stress` does and call `_run_lag_stress_for_strategy` for each. (Import `update_lag_stress_runs` alongside `update_stress_runs`.)

- [ ] **Step 5: Run to verify plan test passes + suite green**

Run: `cd research && python -m pytest tests/test_stage3_backtest.py -v`
Expected: all PASS (the pure `lag_stress_run_plan` test; the runner is integration, verified by the smoke run in Task 6-verification).

- [ ] **Step 6: Commit**

```bash
git add research/pipeline/stage3_backtest.py research/pipeline/strategy_runs.py research/tests/test_stage3_backtest.py
git commit -m "feat(stage3): --lag-stress runner (recompiled lagged signal per window)"
```

---

### Task 5: emit_manifest lag_stress block + `edge_survives_lag` gate

**Files:**
- Modify: `research/emit_manifest.py` (aggregation of lag_stress runs → block; `compute_gate` new threshold)
- Test: `research/tests/test_emit_manifest.py`

- [ ] **Step 1: Write the failing tests**

Add to `research/tests/test_emit_manifest.py`:

```python
class TestEdgeSurvivesLag:
    def _bt(self, is_sharpe, oos_sharpe, levels):
        from schemas import BacktestBlock, BacktestMetrics, LagStressBlock, LagStressLevel
        lag = LagStressBlock(source_run="r", levels=[
            LagStressLevel(label=l[0], source_run="r", lag_bars=l[1], window=l[2], sharpe=l[3])
            for l in levels])
        return BacktestBlock(
            in_sample=BacktestMetrics(source_run="b", sharpe=is_sharpe),
            oos=BacktestMetrics(source_run="o", sharpe=oos_sharpe),
            lag_stress=lag)

    def _th(self, gate):
        return {t.name: t for t in gate.thresholds}.get("edge_survives_lag")

    def test_retention_fail(self):
        # base 1.2, worst lag 0.6 -> retention 0.5 < 0.6 -> fail (floor 0.6>=0.5 passes)
        bt = self._bt(1.2, 1.2, [("lag1_train", 1, "train", 0.6), ("lag1_oos", 1, "oos", 0.6)])
        t = self._th(compute_gate(bt))
        assert t is not None and t.passed is False and t.fatal is False

    def test_floor_fail(self):
        # worst 0.4 < 0.5 floor -> fail even with fine retention
        bt = self._bt(0.45, 0.45, [("lag1_train", 1, "train", 0.4)])
        assert self._th(compute_gate(bt)).passed is False

    def test_passes(self):
        bt = self._bt(1.0, 1.0, [("lag1_train", 1, "train", 0.8), ("lag2_oos", 2, "oos", 0.8)])
        assert self._th(compute_gate(bt)).passed is True

    def test_nonpositive_base_skips_retention(self):
        # train base <=0 -> retention skipped; floor alone decides (0.6>=0.5 -> pass)
        bt = self._bt(-0.1, 1.5, [("lag1_train", 1, "train", 0.6)])
        assert self._th(compute_gate(bt)).passed is True

    def test_absent_when_no_lag_stress(self):
        from schemas import BacktestBlock, BacktestMetrics
        bt = BacktestBlock(in_sample=BacktestMetrics(source_run="b", sharpe=1.0))
        assert self._th(compute_gate(bt)) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd research && python -m pytest tests/test_emit_manifest.py::TestEdgeSurvivesLag -v`
Expected: FAIL — no `edge_survives_lag` threshold.

- [ ] **Step 3: Add the gate**

In `research/emit_manifest.py`, import `GATE_MIN_LAG_SHARPE, GATE_MIN_LAG_RETENTION` from schemas. In `compute_gate`, before `overall_pass = ...`, append:

```python
    # ── edge_survives_lag (execution-delay dual gate; non-fatal) ─────────────────
    if backtest.lag_stress is not None and backtest.lag_stress.levels:
        lvls = backtest.lag_stress.levels
        lag_sharpes = [l.sharpe for l in lvls if l.sharpe is not None]
        worst_lag = min(lag_sharpes) if lag_sharpes else None
        retentions: list[float] = []
        for l in lvls:
            base = is_m.sharpe if l.window == "train" else (backtest.oos.sharpe if backtest.oos else None)
            if l.sharpe is not None and base is not None and base > 0:
                retentions.append(l.sharpe / base)
        worst_ret = min(retentions) if retentions else None
        floor_ok = worst_lag is not None and worst_lag >= GATE_MIN_LAG_SHARPE
        ret_ok = worst_ret is None or worst_ret >= GATE_MIN_LAG_RETENTION
        thresholds.append(GateThreshold(
            name="edge_survives_lag", threshold=GATE_MIN_LAG_SHARPE,
            actual=worst_lag, passed=(floor_ok and ret_ok), fatal=False))
```

(`is_m` is the in-sample metrics already bound at the top of `compute_gate` — line ~205.)

- [ ] **Step 4: Build the `lag_stress` block when assembling the manifest**

In the manifest-assembly function (where `cost_stress` is aggregated from `stress_runs`), aggregate `lag_stress_runs` into a `LagStressBlock` the same way — one `LagStressLevel` per run, parsing `lag_bars`/`window` from the run label (`lag{N}_{window}`), reading `sharpe` from each run's `metrics.csv` — and set it on the `BacktestBlock(..., lag_stress=lag_block)`. (Mirror the existing `cost_stress` aggregation block; import `LagStressBlock`, `LagStressLevel`.)

- [ ] **Step 5: Run to verify all pass**

Run: `cd research && python -m pytest tests/test_emit_manifest.py -v`
Expected: all PASS (new gate tests + existing — `lag_stress` defaults None so existing gates unaffected).

- [ ] **Step 6: Commit**

```bash
git add research/emit_manifest.py research/tests/test_emit_manifest.py
git commit -m "feat(emit_manifest): lag_stress block + non-fatal edge_survives_lag dual gate"
```

---

## Verification checklist (after all tasks)

- [ ] `cd research && python -m pytest tests/test_signal_compiler.py tests/test_stage3_backtest.py tests/test_emit_manifest.py -q` — green.
- [ ] `cd dashboard/server && python -m pytest test_schemas.py -q` — green.
- [ ] `compile_strategy(spec)` (no lag) output unchanged (no `.shift(`); `lag_bars=N` appends the symmetric shift on the position series only.
- [ ] Dual gate: worst < 0.5 → fail (floor); retention < 0.6 → fail; base ≤ 0 window → retention skipped; non-fatal; absent when no lag_stress.
- [ ] No `agent/backtest` change.
- [ ] Live smoke (optional, server, docker container): `python -m research.pipeline.stage3_backtest --lag-stress` for one symbol → `<id>_lagstress_*` runs + `edge_survives_lag` in the re-emitted manifest gate.
