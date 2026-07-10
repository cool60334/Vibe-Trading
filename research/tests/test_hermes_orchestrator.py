import numpy as np
import pandas as pd
import pytest
from research.hermes.orchestrator import make_run_sandbox


class _FakeSandbox:
    """Stands in for DockerSandbox: 'runs' by writing a candidate parquet."""
    def __init__(self, fn): self.fn = fn
    def run(self, source, input_parquet, output_dir):
        panel = pd.read_parquet(input_parquet)
        out = pd.DataFrame({"candidate": self.fn(panel)})
        from pathlib import Path
        p = Path(output_dir) / "candidate.parquet"; out.to_parquet(p)
        return str(p)


def test_run_sandbox_roundtrips_panel_to_series(tmp_path):
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    sb = _FakeSandbox(lambda p: p["close"].pct_change(3))
    run = make_run_sandbox(sb, scratch_dir=tmp_path)
    s = run("def compute(df): ...", panel)
    assert isinstance(s, pd.Series)
    assert s.index.equals(panel.index)               # index restored from panel
    pd.testing.assert_series_equal(s, panel["close"].pct_change(3), check_names=False)


def test_run_sandbox_raises_on_length_mismatch(tmp_path):
    idx = pd.date_range("2024-01-01", periods=50, freq="1h")
    panel = pd.DataFrame({"close": np.arange(50.0)}, index=idx)
    # sandboxed compute() returns a shorter series than the input panel
    sb = _FakeSandbox(lambda p: p["close"].iloc[:-5].reset_index(drop=True))
    run = make_run_sandbox(sb, scratch_dir=tmp_path)
    with pytest.raises(ValueError, match="sandbox output length"):
        run("def compute(df): ...", panel)
