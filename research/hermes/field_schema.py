# research/hermes/field_schema.py
"""Panel 欄位的經濟定義表：Foundry ideator 的燃料。

zoo 那 456 個股票因子只碰 close/volume，從沒碰過 panel 的 crypto 原生欄位。
ideator 要出 crypto 原生假設，就必須知道 funding_z / basis_rel / oi_z 這些欄位
的『經濟含意』—— 光給欄名，LLM 只能瞎猜命名玩排列組合，那就退化回無腦 data
mining。

自動從 docstring 抽取這條路已驗證不可行：derived_factors.py 的
basis_factors / funding_factors / oi_factors（產出 crypto 核心欄的三個函式）
docstring 全是 None。所以這張表是手寫的資料檔。
"""
from __future__ import annotations

from pathlib import Path

import yaml

SCHEMA_PATH = Path(__file__).with_name("field_schema.yaml")


def load_field_schema(path: "str | Path | None" = None) -> dict:
    """Load the hand-written field schema. Returns {column: {what, positive, notes}}."""
    p = Path(path) if path else SCHEMA_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p} must contain a mapping of column -> definition")
    return data


def reconcile_schema(schema: dict, panel_columns) -> tuple:
    """Split the schema against the panel's ACTUAL columns.

    Returns (usable, undocumented, stale):
      usable       — {column: definition} for columns present in BOTH. Only these
                     are ever offered to the ideator.
      undocumented — panel columns with no definition (sorted).
      stale        — documented columns the panel no longer has (sorted).

    Callers differ deliberately (spec §6): the TEST asserts undocumented/stale are
    both empty (a rotted schema is a repo-consistency bug), but the RUNTIME only
    intersects and warns — stage0a adding a column must not crash that night's
    cron, it just means the column isn't offered to the LLM until someone
    documents it.
    """
    cols = set(panel_columns)
    usable = {k: schema[k] for k in sorted(cols & set(schema))}
    undocumented = sorted(cols - set(schema))
    stale = sorted(set(schema) - cols)
    return usable, undocumented, stale
