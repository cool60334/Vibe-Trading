# Factor Induction (因子轉正) — Design

> Date: 2026-07-15 · Branch: `quant-trading-dashboard`
> Status: design approved, pending implementation plan
> Reviewers: self + agy adversarial review ×2 (design + spec-file passes; every point adjudicated against real code — e.g. kept the shared AST allowlist as-is since re-tightening it would touch Foundry, adding an induct-time determinism check instead).
> Depends on: `2026-07-14-foundry-pipeline-bridge-design.md` (B, code store + promote guard),
>             `2026-07-15-foundry-auto-scheduler-design.md` (A)

## 1. Problem

Foundry mines factors and the bridge (B) + scheduler (A) let them build strategies in **research**. But a selected Foundry strategy **cannot go live**: the trader reads `factor_values_<sym>.parquet`, which `refresh_factors.sh` recomputes daily from the **factor library's code** (`stage0a.build_feature_dict`). A Foundry factor's code is not in that library, so production could never refresh it → the factor goes stale → the trader's staleness guard pauses. B's `promote.py` guard (`assert_promotable_factor_names`) refuses to promote such a strategy exactly to avoid that dead-end.

**Induction (轉正)** closes the loop: it moves a vetted Foundry factor's code into the production factor library so `stage0a` computes it deterministically every day — making it a free, reproducible, trader-usable factor and unblocking deployment.

## 2. Locked decisions

| 決策 | 值 |
|---|---|
| How | Register the stored LLM `compute(df)` code (no hand-rewrite), human-gated + re-validated |
| Re-validation | AST allowlist re-gate + PIT re-check + determinism check + path-consistency reconcile vs the bridge overlay + committed golden test (all mandatory); performance stays the human's judgment (no auto OOS bar) |
| Trigger | per-factor, human-initiated (before deploying a strategy that needs it) |
| Code lives | **committed** as an importable module (production code is version-controlled, not left in the gitignored research store) |

## 3. Key facts (verified against code)

- Foundry code contract: **exactly one `compute(df) -> pd.Series`** preserving `df`'s DatetimeIndex (`forge.py:31`). The sandbox runs it via `exec(...); ns["compute"](df)`.
- Stored at `candidate_features/code/<sym>/<factor_id>.py` (+ `<id>.meta.json` with `code_sha256`) — **gitignored** research artifacts (B).
- AST gate already exists: `research/hermes/sandbox_ast.check_source(src)` — `ALLOWED_IMPORTS = {pandas, numpy, scipy, ta, math, statistics}`, bans `open/eval/exec/compile/__import__/getattr/setattr` and backward-fill. Reuse it.
- PIT re-check exists: `forge.pit_check_via_sandbox(code, panel, baseline, run)` (corrupts the future, asserts the past is unchanged). Reuse it.
- `stage0a.build_feature_dict(...) -> dict[str, pd.Series]` builds the library via `features.update(...)`. Inducted factors merge in here.
- B's guard: `promote.assert_promotable_factor_names(names)` refuses any `foundry_`-prefixed name. It must instead allow **inducted** ones.

## 4. Architecture

```
研究側 (gitignored)              induct (人工, 一次)                 production (committed, 版控)
candidate_features/code/  →  research.hermes.induct --symbol eth   →  research/lib/inducted/<sym>/<id>.py   (可 import module)
  <sym>/<id>.py                --factor foundry_X --confirm            research/lib/inducted/<sym>/<id>.meta.json  (provenance)
  + evidence card              1. check_source (strict AST allowlist)  research/tests/inducted/<id>_fixture.parquet + test  (golden)
                               2. pit_check_via_sandbox (full span)
                               3. generate golden fixture + test
                               4. --confirm → write the above
                                                                     → stage0a.build_feature_dict scans inducted/<sym>/,
                                                                        imports each, computes → features_<sym>.parquet
                                                                     → trader reads it; B guard allows the strategy → deploy
```

## 5. Components

### §5.1 `induct` CLI (`research/hermes/induct.py`, mirrors `promote.py`'s `--confirm` safety)
`python -m research.hermes.induct --symbol eth --factor foundry_X --confirm`
1. Load `candidate_features/code/<sym>/foundry_X.py` + its meta + evidence card. Refuse if the card's `verdict != candidate`, or the stored `code_sha256` does not match the file (tamper check).
2. **AST re-gate**: `sandbox_ast.check_source(code)`. A strict allowlist (no IO/time/network) is what makes `compute` a **pure function of the panel**, so a one-time PIT check is sufficient. Refuse on violation.
3. **PIT re-check over the full span**: run `pit_check_via_sandbox` (in the Docker sandbox, as forge does) against the full-span panel. Refuse on `LookaheadError`.
4. **Determinism check (agy #4)**: run `compute` twice on the same panel; refuse unless the two outputs are identical. This catches a factor that leaked RNG/global state past the allowlist (e.g. an unseeded scipy draw) — such a factor would never reconcile against the trader's daily recompute.
5. **Path-consistency reconcile (agy #6)**: the strategy that motivates this induction was backtested on the **bridge overlay** values. The trader will instead read `stage0a`'s inducted recompute. These must be the **same values**, or the strategy behaves differently live than in the backtest that selected it. So reconcile the inducted-path recompute against the bridge overlay's stored values (`np.allclose(..., equal_nan=True)`); refuse on divergence. (Same reconciliation shape B uses; same code + same full-library panel should match bit-for-bit.)
6. **Generate a golden regression test** (§5.5): sample a static fixture and its expected output, both committed. Tests the CODE LOGIC, decoupled from live data.
7. `--confirm` (required; default refuses like `promote.py`): write the importable module + `<id>.meta.json` (source `factor_id`, `code_sha256`, symbol, induction date) + the golden fixture/test; append an audit event. Without `--confirm`, print what WOULD happen and exit refusing.

### §5.2 Inducted factor discovery — directory scan, no central registry (agy #4)
- A factor is inducted **iff** `research/lib/inducted/<sym>/<id>.py` **and** `<id>.meta.json` exist. No `inducted_registry.json` — a single JSON would be a git-merge-conflict bottleneck across inductions.
- `inducted_names(symbol) -> set[str]` scans the directory (via `importlib.resources`, package-relative — agy #8) and returns the inducted factor names for a symbol.

### §5.3 `compute_inducted` (`research/lib/inducted_factors.py`)
- `compute_inducted(panel, symbol) -> dict[str, pd.Series]`. The `panel` is the **fully-built library** (OHLCV ∪ every standard library factor), passed after `build_feature_dict` finishes the library (§5.4) — an inducted factor may reference any library column but **may not depend on another inducted factor** (flat structure, no ordering/DAG). A missing column is caught by the soft-fail below (agy #3).
- For each inducted module, load it by **`importlib.util.spec_from_file_location(unique_name, path)` + `module_from_spec`** (import the `.py` by path — precise tracebacks, flake8/mypy, own namespace so same-named helpers can't clobber — agy #5/#7). The module name **must be globally unique** — `f"inducted_{symbol}_{factor_id}"` — or `sys.modules` caching would let one symbol's factor shadow another's (agy #8). Then call `compute(panel)`.
- **Soft-fail (agy #1, #6)**: wrap each factor's `compute` in `try/except Exception`; a factor that raises (KeyError on a missing column, an edge-case error) yields NaN + an error log and **must not** break `build_feature_dict` — the library factors still compute and the trader still gets its data. (Per-factor process isolation with a hard timeout was considered and dropped: `stage0a` already runs as a subprocess and Windows `spawn` + pickling the full panel per factor is costly and risks the daemon-can't-spawn `RuntimeError`. The five induct-time gates — AST allowlist, PIT, determinism, path reconcile, human review of committed code — are the real defense against a runaway factor; a residual pure-Python infinite loop is possible but low-risk given committed+reviewed code, and can be hardened later if it ever bites.)
- **Hot kill-switch (agy #7)**: before computing a factor, `compute_inducted` checks a blacklist file (`INDUCTED_BLACKLIST_FILE`, default `runs/inducted_blacklist.txt`, one factor name per line). A blacklisted factor is skipped (absent from the output) — so an operator can neutralise a factor that starts emitting poison values **live, without a redeploy or git revert**. The factor going absent makes the trader's own staleness/NaN guard pause that strategy — the safe outcome.

### §5.4 `stage0a` integration
- `build_feature_dict` builds the whole library first, then merges: build a `panel` = `candles` joined with the completed `features` dict, and `features.update(compute_inducted(panel, symbol))`. Running LAST guarantees the panel an inducted factor sees is the full library it was vetted on (agy #3). Deterministic, daily, into `features_<sym>.parquet`. Zero inducted factors → `compute_inducted` returns `{}` (no-op).

### §5.5 Golden regression test — static fixture + explicit tolerance (agy #3, #2)
- Pinning "first N live values" would go red whenever data is revised/re-sourced. Instead, `induct` samples a **static fixture** (`research/tests/inducted/<id>_fixture.parquet`, committed) and its expected `compute(fixture)` output, and writes `research/tests/inducted/test_<id>.py`.
- The assertion uses **`pandas.testing.assert_series_equal(got, expected, rtol=1e-5, atol=1e-8)`** — not bare equality. A `pandas`/`numpy`/`scipy` upgrade can shift the last bits of a float; an explicit tolerance treats that as legitimate and only fails on a real logic change (agy #2).

### §5.6 B promote-guard integration
- Change `promote.assert_promotable_factor_names(names, symbol)` — it must take the **`symbol`** (a strategy is per-symbol; the guard needs it to know which `inducted/<symbol>/` dir to scan — agy #5). A `foundry_`-prefixed name is refused **only if it is not inducted for that symbol** (consult `inducted_names(symbol)`). An inducted factor's code is in the library, so the trader can recompute it → the strategy is safe to deploy. The strategy keeps referencing `foundry_X` (no rename); the library now computes `foundry_X`.

## 6. Safety

- Inducted code runs in the **non-sandboxed** production compute path, so induction gates hard **before** writing: sha/verdict check + strict AST allowlist + full-span PIT re-check + determinism check + path-consistency reconcile vs the bridge overlay + human `--confirm`. Six gates; the committed code is also code-reviewable.
- The daily compute path **soft-fails** per factor (a bad factor → NaN + log, never breaks the batch) and honours a **hot kill-switch** blacklist so an operator can neutralise a live factor instantly without a redeploy.
- Inducted code is **committed** → code-reviewable, sha-traceable to its Foundry origin, and covered by a static-fixture golden test.

## 7. Error handling

- `induct`: non-candidate verdict / sha mismatch / AST violation / `LookaheadError` / non-deterministic recompute / bridge-overlay divergence / missing `--confirm` → refuse with a clear message, write nothing.
- `compute_inducted`: per-factor exception (KeyError on a missing column, edge-case error) → that factor is NaN + logged; the loop continues. A blacklisted factor is skipped.
- Missing/partial inducted dir → treated as zero inducted factors (no-op), never a crash.

## 8. Testing (TDD)

- **induct**: refuses a non-candidate card; refuses on sha mismatch; refuses AST-unsafe code; refuses on a PIT leak; refuses a non-deterministic factor (compute-twice differs); refuses on bridge-overlay divergence; without `--confirm` writes nothing; with `--confirm` writes the module + meta + golden fixture/test + audit event.
- **inducted discovery**: `inducted_names` scans the dir; a factor with only a `.py` (no meta) is not counted.
- **compute_inducted**: imports + computes an inducted factor into the dict; a factor that raises → NaN + others still computed (soft-fail); two factors (different symbols) with same-named helpers don't clobber (unique module name / namespace isolation); a blacklisted factor is skipped.
- **stage0a**: an inducted factor appears in `build_feature_dict`'s output over the full-library panel; zero inducted → unchanged output.
- **B guard**: an inducted `foundry_` factor (for that symbol) is allowed; a non-inducted `foundry_` factor is still refused; a factor inducted for a DIFFERENT symbol is still refused.
- **golden**: the generated test passes against its committed fixture within tolerance and fails if the compute logic changes.

## 9. Out of scope

- **Auto-selecting** which factor deserves induction (the human picks; the mechanism validates + moves).
- **Strategy deployment** itself (unchanged — goes through the existing promote flow once the guard passes).
- **Full de-induction workflow** (permanently removing an inducted factor from the library) — a later concern; permanent removal is a git revert of the committed module. **Emergency** neutralisation IS in scope (the §5.3 hot kill-switch blacklist stops a live factor instantly without a redeploy).
- **Candidate archival** in the research store (owned by the auto-scheduler's follow-up, not here).

## 10. Files touched

| 檔 | 動作 |
|---|---|
| `research/hermes/induct.py` | new: induct CLI (AST re-gate + PIT + determinism + bridge-overlay reconcile + golden gen + `--confirm`) |
| `research/lib/inducted_factors.py` | new: `inducted_names`, `compute_inducted` (importlib unique-name + soft-fail + hot kill-switch blacklist) |
| `research/lib/inducted/` | new package dir (committed inducted modules + meta land here) |
| `research/pipeline/stage0a_features.py` | `build_feature_dict` merges `compute_inducted(panel, symbol)` |
| `research/hermes/promote.py` | `assert_promotable_factor_names` allows inducted `foundry_` factors |
| `research/tests/inducted/` | committed golden fixtures + generated tests |
| `research/tests/test_*` | §8 tests |
