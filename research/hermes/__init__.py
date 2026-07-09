"""Talos Factor Foundry infrastructure (Phase 0 hardening)."""
from research.hermes.errors import HermesGuardError
from research.hermes.pit import assert_no_lookahead, LookaheadError
from research.hermes.split import foundry_split, OOSLeakError
from research.hermes.candidate_store import write_candidate, ProductionWriteError
from research.hermes.sandbox_ast import check_source, UnsafeCodeError
from research.hermes.sandbox import DockerSandbox, SandboxExecutor, SandboxError, is_docker_available
from research.hermes.evidence_card import (
    EvidenceCard, CardValidationError, VERDICT_CANDIDATE, VERDICT_GRAVEYARD,
)
from research.hermes.evidence_store import upsert_card, load_cards
from research.hermes.gatekeeper import evaluate, GateConfig, GatekeeperResult
from research.hermes.hypothesis import Hypothesis
from research.hermes.hypothesis_queue import build_queue
from research.hermes.forge import forge, ForgeResult, LLMCoder

# NOTE: research.hermes.promote is deliberately NOT imported/exported here.
# promote_candidate is the only Foundry path that writes production features;
# keeping it out of package namespace means `import research.hermes` can
# never accidentally surface it. Use `python -m research.hermes.promote` or
# an explicit `from research.hermes.promote import promote_candidate`.

__all__ = [
    "HermesGuardError",
    "assert_no_lookahead", "LookaheadError",
    "foundry_split", "OOSLeakError",
    "write_candidate", "ProductionWriteError",
    "check_source", "UnsafeCodeError",
    "DockerSandbox", "SandboxExecutor", "SandboxError", "is_docker_available",
    "EvidenceCard", "CardValidationError", "VERDICT_CANDIDATE", "VERDICT_GRAVEYARD",
    "upsert_card", "load_cards",
    "evaluate", "GateConfig", "GatekeeperResult",
    "Hypothesis",
    "build_queue",
    "forge", "ForgeResult", "LLMCoder",
]
