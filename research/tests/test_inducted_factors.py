from research.lib.inducted_factors import inducted_dir, inducted_names


def test_inducted_names_needs_both_py_and_meta(tmp_path):
    d = inducted_dir("eth", root=tmp_path)
    d.mkdir(parents=True)
    (d / "foundry_a.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")
    (d / "foundry_a.meta.json").write_text("{}", encoding="utf-8")
    (d / "foundry_b.py").write_text("def compute(df):\n    return df['close']\n", encoding="utf-8")  # no meta

    assert inducted_names("eth", root=tmp_path) == {"foundry_a"}


def test_inducted_names_empty_when_dir_absent(tmp_path):
    assert inducted_names("eth", root=tmp_path) == set()
