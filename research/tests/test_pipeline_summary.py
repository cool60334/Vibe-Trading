"""pipeline_summary — one machine-readable verdict file per pipeline run."""
import json
import sys
from pathlib import Path

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from pipeline.lib.pipeline_summary import build_pipeline_summary, write_pipeline_summary  # noqa: E402


def _manifest(action, fatal, sharpe):
    # NOTE: real manifest.json (StrategyManifest schema, see
    # dashboard/server/schemas.py) has NO top-level "selected" field — that
    # only lives in selection.json's SelectionEntry (per strategy_id), a
    # separate file written by stage5. Verified against emit_manifest.py,
    # which never writes "selected" into manifest.json. OOS trade count is
    # BacktestMetrics.trades, not "trade_count".
    return {
        "diagnosis": {"recommended_action": action},
        "gate": {"overall_pass": not fatal, "fatal_fail": fatal},
        "backtest": {"oos": {"sharpe": sharpe, "max_drawdown": -0.05, "trades": 42}},
    }


def _write(manifests, sid, data):
    d = manifests / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


def _write_selection(manifests, ranking):
    (manifests / "selection.json").write_text(
        json.dumps({
            "schema_version": 1,
            "generated_at": "2026-07-05T00:00:00+00:00",
            "method": "weighted_composite_score_v1",
            "ranking": ranking,
        }),
        encoding="utf-8",
    )


def test_build_summary_one_row_per_strategy(tmp_path):
    _write(tmp_path, "eth_s5", _manifest("proceed", False, 1.02))
    _write(tmp_path, "btc_s9", _manifest("back_to_stage_4", True, 0.3))
    _write_selection(tmp_path, [
        {"strategy_id": "eth_s5", "symbol": "ETH", "rank": 1, "score": 0.9, "selected": True},
        {"strategy_id": "btc_s9", "symbol": "BTC", "rank": 2, "score": 0.1, "selected": False},
    ])

    s = build_pipeline_summary(tmp_path)

    assert s["schema_version"] == 1
    assert s["generated_at"].endswith("+00:00") or s["generated_at"].endswith("Z")
    row = s["strategies"]["eth_s5"]
    assert row == {
        "selected": True, "recommended_action": "proceed",
        "gate_overall_pass": True, "gate_fatal_fail": False,
        "oos_sharpe": 1.02, "oos_max_drawdown": -0.05, "oos_trade_count": 42,
    }
    assert s["strategies"]["btc_s9"]["gate_fatal_fail"] is True
    assert s["strategies"]["btc_s9"]["selected"] is False


def test_build_summary_tolerates_corrupt_and_partial_manifests(tmp_path):
    d = tmp_path / "broken"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, "bare", {})  # every field missing → Nones, no crash
    # No selection.json at all — selected must default to None, not crash.

    s = build_pipeline_summary(tmp_path)

    assert "broken" not in s["strategies"]
    assert s["strategies"]["bare"]["oos_sharpe"] is None
    assert s["strategies"]["bare"]["selected"] is None


def test_build_summary_tolerates_corrupt_selection_json(tmp_path):
    _write(tmp_path, "eth_s5", _manifest("proceed", False, 1.0))
    (tmp_path / "selection.json").write_text("{not json", encoding="utf-8")

    s = build_pipeline_summary(tmp_path)

    assert s["strategies"]["eth_s5"]["selected"] is None
    assert s["strategies"]["eth_s5"]["oos_sharpe"] == 1.0


def test_write_summary_creates_file(tmp_path):
    _write(tmp_path, "eth_s5", _manifest("proceed", False, 1.0))
    _write_selection(tmp_path, [
        {"strategy_id": "eth_s5", "symbol": "ETH", "rank": 1, "score": 0.9, "selected": True},
    ])
    p = write_pipeline_summary(tmp_path)
    assert p == tmp_path / "pipeline_summary.json"
    assert json.loads(p.read_text(encoding="utf-8"))["strategies"]["eth_s5"]["selected"] is True
