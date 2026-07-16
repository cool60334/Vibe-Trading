# research/tests/test_hermes_field_schema.py
"""field_schema 的載入 + 對帳。純資料，不碰 LLM。"""
from pathlib import Path

import pandas as pd
import pytest

from research.hermes.field_schema import SCHEMA_PATH, load_field_schema, reconcile_schema

_REQUIRED_KEYS = {"what", "positive", "notes"}


def test_schema_loads_and_every_entry_has_required_keys():
    schema = load_field_schema()
    assert schema, "field_schema.yaml is empty"
    for name, entry in schema.items():
        missing = _REQUIRED_KEYS - set(entry)
        assert not missing, f"{name} missing keys {missing}"
        for k in _REQUIRED_KEYS:
            assert str(entry[k]).strip(), f"{name}.{k} is blank"


def test_reconcile_splits_usable_undocumented_and_stale():
    schema = {"a": {"what": "A", "positive": "p", "notes": "n"},
              "gone": {"what": "G", "positive": "p", "notes": "n"}}
    usable, undocumented, stale = reconcile_schema(schema, ["a", "newcol"])
    assert set(usable) == {"a"}
    assert undocumented == ["newcol"]
    assert stale == ["gone"]


def test_reconcile_is_deterministic_and_sorted():
    schema = {"b": {"what": "B", "positive": "p", "notes": "n"},
              "a": {"what": "A", "positive": "p", "notes": "n"}}
    usable, undocumented, stale = reconcile_schema(schema, ["z", "a", "b", "y"])
    assert list(usable) == ["a", "b"]
    assert undocumented == ["y", "z"]
    assert stale == []


@pytest.mark.parametrize("symbol", ["eth"])
def test_schema_covers_the_real_feature_panel(symbol):
    """schema 必須跟 features_<sym>.parquet 的真實欄位完全對上。
    這條擋的是 spec §8 限制 3：stage0a 新增欄位時 schema 會腐爛。"""
    panel_path = Path("research/manifests") / f"features_{symbol}.parquet"
    if not panel_path.exists():
        pytest.skip(f"{panel_path} not present in this checkout")
    cols = list(pd.read_parquet(panel_path).columns)
    _usable, undocumented, stale = reconcile_schema(load_field_schema(), cols)
    assert not undocumented, (
        f"panel has undocumented columns {undocumented}; "
        f"add them to {SCHEMA_PATH.name} so the ideator can use them")
    assert not stale, (
        f"{SCHEMA_PATH.name} documents columns the panel no longer has: {stale}")
