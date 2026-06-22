# research/tests/test_oi_metrics.py
import json
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

from lib import oi_metrics

_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio"
)


# ── parse ──────────────────────────────────────────────────────────────────


def test_parse_metrics_csv_renames_indexes_and_types():
    text = (
        _HEADER + "\n"
        "2024-06-01 00:05:00,ETHUSDT,1127415.71,4249139617.73,2.604,2.487,2.497,1.0257\n"
        "2024-06-01 00:10:00,ETHUSDT,1127939.50,4246071873.36,2.607,2.486,2.503,0.8674\n"
    )
    df = oi_metrics.parse_metrics_csv(text)

    assert list(df.columns) == [
        "oi",
        "oi_usd",
        "toptrader_ls_accounts",
        "toptrader_ls_positions",
        "global_ls_accounts",
        "taker_buysell_ratio",
    ]
    # index is UTC tz-aware, parsed from create_time
    assert df.index.tz is not None
    assert df.index[0] == pd.Timestamp("2024-06-01 00:05:00", tz="UTC")
    # values are floats, correctly mapped
    assert df["oi"].iloc[0] == pytest.approx(1127415.71)
    assert df["oi_usd"].iloc[1] == pytest.approx(4246071873.36)
    assert df["taker_buysell_ratio"].iloc[0] == pytest.approx(1.0257)
    assert df.dtypes["oi"] == "float64"


# ── aggregate 5min -> 1H ───────────────────────────────────────────────────


def test_aggregate_to_hourly_snapshot_last_flow_mean():
    """OI/ratio = last snapshot in [T,T+1h); taker flow = mean over the hour."""
    idx = pd.date_range("2024-06-01 00:00", periods=24, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "oi": range(24),  # 0..23
            "oi_usd": [v * 4000.0 for v in range(24)],
            "toptrader_ls_accounts": 2.0,
            "toptrader_ls_positions": 2.0,
            "global_ls_accounts": 2.0,
            "taker_buysell_ratio": [1.0] * 12 + [2.0] * 12,
        },
        index=idx,
    )

    out = oi_metrics.aggregate_to_hourly(df)

    assert list(out.index) == [
        pd.Timestamp("2024-06-01 00:00", tz="UTC"),
        pd.Timestamp("2024-06-01 01:00", tz="UTC"),
    ]
    # last snapshot of each hour: idx 11 (00:55) and idx 23 (01:55)
    assert list(out["oi"]) == [11.0, 23.0]
    # flow column = mean over the hour
    assert list(out["taker_buysell_ratio"]) == [1.0, 2.0]


def test_aggregate_preserves_gap_as_nan_no_ffill():
    """A missing hour stays NaN — ffill would inflate IC (measurement-layer lesson)."""
    idx = pd.DatetimeIndex(
        ["2024-06-01 00:05", "2024-06-01 02:05"], tz="UTC"
    )  # hour 01:00 absent
    df = pd.DataFrame(
        {
            "oi": [100.0, 300.0],
            "oi_usd": [1.0, 1.0],
            "toptrader_ls_accounts": [1.0, 1.0],
            "toptrader_ls_positions": [1.0, 1.0],
            "global_ls_accounts": [1.0, 1.0],
            "taker_buysell_ratio": [1.0, 1.0],
        },
        index=idx,
    )

    out = oi_metrics.aggregate_to_hourly(df)

    assert pd.Timestamp("2024-06-01 01:00", tz="UTC") in out.index
    assert pd.isna(out.loc[pd.Timestamp("2024-06-01 01:00", tz="UTC"), "oi"])


# ── load range (download + parse + concat + aggregate) ─────────────────────


def _write_day_zip(dest_dir, symbol, day, oi_val):
    rows = "\n".join(
        f"{day} 00:{m:02d}:00,{symbol},{oi_val},{oi_val * 4000},2.6,2.5,2.5,1.0"
        for m in range(5, 60, 5)
    )
    csv_text = _HEADER + "\n" + rows + "\n"
    p = dest_dir / f"{symbol}-metrics-{day.isoformat()}.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(f"{symbol}-metrics-{day.isoformat()}.csv", csv_text)
    return p


def test_load_oi_range_concats_and_preserves_gap_days_as_nan(tmp_path, monkeypatch):
    present = {date(2024, 6, 1): 100.0, date(2024, 6, 3): 300.0}

    def fake_download(symbol, day, dest_dir, verify=True):
        if day not in present:  # 2024-06-02 is a missing day (404 -> None)
            return None
        return _write_day_zip(dest_dir, symbol, day, present[day])

    monkeypatch.setattr(oi_metrics.binance_dump, "download_metrics_day", fake_download)

    out = oi_metrics.load_oi_range(
        "ETHUSDT", date(2024, 6, 1), date(2024, 6, 3), cache_dir=tmp_path
    )

    # present days carry data (00:00 bin holds 00:05..00:55 -> last)
    assert out.loc[pd.Timestamp("2024-06-01 00:00", tz="UTC"), "oi"] == pytest.approx(100.0)
    assert out.loc[pd.Timestamp("2024-06-03 00:00", tz="UTC"), "oi"] == pytest.approx(300.0)
    # gap day stays on the continuous grid but all-NaN — never forward-filled
    gap = out.loc[
        pd.Timestamp("2024-06-02 00:00", tz="UTC"):pd.Timestamp("2024-06-02 23:00", tz="UTC"),
        "oi",
    ]
    assert len(gap) == 24
    assert gap.isna().all()


# ── parquet cache round-trip ───────────────────────────────────────────────


def test_dump_and_load_oi_parquet_roundtrip(tmp_path):
    idx = pd.date_range("2024-06-01", periods=3, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {c: [1.0, 2.0, 3.0] for c in oi_metrics.OI_COLUMNS}, index=idx
    )

    path = oi_metrics.dump_oi_parquet("ETHUSDT", df, cache_dir=tmp_path)
    assert path.exists()

    back = oi_metrics.load_oi_parquet("ETHUSDT", cache_dir=tmp_path)
    # parquet does not persist the DatetimeIndex freq tag — data fidelity is the point
    pd.testing.assert_frame_equal(back, df, check_freq=False)

    meta = json.loads(path.with_suffix(".meta.json").read_text())
    assert meta["symbol"] == "ETHUSDT"
    assert meta["n_rows"] == 3
    assert meta["columns"] == list(oi_metrics.OI_COLUMNS)


def test_load_oi_parquet_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="dump_oi"):
        oi_metrics.load_oi_parquet("ETHUSDT", cache_dir=tmp_path)


# ── live L/S fetch + merge ──────────────────────────────────────────────────


def test_fetch_live_ls_ratios_builds_two_col_frame(monkeypatch):
    from lib import binance_dump

    def fake_raw(symbol, endpoint_path, period="5m", limit=500):
        base = 1718900000000
        if endpoint_path == binance_dump.GLOBAL_LS_ACCOUNT_PATH:
            return [{"longShortRatio": "1.5", "timestamp": base},
                    {"longShortRatio": "1.6", "timestamp": base + 300000}]
        return [{"longShortRatio": "2.5", "timestamp": base},
                {"longShortRatio": "2.6", "timestamp": base + 300000}]
    monkeypatch.setattr(binance_dump, "fetch_live_ls_raw", fake_raw)

    df = oi_metrics.fetch_live_ls_ratios("SOLUSDT")
    assert list(df.columns) == ["global_ls_accounts", "toptrader_ls_positions"]
    assert df["global_ls_accounts"].iloc[0] == 1.5
    assert df["toptrader_ls_positions"].iloc[0] == 2.5
    assert df.index.tz is not None  # UTC


def test_merge_live_tail_live_precedence_and_append():
    idx = pd.date_range("2026-06-20", periods=4, freq="1h", tz="UTC")
    archive = pd.DataFrame({
        "oi": [10.0, 11, 12, 13],
        "global_ls_accounts": [1.0, 1.0, 1.0, 1.0],
        "toptrader_ls_positions": [2.0, 2.0, 2.0, 2.0],
    }, index=idx)
    # Live 5-min frame spanning the last archive hour + one new hour.
    live_idx = pd.date_range("2026-06-20 03:00", periods=24, freq="5min", tz="UTC")
    live = pd.DataFrame({
        "global_ls_accounts": [9.0] * 24,
        "toptrader_ls_positions": [8.0] * 24,
    }, index=live_idx)

    out = oi_metrics.merge_live_tail(archive, live)
    # Overlap hour 03:00 overwritten by live (precedence)…
    assert out.loc[idx[3], "global_ls_accounts"] == 9.0
    # …new hour 04:00 appended…
    assert out.loc[pd.Timestamp("2026-06-20 04:00", tz="UTC"), "toptrader_ls_positions"] == 8.0
    # …oi (not an L/S col) untouched on overlap, NaN on appended row.
    assert out.loc[idx[3], "oi"] == 13.0
    assert pd.isna(out.loc[pd.Timestamp("2026-06-20 04:00", tz="UTC"), "oi"])


# ── reconcile_live_archive ─────────────────────────────────────────────────


def _hourly_ls(values_g, values_t, start="2026-06-20"):
    idx = pd.date_range(start, periods=len(values_g), freq="1h", tz="UTC")
    return pd.DataFrame({"global_ls_accounts": values_g,
                         "toptrader_ls_positions": values_t}, index=idx)


def test_reconcile_passes_when_live_matches_archive():
    n = 48
    g = np.linspace(1.0, 2.0, n)
    t = np.linspace(2.0, 3.0, n)
    archive = _hourly_ls(g, t)
    live = _hourly_ls(g * 1.01, t * 0.99)   # within 5% / high corr
    oi_metrics.reconcile_live_archive(archive, live)  # must not raise


def test_reconcile_fails_on_account_vs_position_swap():
    n = 48
    g = np.linspace(1.0, 2.0, n)
    t = np.linspace(2.0, 3.0, n)
    archive = _hourly_ls(g, t)
    # toptrader_ls_positions accidentally fed the account-ratio series (different scale/shape).
    live = _hourly_ls(g, np.linspace(0.5, 0.6, n))
    with pytest.raises(ValueError, match="reconcile"):
        oi_metrics.reconcile_live_archive(archive, live)
