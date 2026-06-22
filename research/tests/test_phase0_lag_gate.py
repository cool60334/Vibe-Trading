import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from phase0_lag_gate import gate_decision  # noqa: E402


def test_gate_passes_when_lagged_sharpe_meets_threshold():
    d = gate_decision(baseline_sharpe=1.79, lagged_sharpe=1.2, threshold=1.0)
    assert d.go is True
    assert "1.2" in d.summary


def test_gate_fails_when_lag_destroys_edge():
    d = gate_decision(baseline_sharpe=1.79, lagged_sharpe=0.3, threshold=1.0)
    assert d.go is False


def test_gate_fails_on_missing_lagged_metric():
    d = gate_decision(baseline_sharpe=1.79, lagged_sharpe=None, threshold=1.0)
    assert d.go is False
