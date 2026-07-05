"""Atomic writes must yield group/other-readable files.

tempfile.mkstemp creates 0600 regardless of umask and os.replace preserves
it — read-only observers (fable-ro ACL) lose access to every freshly
rewritten parquet/meta. The helpers must chmod 0o644 BEFORE the replace.
"""
import os
import stat
import sys
from pathlib import Path

import pandas as pd
import pytest

_RESEARCH = Path(__file__).resolve().parents[1]
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))

from lib.factor_io import _atomic_to_parquet, _atomic_write_text  # noqa: E402


def test_atomic_text_chmods_before_replace(tmp_path, monkeypatch):
    calls = []
    real_chmod, real_replace = os.chmod, os.replace
    monkeypatch.setattr(os, "chmod", lambda p, m: (calls.append(("chmod", m)), real_chmod(p, m)))
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(("replace", None)), real_replace(a, b)))

    _atomic_write_text(tmp_path / "x.json", "{}")

    ops = [c[0] for c in calls]
    assert "chmod" in ops, "no chmod call — file stays mkstemp-0600"
    assert ops.index("chmod") < ops.index("replace"), "chmod must precede replace"
    assert ("chmod", 0o644) in calls


def test_atomic_parquet_chmods_before_replace(tmp_path, monkeypatch):
    calls = []
    real_chmod, real_replace = os.chmod, os.replace
    monkeypatch.setattr(os, "chmod", lambda p, m: (calls.append(("chmod", m)), real_chmod(p, m)))
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(("replace", None)), real_replace(a, b)))

    df = pd.DataFrame({"a": [1.0, 2.0]},
                      index=pd.date_range("2026-01-01", periods=2, freq="h"))
    _atomic_to_parquet(df, tmp_path / "x.parquet")

    ops = [c[0] for c in calls]
    assert ops.index("chmod") < ops.index("replace")
    assert ("chmod", 0o644) in calls


@pytest.mark.skipif(os.name != "posix", reason="mode bits only meaningful on POSIX")
def test_atomic_text_final_mode_is_group_readable(tmp_path):
    _atomic_write_text(tmp_path / "y.json", "{}")
    mode = stat.S_IMODE(os.stat(tmp_path / "y.json").st_mode)
    assert mode & stat.S_IRGRP and mode & stat.S_IROTH
