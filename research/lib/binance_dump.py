"""Download + checksum-verify Binance futures aggTrades monthly archives.

Free public data dumps from data.binance.vision. Network-facing; everything
downstream (aggregation, factors) is offline and deterministic.
"""
from __future__ import annotations

import hashlib
import time
import urllib.request
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError

_BASE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
_METRICS_BASE = "https://data.binance.vision/data/futures/um/daily/metrics"

# Per-request socket timeout (seconds). Without it urlopen blocks forever on a
# stalled connection — a 5400-file backfill then hangs mid-symbol with no error.
_FETCH_TIMEOUT = 30


def aggtrades_url(symbol: str, year: int, month: int) -> str:
    fname = f"{symbol}-aggTrades-{year:04d}-{month:02d}.zip"
    return f"{_BASE}/{symbol}/{fname}"


def _fetch(
    url: str, dest: Path, timeout: float = _FETCH_TIMEOUT, retries: int = 3
) -> None:
    """Download `url` to `dest`, retrying transient network errors with backoff.

    A 404 (HTTPError) reraises immediately so download_metrics_day can soft-skip
    missing days; timeouts / connection drops retry up to `retries` times.
    Isolated for test monkeypatching.
    """
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                dest.write_bytes(resp.read())
            return
        except HTTPError:
            raise  # 404 etc. — caller decides; never retry
        except (URLError, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_checksum(url: str, target: Path, dest_dir: Path, fname: str) -> None:
    """Fetch the sidecar .CHECKSUM and raise ValueError on sha256 mismatch."""
    chk = dest_dir / (fname + ".CHECKSUM")
    _fetch(url + ".CHECKSUM", chk)
    expected = chk.read_text().split()[0].strip()
    actual = _sha256(target)
    if actual != expected:
        target.unlink(missing_ok=True)
        raise ValueError(
            f"checksum mismatch for {fname}: expected {expected}, got {actual}"
        )


def download_month(
    symbol: str, year: int, month: int, dest_dir: Path, verify: bool = True
) -> Path:
    """Download one month's aggTrades zip into `dest_dir` (idempotent).

    Returns the cached zip path. Raises ValueError on checksum mismatch.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{symbol}-aggTrades-{year:04d}-{month:02d}.zip"
    target = dest_dir / fname
    chk_sentinel = dest_dir / (fname + ".CHECKSUM")
    if target.exists() and (not verify or chk_sentinel.exists()):
        return target

    url = aggtrades_url(symbol, year, month)
    _fetch(url, target)

    if verify:
        _verify_checksum(url, target, dest_dir, fname)
    return target


def metrics_url(symbol: str, day: date) -> str:
    fname = f"{symbol}-metrics-{day.isoformat()}.zip"
    return f"{_METRICS_BASE}/{symbol}/{fname}"


def download_metrics_day(
    symbol: str, day: date, dest_dir: Path, verify: bool = True
) -> Path | None:
    """Download one day's futures *metrics* zip (OI + long/short ratios).

    Mirrors :func:`download_month` (idempotent + checksum), but a missing day
    is a *soft gap*: Binance occasionally skips a date, so a 404 returns ``None``
    instead of raising — the batch loop logs the gap and moves on.

    Returns the cached zip path, ``None`` if the day is absent upstream.
    Raises ValueError on checksum mismatch.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{symbol}-metrics-{day.isoformat()}.zip"
    target = dest_dir / fname
    chk_sentinel = dest_dir / (fname + ".CHECKSUM")
    if target.exists() and (not verify or chk_sentinel.exists()):
        return target

    url = metrics_url(symbol, day)
    try:
        _fetch(url, target)
    except HTTPError as exc:
        if exc.code == 404:
            target.unlink(missing_ok=True)
            return None
        raise

    if verify:
        _verify_checksum(url, target, dest_dir, fname)
    return target
