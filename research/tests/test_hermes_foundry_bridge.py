import numpy as np
import pandas as pd
from research.hermes.foundry_bridge import FOUNDRY_PREFIX, recompute_full_span, reconciles_pre_oos


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


def test_reconciles_pre_oos_fails_when_window_is_empty():
    # If recomputed's entire index is at or after oos_start, there's nothing
    # to reconcile against — fail closed to avoid trusting an unevaluated factor.
    oos_cutoff = "2024-11-03"
    panel = _panel(50, start=oos_cutoff)  # starts at oos_start
    rec = pd.Series(np.arange(50.0), index=panel.index)
    stored = pd.Series(np.arange(50.0), index=panel.index)

    assert reconciles_pre_oos(rec, stored, oos_cutoff) is False


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


def test_card_to_entry_downgrades_verdict_when_regime_conditional():
    # Mixed IC signs across regimes -> classify_stability returns CONDITIONAL,
    # which must downgrade SINGLE_USE -> ENSEMBLE_ONLY via refine_verdict.
    from research.hermes.foundry_bridge import card_to_entry
    from research.factor_regime import classify_stability, refine_verdict
    from schemas import FactorStability, FactorVerdict

    class Card:                      # duck-typed EvidenceCard
        factor_id = "zoo_cond"
        gross_ic = 0.06
        ir = 0.4
        n_samples = 20000
        interval = "1H"
        regime_ic = {"bull": 0.05, "bear": -0.05, "neutral": 0.05}  # mixed signs

    assert classify_stability(Card.regime_ic) == FactorStability.CONDITIONAL

    entry = card_to_entry(Card(), horizon_h=24)

    assert entry.stability == FactorStability.CONDITIONAL
    assert entry.verdict == refine_verdict(FactorVerdict.SINGLE_USE, FactorStability.CONDITIONAL)
    assert entry.verdict == FactorVerdict.ENSEMBLE_ONLY


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


def test_build_overlay_drops_factor_absent_from_stored_parquet(tmp_path, monkeypatch):
    """Test that a factor with valid code but missing from the candidate parquet
    is gracefully dropped (not an exception), and does not appear in the overlay."""
    panel = _panel(100)
    # Seed only zoo_ok in the parquet; zoo_missing has code but no parquet entry
    _seed(tmp_path, panel, ["zoo_ok"])
    # Write code for zoo_missing even though it's not in the parquet
    write_candidate_code("zoo_missing", "eth", tmp_path, "code", {"code_sha256": ""})
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards",
                        lambda s, d: [_Card("zoo_ok"), _Card("zoo_missing")])
    run = lambda code, p: pd.Series(np.arange(float(len(p))), index=p.index)

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24)

    # zoo_missing should be dropped; only zoo_ok appears
    assert list(df.columns) == ["foundry_zoo_ok"]
    assert [e.name for e in entries] == ["foundry_zoo_ok"]
    assert "foundry_zoo_missing" not in df.columns


def test_write_overlay_emits_parquet_and_manifest(tmp_path):
    panel = _panel(10)
    df = pd.DataFrame({"foundry_x": np.arange(10.0)}, index=panel.index)
    from research.hermes.foundry_bridge import card_to_entry
    entries = [card_to_entry(_Card("x"), horizon_h=24)]

    write_overlay(tmp_path, "eth", df, entries)

    assert (tmp_path / "foundry_overlay_eth.parquet").exists()
    man = json.loads((tmp_path / "foundry_manifest_eth.json").read_text(encoding="utf-8"))
    assert man["factors"][0]["name"] == "foundry_x"


def test_build_overlay_drops_a_factor_whose_oos_window_is_entirely_nan(tmp_path, monkeypatch):
    # A factor that reproduces correctly pre-oos but is all-NaN across the OOS
    # window (e.g. depends on a column absent post-oos) must be dropped, not
    # kept with a dead OOS column.
    panel = _panel(100)
    _seed(tmp_path, panel, ["zoo_deadoos"])
    monkeypatch.setattr("research.hermes.foundry_bridge.load_cards",
                        lambda s, d: [_Card("zoo_deadoos")])

    def run(code, p):
        s = pd.Series(np.arange(float(len(p))), index=p.index)
        oos_mask = s.index >= pd.Timestamp(_OOS, tz="UTC")
        s = s.copy()
        s[oos_mask] = np.nan
        return s

    df, entries = build_overlay("eth", tmp_path, panel, run, _OOS, horizon_h=24)

    assert df.empty and entries == []


def test_bridge_soft_fails_to_empty_overlay_when_docker_unavailable(tmp_path, monkeypatch):
    # If docker is down, the bridge must NOT fail the discovery_pipeline job —
    # the stages still need to run on library factors. Empty overlay + exit 0.
    from research.hermes import foundry_bridge as fb
    from research.hermes.sandbox import SandboxError

    def boom(*a, **k):
        raise SandboxError("docker daemon unavailable")
    monkeypatch.setattr(fb, "resolve_image_id", boom)

    # main() loads production features + ohlcv BEFORE touching the sandbox
    # (load_features/load_ohlcv are real reads, unrelated to docker), so seed
    # both parquets in manifests-dir the same way stage0a would, with disjoint
    # column names so the features.join(ohlcv) inside main() doesn't collide.
    idx = pd.date_range("2024-11-01", periods=50, freq="1h", tz="UTC")
    pd.DataFrame({"mom_20": np.arange(50.0)}, index=idx).to_parquet(
        tmp_path / "features_eth.parquet")
    pd.DataFrame({"close": np.arange(50.0) + 100.0}, index=idx).to_parquet(
        tmp_path / "ohlcv_eth.parquet")

    ov = tmp_path / "ov"
    rc = fb.main(["--symbol", "eth", "--overlay-dir", str(ov),
                  "--image", "talos-sandbox:test",
                  "--manifests-dir", str(tmp_path)])

    assert rc == 0
    assert (ov / "foundry_overlay_eth.parquet").exists()   # empty overlay written
    assert pd.read_parquet(ov / "foundry_overlay_eth.parquet").shape[1] == 0
