"""Tests for KillSwitch peak persistence — drawdown survives a restart.

Without persistence the peak re-baselines to current equity on every restart,
so a real drawdown reads as zero and the kill switch never fires.
"""

import pytest

from trader.killswitch import KillSwitch


def test_fresh_state_path_peak_is_initial(tmp_path):
    ks = KillSwitch(100.0, state_path=tmp_path / "ks.json")
    assert ks.peak_equity == 100.0


def test_restart_restores_peak_not_current(tmp_path):
    sp = tmp_path / "ks.json"
    ks1 = KillSwitch(100.0, state_path=sp)
    ks1.check(150.0)  # new high → peak 150 persisted

    ks2 = KillSwitch(120.0, state_path=sp)  # restart while equity dipped to 120
    assert ks2.peak_equity == 150.0
    assert ks2.current_drawdown(120.0) == pytest.approx((150.0 - 120.0) / 150.0)


def test_restored_peak_triggers_terminate_after_restart(tmp_path):
    sp = tmp_path / "ks.json"
    KillSwitch(100.0, state_path=sp).check(150.0)  # persist peak 150

    ks = KillSwitch(120.0, pause_dd=0.05, terminate_dd=0.07, state_path=sp)
    # 138 vs restored peak 150 → 8% DD → terminate (vs "ok" if peak had reset).
    decision, _ = ks.check(138.0)
    assert decision == "terminate"


def test_no_state_path_keeps_original_behavior(tmp_path):
    ks = KillSwitch(100.0)
    ks.check(150.0)
    assert ks.peak_equity == 150.0
    decision, _ = ks.check(100.0)  # 33% DD
    assert decision == "terminate"
