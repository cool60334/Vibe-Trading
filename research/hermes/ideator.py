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

import json
import re

from research.hermes.evidence_card import VERDICT_GRAVEYARD
from research.hermes.errors import HermesGuardError

# mirrors forge._FENCE but also accepts ```json
_FENCE = re.compile(r"```(?:json|python|py)?\s*(.*?)```", re.DOTALL)

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


class IdeationParseError(HermesGuardError, ValueError):
    """The ideator's response was not usable JSON."""


def _first_json_value(text: str):
    """Find the first balanced [...] or {...} that decodes to a real payload.

    raw_decode from each bracket, rather than a regex: an idea's description
    can legitimately contain brackets, and a greedy/non-greedy regex mis-cuts on
    those. raw_decode stops exactly at the end of the first complete value, so
    trailing prose after the JSON is simply ignored.

    A syntactically-valid but EMPTY `[]`/`{}` does not short-circuit the scan.
    LLMs routinely precede the real payload with markdown checklists like
    "- [ ] some idea" -- the `[ ]` there is a perfectly valid empty JSON array,
    and accepting it on sight means the real array later in the text is never
    reached, `parse_ideas` happily returns `[]`, and no IdeationParseError ever
    fires (exactly the historical llm_raw=[] bug this module exists to catch).
    So an empty match is remembered as a last-resort fallback but the scan keeps
    going for a non-trivial list/dict first.
    """
    fallback = None
    have_fallback = False
    for i, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            # strict=False: LLMs emit literal tabs/newlines inside long Chinese
            # strings, which strict JSON rejects outright (existing repo lesson).
            value, _end = json.JSONDecoder(strict=False).raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if value == [] or value == {}:
            if not have_fallback:
                fallback = value
                have_fallback = True
            continue
        return value
    if have_fallback:
        return fallback
    raise IdeationParseError(f"no decodable JSON value found in response: {text[:200]!r}")


def parse_ideas(response: str) -> list:
    """LLM response -> list of raw idea dicts. Raises IdeationParseError."""
    m = _FENCE.search(response)
    body = m.group(1) if m else response
    value = _first_json_value(body)
    if isinstance(value, dict):
        # tolerate {"ideas": [...]} — a very common LLM shape
        for key in ("ideas", "hypotheses", "factors"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            raise IdeationParseError(
                f"expected a JSON list of ideas, got an object with keys {sorted(value)}")
    if not isinstance(value, list):
        raise IdeationParseError(f"expected a JSON list of ideas, got {type(value).__name__}")
    if not all(isinstance(x, dict) for x in value):
        raise IdeationParseError("every idea must be a JSON object")
    return value


def validate_ideas(ideas: list, panel_columns) -> tuple:
    """Deterministic pre-filter. Returns (accepted, rejected[{id, reason}]).

    ONE substantive rule: every declared field must exist in the panel. A
    hallucinated column (the LLM inventing `liquidation_z`) would otherwise cost
    three forge calls before dying on a KeyError inside the sandbox. Killing it
    here is purely about not paying for a certain failure.

    There is deliberately NO "must span >= 2 columns" rule. That rule was
    proposed on the premise that single-column transforms are always killed by
    the 0.7 Spearman dedup gate -- which is only true for MONOTONIC transforms
    (rank/global-zscore give Spearman exactly 1.0). Measured on real eth pre-oos
    data, rolling_std(funding_rate_raw, 168) scores max |Spearman| 0.617 against
    all 31 panel columns and passes the gate. Banning single-column ideas would
    kill that whole class -- funding volatility is not in the panel and is
    economically meaningful -- for a reason that does not exist.

    Note this is an ADVISORY filter, not a guarantee: `fields` is what the LLM
    DECLARED, while the code is written later by forge. A declaration of two
    columns does not bind the code to use two. The real backstop is the gate.
    """
    cols = set(panel_columns)
    accepted, rejected, seen = [], [], set()
    for i, idea in enumerate(ideas):
        fid = idea.get("id")
        if not fid or not str(fid).strip():
            rejected.append({"id": f"<index {i}>", "reason": "missing 'id'"})
            continue
        fid = str(fid)
        if not str(idea.get("description", "")).strip():
            rejected.append({"id": fid, "reason": "missing 'description'"})
            continue
        if fid in seen:
            rejected.append({"id": fid, "reason": "duplicate id"})
            continue
        fields = idea.get("fields")
        if not isinstance(fields, list):
            rejected.append({"id": fid, "reason": "'fields' must be a list of panel columns"})
            continue
        if not fields:
            rejected.append({"id": fid, "reason": "'fields' is empty"})
            continue
        unknown = sorted({str(f) for f in fields} - cols)
        if unknown:
            rejected.append({"id": fid, "reason": f"unknown panel columns: {unknown}"})
            continue
        seen.add(fid)
        accepted.append(idea)
    return accepted, rejected
