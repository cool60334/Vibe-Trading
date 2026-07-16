"""Foundry LLM crypto-native hypothesis ideation.

The missing upstream of hypothesis_queue.hypotheses_from_llm(): that adapter has
always existed, but run_foundry passed llm_raw=[], so LLM ideation was never
wired. Meanwhile the queue's first 50 slots were 100% zoo -- 456 equity/A-share
technical alphas touching only close/volume -- while the panel's crypto-native
columns (funding/basis/OI/long-short) were never touched by any hypothesis.

The architecture is already two-stage: Hypothesis.description (a plain-language
economic hypothesis) -> forge._PROMPT turns it into compute(df) code. So this
module only produces descriptions; codegen stays forge's job.
"""
from __future__ import annotations

from research.hermes.evidence_card import VERDICT_GRAVEYARD

# gatekeeper.evaluate's four reject branches, matched on their literal prefixes
# (gatekeeper.py:243-250). Order matters only for readability -- the prefixes are
# mutually exclusive.
_GATE_PREFIXES = (
    ("redundant:", "redundant"),
    ("turnover ", "turnover"),
    ("weak gross_ic ", "weak_ic"),
    ("DSR ", "dsr"),
)
# forge-side deaths: the code never ran clean, so there is no verdict about the
# IDEA -- only about the code. Told apart from gate deaths so the ideator can see
# "your idea was never actually tested" vs "your idea was tested and lost".
_FORGE_MARKERS = ("SandboxRunFailed", "forge failed", "LLM repeated identical code",
                  "LookaheadError", "UnsafeCodeError", "Contract violation")


def death_category(death_reason: "str | None") -> str:
    """Bucket a death_reason into a category. NEVER returns the numbers."""
    if not death_reason:
        return "other"
    for prefix, category in _GATE_PREFIXES:
        if death_reason.startswith(prefix):
            return category
    if any(m in death_reason for m in _FORGE_MARKERS):
        return "forge_failed"
    return "other"


def summarize_deaths(cards, limit: int = 30) -> list:
    """Buried factors as {formula, category} -- descriptions and buckets ONLY.

    Deliberately drops every metric (spec §4.1): feeding "IC 0.029 rejected" to
    the LLM invites it to bolt on a log()/ewma to shove IC past 0.03 rather than
    find new economics. That is automated p-hacking. A category tells the LLM the
    road is dead without telling it by how much.
    """
    out = []
    for c in cards:
        if c.verdict != VERDICT_GRAVEYARD:
            continue
        out.append({"formula": c.formula, "category": death_category(c.death_reason)})
        if len(out) >= limit:
            break
    return out
