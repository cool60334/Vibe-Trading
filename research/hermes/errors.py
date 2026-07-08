"""Shared base for all Talos Hermes safety-rail violations.

Lets a Phase 1 orchestrator catch any guard-rail trip (lookahead leak, OOS
boundary breach, production-path write, unsafe LLM code, sandbox failure)
uniformly via `except HermesGuardError`, while each concrete exception keeps
its original, more specific base for existing isinstance/except compatibility.
"""
from __future__ import annotations


class HermesGuardError(Exception):
    """Common base for all Hermes Phase 0 safety-rail exceptions."""
