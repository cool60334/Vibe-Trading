# research/tests/test_binance_dump.py
import hashlib
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

import pytest

from lib import binance_dump


def test_fetch_passes_socket_timeout(tmp_path, monkeypatch):
    """A stalled connection must not hang forever — urlopen needs a timeout."""
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"data"

    def fake_urlopen(url, timeout=None):
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    binance_dump._fetch("http://example/x", tmp_path / "f.bin")
    assert captured["timeout"] is not None and captured["timeout"] > 0


def test_fetch_retries_transient_errors(tmp_path, monkeypatch):
    """Transient network errors retry with backoff instead of aborting the run."""
    from urllib.error import URLError

    calls = {"n": 0}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def flaky_urlopen(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise URLError("temporary failure")
        return FakeResp()

    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(binance_dump.time, "sleep", lambda *_: None)  # no real backoff
    dest = tmp_path / "f.bin"
    binance_dump._fetch("http://example/x", dest, retries=3)
    assert calls["n"] == 3
    assert dest.read_bytes() == b"ok"


def test_fetch_does_not_retry_http_errors(tmp_path, monkeypatch):
    """A 404 must reraise immediately so download_metrics_day's soft-skip stays fast."""
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(HTTPError):
        binance_dump._fetch("http://example/x", tmp_path / "f.bin", retries=3)
    assert calls["n"] == 1  # not retried


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


# ── daily metrics (OI / long-short ratio) ──────────────────────────────────


def test_build_url_metrics_daily():
    url = binance_dump.metrics_url("ETHUSDT", date(2024, 6, 1))
    assert url == (
        "https://data.binance.vision/data/futures/um/daily/metrics/"
        "ETHUSDT/ETHUSDT-metrics-2024-06-01.zip"
    )


def test_metrics_download_skips_when_cached(tmp_path, monkeypatch):
    target = tmp_path / "ETHUSDT-metrics-2024-06-01.zip"
    target.write_bytes(b"already-here")

    def boom(*a, **k):
        raise AssertionError("should not fetch when cached")

    monkeypatch.setattr(binance_dump, "_fetch", boom)
    out = binance_dump.download_metrics_day(
        "ETHUSDT", date(2024, 6, 1), dest_dir=tmp_path, verify=False
    )
    assert out == target


def test_metrics_download_verifies_checksum(tmp_path, monkeypatch):
    payload = b"fake-metrics-zip"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_fetch(url, dest):
        if url.endswith(".CHECKSUM"):
            dest.write_text(f"{digest}  ETHUSDT-metrics-2024-06-01.zip\n")
        else:
            dest.write_bytes(payload)

    monkeypatch.setattr(binance_dump, "_fetch", fake_fetch)
    out = binance_dump.download_metrics_day(
        "ETHUSDT", date(2024, 6, 1), dest_dir=tmp_path, verify=True
    )
    assert out.read_bytes() == payload


def test_metrics_download_returns_none_on_missing_day(tmp_path, monkeypatch):
    """Binance occasionally skips a day; a 404 must be a soft gap, not a crash."""

    def fake_fetch(url, dest):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(binance_dump, "_fetch", fake_fetch)
    out = binance_dump.download_metrics_day(
        "ETHUSDT", date(2024, 6, 1), dest_dir=tmp_path, verify=True
    )
    assert out is None


# ── live L/S ratio endpoints ───────────────────────────────────────────────


def test_live_ls_url_maps_position_vs_account():
    acct = binance_dump.live_ls_url("SOLUSDT", binance_dump.GLOBAL_LS_ACCOUNT_PATH, "5m", 500)
    pos = binance_dump.live_ls_url("SOLUSDT", binance_dump.TOPTRADER_LS_POSITION_PATH, "5m", 500)
    assert "globalLongShortAccountRatio" in acct and "symbol=SOLUSDT" in acct and "period=5m" in acct
    # MUST be the POSITION variant for toptrader (not the account variant).
    assert "topLongShortPositionRatio" in pos
    assert "topLongShortAccountRatio" not in pos


def test_fetch_live_ls_raw_parses_json(monkeypatch):
    payload = [
        {"symbol": "SOLUSDT", "longShortRatio": "1.50", "timestamp": 1718900000000},
        {"symbol": "SOLUSDT", "longShortRatio": "1.40", "timestamp": 1718900300000},
    ]
    monkeypatch.setattr(binance_dump, "_fetch_json", lambda url, **kw: payload)
    rows = binance_dump.fetch_live_ls_raw("SOLUSDT", binance_dump.GLOBAL_LS_ACCOUNT_PATH)
    assert rows == payload


def test_fetch_json_passes_socket_timeout(monkeypatch):
    """Stalled connection must not hang — urlopen needs a timeout."""
    import json as _json

    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return _json.dumps([]).encode()

    def fake_urlopen(url, timeout=None):
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    binance_dump._fetch_json("http://example/x")
    assert captured["timeout"] is not None and captured["timeout"] > 0


def test_fetch_json_retries_transient_errors(monkeypatch):
    """Transient network errors retry with backoff instead of aborting."""
    import json as _json
    from urllib.error import URLError

    calls = {"n": 0}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return _json.dumps({"ok": True}).encode()

    def flaky_urlopen(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise URLError("temporary failure")
        return FakeResp()

    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(binance_dump.time, "sleep", lambda *_: None)
    result = binance_dump._fetch_json("http://example/x", retries=3)
    assert calls["n"] == 3
    assert result == {"ok": True}


def test_fetch_json_does_not_retry_http_errors(monkeypatch):
    """A 404 must reraise immediately — no retry on HTTPError."""
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(HTTPError):
        binance_dump._fetch_json("http://example/x", retries=3)
    assert calls["n"] == 1


def test_metrics_day_exists_true(monkeypatch):
    """HEAD 200 -> the day's metrics zip exists upstream."""
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        return FakeResp()
    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    assert binance_dump.metrics_day_exists("BTCUSDT", date(2022, 1, 1)) is True
    assert captured["method"] == "HEAD"  # cheap: no body download


def test_metrics_day_exists_false_on_404(monkeypatch):
    """A 404 means Binance has not published that day/symbol -> False (not an error)."""
    def fake_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    assert binance_dump.metrics_day_exists("ZZZUSDT", date(2022, 1, 1)) is False


def test_metrics_day_exists_reraises_non_404(monkeypatch):
    """A 500 is a probe failure, not 'absent' -> propagate so callers can tell them apart."""
    def fake_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 500, "Server Error", {}, None)
    monkeypatch.setattr(binance_dump.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(HTTPError):
        binance_dump.metrics_day_exists("BTCUSDT", date(2022, 1, 1))
