import numpy as np
import pandas as pd

from research.lib.inducted_factors import inducted_dir, inducted_names, compute_inducted


def _seed(tmp_path, sym, fid, body):
    d = inducted_dir(sym, root=tmp_path); d.mkdir(parents=True, exist_ok=True)
    (d / f"{fid}.py").write_text(body, encoding="utf-8")
    (d / f"{fid}.meta.json").write_text("{}", encoding="utf-8")


def _panel(n=10):
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.arange(float(n))}, index=idx)


def test_compute_inducted_imports_and_computes(tmp_path):
    _seed(tmp_path, "eth", "foundry_x", "def compute(df):\n    return df['close'] * 2\n")
    out = compute_inducted(_panel(), "eth", root=tmp_path)
    assert list(out) == ["foundry_x"]
    assert (out["foundry_x"].to_numpy() == np.arange(10.0) * 2).all()


def test_compute_inducted_soft_fails_a_raising_factor(tmp_path):
    _seed(tmp_path, "eth", "foundry_ok", "def compute(df):\n    return df['close']\n")
    _seed(tmp_path, "eth", "foundry_bad", "def compute(df):\n    return df['missing_col']\n")
    out = compute_inducted(_panel(), "eth", root=tmp_path)
    assert "foundry_ok" in out and "foundry_bad" not in out       # bad one dropped, good one survived


def test_compute_inducted_honours_kill_switch(tmp_path, monkeypatch):
    _seed(tmp_path, "eth", "foundry_x", "def compute(df):\n    return df['close']\n")
    bl = tmp_path / "bl.txt"; bl.write_text("foundry_x\n", encoding="utf-8")
    monkeypatch.setenv("INDUCTED_BLACKLIST_FILE", str(bl))
    assert compute_inducted(_panel(), "eth", root=tmp_path) == {}   # blacklisted -> skipped


def test_same_named_helpers_dont_clobber_across_factors(tmp_path):
    _seed(tmp_path, "eth", "foundry_a",
          "def helper(df):\n    return df['close']\ndef compute(df):\n    return helper(df) + 1\n")
    _seed(tmp_path, "eth", "foundry_b",
          "def helper(df):\n    return df['close']\ndef compute(df):\n    return helper(df) + 100\n")
    out = compute_inducted(_panel(), "eth", root=tmp_path)
    assert (out["foundry_a"].to_numpy() == np.arange(10.0) + 1).all()
    assert (out["foundry_b"].to_numpy() == np.arange(10.0) + 100).all()


def test_inducted_names_needs_both_py_and_meta(tmp_path):
    d = inducted_dir("eth", root=tmp_path)
    d.mkdir(parents=True)
    (d / "foundry_a.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")
    (d / "foundry_a.meta.json").write_text("{}", encoding="utf-8")
    (d / "foundry_b.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")  # no meta

    assert inducted_names("eth", root=tmp_path) == {"foundry_a"}


def test_inducted_names_empty_when_dir_absent(tmp_path):
    assert inducted_names("eth", root=tmp_path) == set()
