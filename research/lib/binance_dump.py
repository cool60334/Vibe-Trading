"""Download + checksum-verify Binance futures aggTrades monthly archives.

Free public data dumps from data.binance.vision. Network-facing; everything
downstream (aggregation, factors) is offline and deterministic.
"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

_BASE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"


def aggtrades_url(symbol: str, year: int, month: int) -> str:
    fname = f"{symbol}-aggTrades-{year:04d}-{month:02d}.zip"
    return f"{_BASE}/{symbol}/{fname}"


def _fetch(url: str, dest: Path) -> None:
    """Download `url` to `dest`. Isolated for test monkeypatching."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (trusted host)
        dest.write_bytes(resp.read())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
        chk = dest_dir / (fname + ".CHECKSUM")
        _fetch(url + ".CHECKSUM", chk)
        expected = chk.read_text().split()[0].strip()
        actual = _sha256(target)
        if actual != expected:
            target.unlink(missing_ok=True)
            raise ValueError(
                f"checksum mismatch for {fname}: expected {expected}, got {actual}"
            )
    return target
