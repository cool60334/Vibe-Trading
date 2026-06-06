## Context

Two artefacts live alongside in `research/manifests/<strategy_id>/`:

| File | Producer (today) | Consumer | Schema |
|---|---|---|---|
| `diagnosis.json` | `stage3_diagnose.py` | stage5_select, emit_manifest | `DiagnosisBlock` |
| `optimization.json` | `stage4_optimize.py` | stage5_select, emit_manifest | (untyped dict) |
| `selection.json` (one global) | `stage5_select.py` | dashboard `/api/selection` | `SelectionManifest` |
| `manifest.json` | **`emit_manifest.py` — manual** | **dashboard `/api/strategies/*`** | `StrategyManifest` |

`emit_manifest.py::build_strategy_manifest(strategy_id, symbol, entry, runs_root, manifests_dir) -> dict` exists and aggregates everything needed (generation.json + diagnosis.json + optimization.json + metrics + gate computation) into the StrategyManifest shape. Its standalone `main()` already iterates `strategy_runs.json` and writes one file per strategy. But pipeline never invokes it.

## Goals / Non-Goals

**Goals:**
- After `stage5_select.py` writes `selection.json`, every strategy that appeared in stage 5's processing loop (eligible or skipped) has an up-to-date `manifest.json` written.
- Fail-soft: a single strategy's emit failure does not block emission for other strategies and does not abort the stage 5 run.
- Standalone `python -m research.emit_manifest` continues to work for backfilling (historical strategies not run through the new stage 5 path).
- Manifest emission is idempotent: re-running stage 5 overwrites with the latest aggregated data.

**Non-Goals:**
- Changing `StrategyManifest` schema.
- Modifying dashboard endpoints.
- Auto-emitting at earlier stages (3/4) — the manifest only stabilises after stage 5 produces final scoring + selected flag.
- Triggering testnet auto-start. Promotion is still a separate user-driven action.

## Decisions

### D1: Where to hook emission

Add the loop at the end of `stage5_select.py::main()`, after `selection.json` is written and before `sys.exit(...)`. This guarantees:
- `selection.json` is the source of truth for ranking + selected flag, written first.
- A stage 5 failure before the selection write skips emission cleanly.
- Per-strategy emit failures are reported in the same summary as the ranking table.

### D2: Which strategies get emitted

**All strategies registered in `strategy_runs.json`**, not just `eligible_for_ranking`. Rationale:
- A strategy tagged `back_to_stage_2` is correctly excluded from ranking, but the dashboard still needs to show "this concept was tested and rejected" — useful for research audit.
- `build_strategy_manifest` already handles partial-artifact strategies gracefully (returns a manifest with `pipeline_stage` reflecting how far it got).
- Symmetric with the existing standalone `emit_manifest.py::main()` behaviour.

### D3: Fail-soft per strategy

Wrap each `build_strategy_manifest(...)` call in `try/except Exception`:
- On exception: print red warning `[stage5] {sid}: manifest emit failed ({exc})`, continue loop.
- Track success/fail count in summary.
- Exit code: 0 if at least one manifest emitted successfully **AND** `selection.json` was written; non-zero only if `selection.json` failed (existing behaviour).

This matches the user expectation that a single bad strategy doesn't tank the whole pipeline run.

### D4: Refactor surface

`build_strategy_manifest` already returns a dict — no refactor needed for the data shape. We just need a thin write helper:

```python
def emit_manifest_for_strategy(
    strategy_id: str,
    entry: StrategyRunsEntry,
    runs_root: Path,
    manifests_dir: Path,
) -> Path:
    """Build + write manifest.json. Returns the written path."""
    manifest_dict = build_strategy_manifest(
        strategy_id=strategy_id,
        symbol=entry.symbol,
        entry=entry,
        runs_root=runs_root,
        manifests_dir=manifests_dir,
    )
    out_dir = manifests_dir / strategy_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "manifest.json"
    out_path.write_text(json.dumps(manifest_dict, indent=2, default=str), encoding="utf-8")
    return out_path
```

Place this in `research/emit_manifest.py` next to `build_strategy_manifest`. The existing `main()` uses it instead of duplicating its own write logic — small simplification.

### D5: Stage 5 summary lines

Add a new summary block after the ranking table:

```
Manifest emission
─────────────────
  [OK] btc_s7_stablecoin_funding_gated → research/manifests/btc_s7_stablecoin_funding_gated/manifest.json
  [OK] eth_s5_half_size → research/manifests/eth_s5_half_size/manifest.json
  ...
  Emitted: 8/8 manifests successfully.
```

### D6: Testing strategy

Stage 5 already has tests at `research/tests/test_stage5_select.py`. Add two scenarios:
- "manifest emitted for selected strategy" — after main(), assert `research/manifests/<sid>/manifest.json` exists and validates against `StrategyManifest`.
- "fail-soft: bad strategy doesn't abort emission" — mock `build_strategy_manifest` to raise for one strategy; assert others still emit + exit code unchanged.

## Risks / Trade-offs

- **[Risk] Disk write pressure on large strategy counts** — currently 8 strategies, but future archetype-factory backlog could push to 20-30. → **Mitigation**: each manifest is a few KB; negligible. No batching needed.
- **[Risk] Overwriting a manually-curated manifest** — if someone hand-edits `manifest.json`, the next stage 5 run clobbers it. → **Acceptance**: this is correct behaviour. `manifest.json` is a derived artefact, not a hand-edited one. Document this in PIPELINE.md.
- **[Trade-off] Eagerly emitting all strategies (including rejected)** — pollutes dashboard with concept-failed entries. → **Acceptance**: dashboard can filter on `pipeline_stage` / `gate.overall_pass` if it wants to hide them. Default behaviour is "show everything" which is more useful for research.
- **[Risk] Standalone `emit_manifest.py::main()` and stage 5 hook diverge over time** — both call the same `build_strategy_manifest` and write helper, so they stay in sync. → **Mitigation**: have stage 5's hook reuse the same `emit_manifest_for_strategy()` helper introduced in D4.

## Migration Plan

1. Add `emit_manifest_for_strategy()` helper in `research/emit_manifest.py`.
2. Refactor `main()` to use the helper (verify standalone CLI behaviour unchanged via existing tests).
3. Add the emission loop in `stage5_select.py::main()` after `selection.json` write.
4. Add unit tests for the emission integration.
5. Run `python -m research.pipeline.stage5_select` against current strategies; verify `manifest.json` appears for `eth_s5_half_size`, `btc_s9`, etc.
6. Boot the dashboard locally (`docker compose up` or `uvicorn dashboard.server.main:app`); confirm `eth_s5_half_size` shows up in `/api/strategies` and can be promoted via `POST /api/strategies/eth_s5_half_size/promote`.
7. Update PIPELINE.md.

Rollback: revert the diff in `stage5_select.py` (and `emit_manifest.py` if helper was extracted). The standalone CLI keeps working.
