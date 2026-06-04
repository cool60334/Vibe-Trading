import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from trader.freshness import factor_index_end, is_stale, _symbol_short


def _utc(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def test_symbol_short_handles_dash_slash_and_plain():
    assert _symbol_short("ETH-USDT-SWAP") == "eth"
    assert _symbol_short("ETH/USDT:USDT") == "eth"
    assert _symbol_short("eth") == "eth"


def test_factor_index_end_reads_meta(tmp_path: Path):
    (tmp_path / "factor_values_eth.meta.json").write_text(
        json.dumps({"index_end": "2026-06-01T23:00:00+00:00"}), encoding="utf-8"
    )
    assert factor_index_end(tmp_path, "ETH-USDT-SWAP") == _utc(2026, 6, 1, 23)


def test_factor_index_end_meta_naive_timestamp_assumed_utc(tmp_path: Path):
    (tmp_path / "factor_values_eth.meta.json").write_text(
        json.dumps({"index_end": "2026-06-01T23:00:00"}), encoding="utf-8"
    )
    assert factor_index_end(tmp_path, "eth") == _utc(2026, 6, 1, 23)


def test_factor_index_end_falls_back_to_parquet_when_no_meta(tmp_path: Path):
    idx = pd.to_datetime(["2026-05-30", "2026-06-02"], utc=True)
    pd.DataFrame({"funding_z": [0.1, 0.2]}, index=idx).to_parquet(
        tmp_path / "factor_values_eth.parquet", engine="pyarrow"
    )
    assert factor_index_end(tmp_path, "eth") == _utc(2026, 6, 2)


def test_factor_index_end_none_when_nothing_present(tmp_path: Path):
    assert factor_index_end(tmp_path, "eth") is None


def test_is_stale_fresh_within_window():
    now = _utc(2026, 6, 4)
    assert is_stale(_utc(2026, 6, 3), now, timedelta(days=2)) is False


def test_is_stale_old_beyond_window():
    now = _utc(2026, 6, 4)
    assert is_stale(_utc(2026, 6, 1), now, timedelta(days=2)) is True


def test_is_stale_exact_boundary_is_not_stale():
    now = _utc(2026, 6, 4)
    # exactly 2 days old → not stale (strictly greater triggers)
    assert is_stale(_utc(2026, 6, 2), now, timedelta(days=2)) is False


def test_is_stale_none_index_is_stale():
    assert is_stale(None, _utc(2026, 6, 4), timedelta(days=2)) is True


def test_is_stale_naive_now_treated_as_utc():
    index_end = _utc(2026, 6, 3)
    naive_now = datetime(2026, 6, 4)  # no tzinfo
    assert is_stale(index_end, naive_now, timedelta(days=2)) is False
