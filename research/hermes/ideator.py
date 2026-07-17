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


_IDEATION_PROMPT = """You are a quantitative researcher proposing NEW alpha factors for a \
crypto perpetual-futures research pipeline (1-hour bars).

You may use ONLY these columns. Nothing else exists.

{fields}

{deaths}
A gate kills any factor whose |Spearman| against ANY column above is >= 0.7. Everything below \
follows from that one number.

[1] FATAL SHAPES — these are rejected by arithmetic, before anyone looks at the economics:
- A MONOTONIC transform of one column: rank(x), a global z-score of x, log(x), any linear \
rescale. Spearman is a RANK correlation, so these score EXACTLY 1.0 against x. Guaranteed dead.
- A plain product A * B. This is NOT an interaction: the product inherits its VARIANCE from \
whichever parent swings harder, so it just re-expresses that parent and dies as redundant.
- A rolling z-score or a momentum/difference of the main crypto series. Those ARE the _z and \
_mom columns. Re-proposing them is proposing a duplicate.
- Price momentum. roc_10 and the cross columns already hold it.

[2] SHAPES THAT SURVIVE — and the condition each one needs. Ignore the condition and you get \
a >= 0.7 clone of A anyway:
- Conditional sign flip / state gate: use A, flipped or zeroed by B's state. B's condition \
MUST fire roughly half the time. A condition that fires 10% of the time leaves the factor \
identical to A for the other 90% — a clone.
- Residual: how far A sits from what B predicts (subtract a scaled B). B MUST explain more \
than half of A's variance. The residual's correlation to A is sqrt(1 - R^2), so at R^2 < 0.51 \
the residual is still a clone of A. Only residualise against a column A really tracks.
- Relative position: rank(A) - rank(B), each ranked over its own rolling window. Use with \
care: measured on this panel it lands at 0.44-0.67 against its parent — under the gate, but \
close enough that mismatched inputs push it over. A and B must have comparable volatility.
- Dispersion / asymmetry / duration / quantile position of a single series. These are NOT \
monotonic, and none of them are taken yet.

[3] IT MUST MAKE MONEY, NOT MERELY CORRELATE:
- The score is the SHARPE of the position the factor implies, net of trading cost — not rank \
correlation. A signal that is right about small moves and wrong about large ones has real \
rank IC and still loses money. Every extra unit of turnover is charged, so a fast-flipping \
signal must earn its churn back before it scores at all.
- State the DIRECTION explicitly: which way to trade, in which state, and why that side is \
the profitable one. If your economics say price rises, your factor must be POSITIVE there.
- Point-in-time: a value at bar t may use only bars <= t. No look-ahead.
- Write the economic reasoning FIRST, then the factor. An idea with no economic story is data \
mining and will not survive out-of-sample.

Return ONLY a fenced ```json block: a list of exactly {n} objects, each with:
  "id"          — short snake_case identifier, unique
  "description" — ENGLISH, <= 200 chars. The economic hypothesis AND how to compute it. \
This string is handed verbatim to a code-writing model, so it must be precise enough to \
implement without guessing.
  "fields"      — list of the column names the factor reads. Must all come from the list above.
{repair}"""

_REPAIR_TEMPLATE = """
Your previous response failed with:
{error}
Return ONLY the fenced ```json block this time. No prose outside it."""


def _render_fields(schema: dict) -> str:
    return "\n".join(
        f"- {col}: {e['what']}. POSITIVE MEANS: {e['positive']}. NOTE: {e['notes']}"
        for col, e in schema.items())


def _render_deaths(deaths: list) -> str:
    if not deaths:
        return ""
    lines = "\n".join(f"- {d['formula']}  [died: {d['category']}]" for d in deaths)
    return (
        "These factors were already tried on this symbol and BURIED. Do not re-propose "
        "them or trivial variants of them:\n"
        f"{lines}\n"
        "  redundant = too correlated with something that already exists\n"
        "  weak_ic = no predictive power\n"
        "  turnover = traded too often to be viable after costs\n"
        "  dsr = not significant once multiple testing was accounted for\n"
        "  forge_failed = the code never ran; the idea itself was never tested\n\n")


def build_ideation_prompt(schema: dict, death_summary: list, n_ideas: int,
                          prior_error: "str | None" = None) -> str:
    """Assemble the ideation prompt. Carries NO IC/performance numbers (spec §4.1)."""
    repair = "" if not prior_error else _REPAIR_TEMPLATE.format(error=prior_error)
    return _IDEATION_PROMPT.format(
        fields=_render_fields(schema), deaths=_render_deaths(death_summary),
        n=n_ideas, repair=repair)


def generate_ideas(llm, schema: dict, death_summary: list, n_ideas: int,
                   budget=None, max_attempts: int = 2) -> tuple:
    """One (or at most `max_attempts`) LLM call(s) -> validated ideas.

    Returns (accepted, rejected, failure_reason); failure_reason is None on
    success. `rejected` is the LAST attempt's validate_ideas fallout and is
    surfaced in the run summary: without it, the hallucinated-column rejection
    rate is invisible, and that rate is exactly what tells us whether the field
    schema is doing its job.

    Bounded retry with error feedback mirrors forge's repair loop, and is
    justified by evidence rather than caution: a missing JSON fence is this
    repo's known high-frequency LLM failure (stage0/stage2's swarm hit it
    repeatedly).

    BudgetExhausted propagates rather than being swallowed into failure_reason:
    like forge, running out of LLM calls is INFRASTRUCTURE exhaustion, not a bad
    response, and burning a retry on it would be wrong.

    `schema` must already be reconcile_schema()'d down to columns that really
    exist in the panel, so its keys ARE the legal field set for validation.
    """
    prior_error, rejected = None, []
    for _attempt in range(max_attempts):
        if budget is not None:
            budget.charge_call()                       # propagates BudgetExhausted
        prompt = build_ideation_prompt(schema, death_summary, n_ideas, prior_error)
        try:
            raw = parse_ideas(llm.complete(prompt))
        except IdeationParseError as exc:
            prior_error, rejected = f"{type(exc).__name__}: {exc}", []
            continue
        accepted, rejected = validate_ideas(raw, schema.keys())
        if accepted:
            return accepted, rejected, None
        prior_error = "every idea was rejected: " + "; ".join(
            f"{r['id']}: {r['reason']}" for r in rejected)
    return [], rejected, prior_error
