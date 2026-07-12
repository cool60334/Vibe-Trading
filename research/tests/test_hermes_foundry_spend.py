import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from research.hermes.foundry_spend import (
    AlreadyRunning, default_ledger_path, record_spend, single_instance_lock, todays_spend,
)


def test_todays_spend_sums_only_today_utc(tmp_path):
    led = tmp_path / "spend.jsonl"
    led.write_text(
        json.dumps({"date": "2026-07-12", "calls": 4}) + "\n" +
        json.dumps({"date": "2026-07-12", "calls": 2}) + "\n" +
        json.dumps({"date": "2026-07-11", "calls": 9}) + "\n", encoding="utf-8")
    assert todays_spend(led, date(2026, 7, 12)) == 6      # yesterday's 9 excluded
    assert todays_spend(led, date(2026, 7, 13)) == 0      # nothing today


def test_todays_spend_skips_malformed_line(tmp_path):
    led = tmp_path / "spend.jsonl"
    led.write_text('{"date": "2026-07-12", "calls": 3}\nNOT JSON\n', encoding="utf-8")
    assert todays_spend(led, date(2026, 7, 12)) == 3      # corrupt line skipped, no crash


def test_todays_spend_missing_file_is_zero(tmp_path):
    assert todays_spend(tmp_path / "nope.jsonl", date(2026, 7, 12)) == 0


def test_record_spend_appends_with_pinned_day(tmp_path):
    led = tmp_path / "spend.jsonl"
    record_spend(led, day=date(2026, 7, 12), calls=6, tokens=2562)
    rec = json.loads(led.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["date"] == "2026-07-12"                    # the PASSED day, not now
    assert rec["calls"] == 6 and rec["tokens"] == 2562


def test_record_spend_raises_on_write_failure(tmp_path, monkeypatch):
    # a directory where the ledger path is unwritable -> OSError propagates
    led = tmp_path / "spend.jsonl"
    def boom(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(Path, "open", boom)
    with pytest.raises(OSError):
        record_spend(led, day=date(2026, 7, 12), calls=1, tokens=1)


def test_lock_blocks_a_second_instance(tmp_path):
    with single_instance_lock(tmp_path):
        with pytest.raises(AlreadyRunning):
            with single_instance_lock(tmp_path):
                pass


def test_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "foundry.lock"
    lock.mkdir()
    old = time.time() - 7 * 3600            # 7h old, past the 6h default
    os.utime(lock, (old, old))
    with single_instance_lock(tmp_path, stale_hours=6):   # takes over, no raise
        assert lock.exists()


def test_lock_released_on_exit(tmp_path):
    with single_instance_lock(tmp_path):
        pass
    assert not (tmp_path / "foundry.lock").exists()


def test_default_ledger_path(tmp_path):
    assert default_ledger_path(tmp_path) == tmp_path / "foundry_spend.jsonl"
