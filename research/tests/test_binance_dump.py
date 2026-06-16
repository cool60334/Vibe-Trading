# research/tests/test_binance_dump.py
import hashlib
from pathlib import Path

import pytest

from lib import binance_dump


def test_build_url_monthly():
    url = binance_dump.aggtrades_url("ETHUSDT", 2025, 6)
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/aggTrades/"
        "ETHUSDT/ETHUSDT-aggTrades-2025-06.zip"
    )


def test_download_skips_when_cached(tmp_path, monkeypatch):
    target = tmp_path / "ETHUSDT-aggTrades-2025-06.zip"
    target.write_bytes(b"already-here")

    def boom(*a, **k):
        raise AssertionError("should not fetch when cached")

    monkeypatch.setattr(binance_dump, "_fetch", boom)
    out = binance_dump.download_month("ETHUSDT", 2025, 6, dest_dir=tmp_path, verify=False)
    assert out == target


def test_download_verifies_checksum(tmp_path, monkeypatch):
    payload = b"fake-zip-bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_fetch(url, dest):
        if url.endswith(".CHECKSUM"):
            dest.write_text(f"{digest}  ETHUSDT-aggTrades-2025-06.zip\n")
        else:
            dest.write_bytes(payload)

    monkeypatch.setattr(binance_dump, "_fetch", fake_fetch)
    out = binance_dump.download_month("ETHUSDT", 2025, 6, dest_dir=tmp_path, verify=True)
    assert out.read_bytes() == payload


def test_download_raises_on_checksum_mismatch(tmp_path, monkeypatch):
    def fake_fetch(url, dest):
        if url.endswith(".CHECKSUM"):
            dest.write_text("deadbeef  ETHUSDT-aggTrades-2025-06.zip\n")
        else:
            dest.write_bytes(b"corrupt")

    monkeypatch.setattr(binance_dump, "_fetch", fake_fetch)
    with pytest.raises(ValueError, match="checksum"):
        binance_dump.download_month("ETHUSDT", 2025, 6, dest_dir=tmp_path, verify=True)
