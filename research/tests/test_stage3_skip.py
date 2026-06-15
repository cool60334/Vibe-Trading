# research/tests/test_stage3_skip.py
"""stage3 missing-factor graceful skip — run from research/ (pytest tests/).

A 1H-only strategy whose factor isn't a candidate at a sub-hour interval makes
its signal engine raise KeyError. The runner exits 3 (distinct from a real
failure's 1); stage3 must classify that as a SKIP, not a FAIL, so one
interval-incompatible strategy doesn't block the whole pipeline.
"""
from pipeline.stage3_backtest import (
    BacktestRunResult,
    classify_backtest_proc,
    compute_exit_code,
)


def test_returncode_3_is_skip_not_fail():
    r = classify_backtest_proc("eth_s5_train", returncode=3, stderr="missing column")
    assert r is not None
    assert r.ok is True
    assert r.skipped_missing_factor is True


def test_returncode_1_is_fail():
    r = classify_backtest_proc("eth_s5_train", returncode=1, stderr="KeyError boom")
    assert r is not None
    assert r.ok is False
    assert "code 1" in (r.error or "")


def test_returncode_0_returns_none_for_artifact_verification():
    assert classify_backtest_proc("x", returncode=0, stderr="") is None


def test_missing_factor_skip_does_not_fail_stage():
    results = [
        BacktestRunResult(run_name="ok_run", ok=True),
        BacktestRunResult(run_name="eth_s5_train", ok=True, skipped_missing_factor=True),
    ]
    assert compute_exit_code(results) == 0
