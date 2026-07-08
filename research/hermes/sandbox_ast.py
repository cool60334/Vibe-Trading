"""Layer-0 static gate: reject dangerous LLM-generated ETL before execution.

Allowlist imports; ban filesystem/network/eval primitives; ban backward-fill
(bfill/backfill/fillna(method='bfill')) which silently pulls future values into
the present. Defence-in-depth on top of the Docker sandbox (layer-1) and the
PIT perturbation harness.
"""
from __future__ import annotations

import ast

from research.hermes.errors import HermesGuardError

ALLOWED_IMPORTS = {"pandas", "numpy", "scipy", "ta", "math", "statistics"}
BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "getattr", "setattr"}
BANNED_ATTRS = {"bfill", "backfill"}


class UnsafeCodeError(HermesGuardError, ValueError):
    """Raised when source violates the allowlist / bans."""


def check_source(src: str) -> None:
    """Return None if the source is safe; raise UnsafeCodeError otherwise."""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise UnsafeCodeError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise UnsafeCodeError(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise UnsafeCodeError(f"import not allowed: from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BANNED_CALLS:
                raise UnsafeCodeError(f"banned call: {fn.id}")
            if isinstance(fn, ast.Attribute) and fn.attr in BANNED_CALLS:
                raise UnsafeCodeError(f"banned call: .{fn.attr}()")
            if isinstance(fn, ast.Attribute) and fn.attr in BANNED_ATTRS:
                raise UnsafeCodeError(f"banned method: .{fn.attr}()")
            # fillna(method='bfill'/'backfill')
            if isinstance(fn, ast.Attribute) and fn.attr == "fillna":
                for kw in node.keywords:
                    if (kw.arg == "method" and isinstance(kw.value, ast.Constant)
                            and kw.value.value in ("bfill", "backfill")):
                        raise UnsafeCodeError("banned: fillna(method='bfill')")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            raise UnsafeCodeError(f"banned attribute: .{node.attr}")
