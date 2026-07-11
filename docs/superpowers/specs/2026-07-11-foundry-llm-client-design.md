# Talos Foundry — OpenRouter LLM Client + Batch Cost Ceiling

**Date:** 2026-07-11
**Status:** design approved, pending implementation
**Scope:** implement `build_llm("openrouter")` (the first-run spec left it a
`NotImplementedError`) as a thin adapter over the agent's existing OpenRouter
client, add a batch-level cost ceiling across reconciled jobs, and gate a small
real paid run behind an explicit flag. This is the follow-up to
`docs/superpowers/specs/2026-07-10-foundry-first-run-design.md`.

---

## Problem

The Foundry runs end-to-end at zero cost with a scripted fake LLM, but the real
production run needs a real LLM. The first-run spec deliberately deferred three
things, now in scope:

- **B1 — no real LLM client.** `build_llm("openrouter")` raises
  `NotImplementedError`. `research/` has no LLM client at all — only the
  `LLMCoder` protocol (`.complete(prompt: str) -> str`).
- **B2 — no batch-level cost ceiling.** `ForgeBudget` counts `.complete()` calls
  per job; `run_foundry` builds a fresh one per job. `reconcile` over N jobs can
  spend up to `N × max_llm_calls` with nothing stopping it.
- **B3 — nothing stops an accidental paid run.** The `run` subcommand would spend
  money the moment it is invoked with a real client.

There is a battle-tested asset to reuse: `agent/src/providers/llm.py`'s
`build_llm(*, model_name, callbacks) -> ChatOpenAI` — env-driven OpenRouter
(`LANGCHAIN_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, `LANGCHAIN_MODEL_NAME`),
with reasoning handling, retries, timeout, three-tier `.env` search, and
credential redaction. It returns a LangChain `ChatOpenAI` whose interface is
`.invoke(messages) -> AIMessage`, not `.complete(prompt) -> str`, so an adapter
is needed regardless.

---

## Non-goals

- USD cost accounting (needs a per-model pricing table; OpenRouter has hundreds
  of models with drifting prices). Report calls + tokens only.
- Token-based budgeting (`ForgeBudget` counts calls; keep that unit for now).
- A shared LLM-client module imported by both `agent/` and `research/` (would
  require editing upstream `agent/`, violating "research changes don't go
  upstream"). Keep the coupling one-directional: `research/` imports `agent/`.
- Any automated test that hits the real OpenRouter API (tests never spend money).
- Parallel/concurrent reconcile (the batch budget assumes sequential).

---

## Approved decisions

- **D1** — a thin adapter wraps the agent's `build_llm`, with a fail-loud fuse if
  the import or signature breaks (the one-directional coupling's insurance).
- **D2** — batch cost ceiling in **call-count** units (consistent with
  `ForgeBudget`), enforced at the reconcile layer.
- **D3** — the paid run is gated by an explicit `--i-will-spend-real-money` flag
  plus a tiny default budget; the flag's absence refuses to run rather than
  silently falling back to the test fake.

---

## Adversarial review (agy) — dispositions

agy reviewed the design and raised twelve points; each verified against the code
before accepting. Four materially improved the design:

- **Accepted (improves the design) — true hard cap.** agy: checking only between
  jobs lets one job overshoot by its full per-job budget (accumulated 5, batch 6,
  next job still carries per_job=5 → total 10). The first fix was a per-job
  `min(per_job, batch_remaining)`; the **second review then superseded it** with a
  single shared `ForgeBudget` (see below), which is strictly better — it also
  survives a job that fails mid-sweep. The final design is the shared counter, not
  the `min()` formula.
- **Accepted — `max_tokens` cap.** Calls ≠ cost: a single runaway 8k-token
  completion is expensive even within 6 calls. The agent's `build_llm` sets no
  `max_tokens`; the adapter binds one.
- **Accepted — model pinning.** `build_llm` is driven by `LANGCHAIN_MODEL_NAME`;
  a stray `.env` could point it at an expensive model. The `run` subcommand
  requires an explicit `--model` and passes it to `build_llm(model_name=...)`,
  never trusting the env for a paid run.
- **Accepted — `max_retries` bypasses the counter.** A retry re-sends a billed
  request but `ForgeBudget` counts the wrapping `.complete()` once. The paid run
  sets `MAX_RETRIES=1`, and the call-count is documented as a floor, not exact.

Smaller accepted: extract `type=="text"` blocks rather than naive `str()` on list
content; wrap auth/quota exceptions as a clean error; `summary.get("llm_calls_used", 0)`.

Verified as already-handled (no action):

- **reasoning pollution** — the agent's `ChatOpenAIWithReasoning` routes reasoning
  into `additional_kwargs["reasoning_content"]` ([llm.py:107]), so `.content` is
  already clean. A light defensive strip is kept as belt-and-suspenders.
- **logging hijack** — `agent/src/providers/llm.py` uses `getLogger(__name__)`, no
  `basicConfig` at import. Importing it does not hijack root logging.
- **`.env` pollution** — the agent loads `.env` with `load_dotenv(override=False)`
  ([llm.py:420]) = setdefault; it does not clobber existing research env.
- **version fragility** — the fail-loud signature fuse covers the main risk; a
  langchain version pin is over-engineering for the first run.

### Second review (agy, folder mode against the code) — dispositions

A folder-mode pass cross-checked the written spec against the real code and found
two genuine holes plus two test-realism gaps. All verified before accepting:

- **Accepted (severe design bug) — the batch cap leaked on job failure.** The
  first draft computed `remaining = N - sum(summary.llm_calls_used)` over
  returned summaries. But `run_foundry_job` re-raises on any exception
  ([orchestrator.py:411]), so `summaries.append(...)` never runs for a failed job
  and its spent calls vanish from the running total — the next job is handed too
  much budget and the batch cap is breached. Fix (N2, rewritten): thread a single
  **shared `ForgeBudget`** through, charged before each call
  ([forge.py:167] charges *before* `complete()` at :169), so the counter reflects
  real spend even when a job then raises. Replaces the sum-of-summaries approach.
- **Accepted — `LLMUnavailable` did not actually abort.** `reconcile`'s
  `except Exception: continue` ([foundry_runner.py:105]) swallows everything that
  is not a `SandboxError`. `LLMUnavailable(HermesGuardError)` is not a
  `SandboxError`, so it would be swallowed, not aborted. Fix (§3): an explicit
  `except LLMUnavailable: raise` **before** the generic catch.
- **Accepted — test doubles must return an `AIMessage`, not a bare value.**
  `ChatOpenAI.invoke()` always returns an `AIMessage` whose text is in `.content`.
  A fake returning a raw `str`/`dict` would hide an `AttributeError` the real
  adapter hits. Fix (§4): the fake's `invoke` returns an object with `.content`.
- **Accepted — the adapter must catch specific provider exceptions.** Catching a
  bare `Exception` and relabelling it `LLMUnavailable` would mask a genuine code
  bug (e.g. `TypeError`) as a quota error. Fix (§1/§4): catch the concrete
  auth/quota/connection exception types only.
- **Accepted (minor) — `budget or Budget()` base.** `reconcile` must resolve the
  shared budget's base before the loop, since the incoming `budget` may be `None`.

---

## Section 1 — Architecture & data flow

```
main() run --model <cheap-model> --i-will-spend-real-money   (MAX_RETRIES=1 in env)
   |   two barriers in series: explicit flag + tiny batch budget; model pinned
   |
   ├─ build_llm("openrouter")  ->  build_openrouter_coder(model=<explicit>, max_tokens=2048)
   |     └─ OpenRouterCoder (adapter, implements LLMCoder.complete):
   |           • import agent build_llm (sys.path guard) -> build_llm(model_name=model)
   |           • .bind(max_tokens=...) to cap single-call output cost
   |           • .complete = invoke([HumanMessage(prompt)]) -> extract text blocks -> str
   |           • wrap auth/quota exceptions as LLMUnavailable
   |           • import/signature failure -> fail-loud RuntimeError
   |
   └─ reconcile_foundry_jobs(..., batch_max_llm_calls=N):
         shared = ForgeBudget(max_llm_calls=N)          # one counter for the whole batch
         each job runs against `shared` (threaded through run_foundry_job -> run_foundry)
         forge charges `shared` BEFORE each call, so failed jobs' spend still counts
         after each job (or on BudgetExhausted): shared.used >= N -> stop the batch
```

### N1 — the adapter (`OpenRouterCoder`)

Wraps the agent's `ChatOpenAI` to satisfy `LLMCoder.complete(prompt) -> str`.
`.invoke()` returns an `AIMessage`; its `.content` may be a plain string or a
list of content blocks (`[{"type":"text","text":"..."}]`). The adapter extracts
and joins the `text` blocks rather than `str()`-ing the list (which would inject
Python syntax into the parser's input). Reasoning is already separated by the
agent's client, but a defensive strip guards against a model that inlines
`<think>` in content.

Exception handling is **narrow, not blanket**: the adapter catches only the
concrete provider auth/quota/connection exception types (from the `openai` SDK
surfaced through LangChain) and re-raises them as `LLMUnavailable`. A bare
`except Exception` would relabel a genuine code bug (e.g. `TypeError`) as a quota
error and hide it — so those propagate unchanged.

### N2 — batch hard cap via one shared `ForgeBudget`

`reconcile` constructs a single `ForgeBudget(max_llm_calls=batch_max_llm_calls)`
and threads it through every job. `forge` charges it *before* each `complete()`
([forge.py:167]), so the counter reflects real spend the instant a call is made —
even if the job then raises and never returns a summary. When the shared budget is
exhausted, `forge` raises `BudgetExhausted`, `run_foundry` ends that job cleanly
(`budget_exhausted=True`), and `reconcile` stops the batch (or checks
`shared.used >= batch_max` after each job).

This requires a minimal, backward-compatible change: `run_foundry` and
`run_foundry_job` gain an optional `forge_budget=None` parameter; `run_foundry`
uses the passed one if given, else builds a fresh per-job one as today
(`forge_budget or ForgeBudget(max_llm_calls=budget.max_llm_calls)`). The per-job
`Budget` still governs structural limits (`max_factors`, `early_stop_after`); the
shared `ForgeBudget` is the money cap.

*Why not sum returned summaries:* a job that raises never returns a summary, so
its spent calls would vanish from the total and the next job would be over-funded
— a real over-spend, caught in review. Charging a shared counter before the call
closes that hole.

### N3 — the spend barrier (`main()` run)

Without `--i-will-spend-real-money`, `run` refuses and exits with a message — it
does not silently fall back to `ScriptedLLM` (a test double that must not enter a
production path). With the flag, it builds the real client from the explicit
`--model`, runs reconcile, and prints `llm_calls_used` + token count at the end.

---

## Section 2 — Components & files

| File | Responsibility |
|---|---|
| `research/hermes/llm_client.py` (create) | `OpenRouterCoder` + `build_openrouter_coder(*, model, max_tokens=2048)`; `LLMUnavailable(HermesGuardError)` |
| `research/hermes/foundry_runner.py` (modify) | `build_llm("openrouter")` returns the coder; `reconcile` builds a shared `ForgeBudget(batch_max_llm_calls)`, threads it through, aborts on `LLMUnavailable`; `main` run gains `--model`, `--i-will-spend-real-money`, usage print |
| `research/hermes/orchestrator.py` (modify) | `run_foundry` + `run_foundry_job` gain an optional `forge_budget=None`, used if given, else built per-job as today |
| `research/tests/test_hermes_llm_client.py` (create) | adapter unit tests (monkeypatch agent `build_llm`) |
| `research/tests/test_hermes_foundry_runner.py` (modify) | batch-budget + flag-gating tests |

Interfaces:

```python
# research/hermes/llm_client.py
class LLMUnavailable(HermesGuardError):
    """The LLM backend is unusable (bad/absent key, exhausted quota, unreachable
    endpoint). Infrastructure, not a repairable code error: it must abort the
    batch, since every subsequent job would hit the same wall."""

class OpenRouterCoder:
    def __init__(self, chat, max_tokens: int): ...      # chat = bound ChatOpenAI
    def complete(self, prompt: str) -> str: ...

def build_openrouter_coder(*, model: str, max_tokens: int = 2048) -> OpenRouterCoder: ...
```

---

## Section 3 — Error handling & honest boundaries

### Error classification

| situation | detected at | class | behaviour |
|---|---|---|---|
| bad/absent API key (auth) | adapter `.complete` catches the provider exception | `LLMUnavailable` | propagates → aborts the batch |
| exhausted quota | same | `LLMUnavailable` | aborts the batch |
| transient 502/timeout | agent `build_llm` with `MAX_RETRIES=1` | — | one retry; then propagates |
| agent import / signature break | `build_openrouter_coder` | `RuntimeError` (fail-loud) | `run` dies before any job |
| batch budget spent | reconcile, between jobs | normal stop | clean stop, prints usage |

**`LLMUnavailable` needs no forge change, but does need an explicit reconcile
branch.** `llm.complete()` is called at [forge.py:169], *outside* forge's `try`
(which starts at line 180), so an exception from it propagates straight out of
forge — never mistaken for a repairable code error, never retried. It exits
`run_foundry` (not caught by `except BudgetExhausted`), is re-raised by
`run_foundry_job` after it writes `status=failed`, and reaches `reconcile`.

`reconcile`'s current chain is `except SandboxError` (abort if not
`SandboxRunFailed`) then `except Exception: continue` ([foundry_runner.py:100-106]).
`LLMUnavailable(HermesGuardError)` is **not** a `SandboxError`, so without a new
branch it would be swallowed by `continue`. Add an explicit
`except LLMUnavailable: raise` **before** the generic `except Exception`, so an
auth/quota failure aborts the batch (every subsequent job would hit the same
wall). Ordering is load-bearing — a test locks it.

### Honest boundaries (written into docstrings, not left in the head)

1. **Call-count is a floor, not exact cost.** With `MAX_RETRIES=1`, one
   `.complete()` may issue up to two billed requests; `ForgeBudget` sees one. The
   batch ceiling is "at least stops here", not "spends exactly this".
2. **The batch budget assumes sequential reconcile.** Currently a plain `for`
   loop. Parallelising later would void the between-jobs check; it would need a
   thread-safe counter.
3. **No USD.** Calls + tokens only; converting to money needs a per-model pricing
   table, out of scope.
4. **`max_tokens` is a cost cap, not a quality guarantee.** Binding 2048 stops a
   runaway completion; a factor whose code genuinely needs more gets truncated and
   the hypothesis is forge-buried. Acceptable for a small cheap model.

---

## Section 4 — Test matrix

Principle: every guard gets a test the production path calls it; no test spends
money (all monkeypatch the agent `build_llm`).

### Adapter unit — `test_hermes_llm_client.py`

The `FakeChat.invoke` returns an **`AIMessage`-shaped** object (an `AIMessage`,
or a `SimpleNamespace(content=...)`), never a bare `str`/`dict` — matching what
the real `ChatOpenAI.invoke` returns, so the adapter's `.content` access is
actually exercised.

| test | assertion |
|---|---|
| `test_complete_flattens_text_blocks` | invoke returns `AIMessage(content=[{"type":"text","text":"c"}])` → `complete()` returns `"c"` |
| `test_complete_passes_plain_string_content` | invoke returns `AIMessage(content="c")` → returns `"c"` |
| `test_complete_wraps_auth_error_as_llm_unavailable` | FakeChat.invoke raises the SDK's `AuthenticationError` → `complete` raises `LLMUnavailable`, not the raw exception |
| `test_complete_does_not_swallow_a_code_bug` | FakeChat.invoke raises `TypeError` → it propagates as `TypeError`, **not** relabelled `LLMUnavailable` |
| `test_adapter_binds_max_tokens` | FakeChat records bind kwargs → `max_tokens` was passed |
| `test_build_openrouter_coder_fails_loud_on_bad_import` | monkeypatch the import to fail → `RuntimeError` with a clear message |
| `test_build_openrouter_coder_passes_explicit_model` | assert `build_llm` received `model_name=<explicit>`, not read from env |

### Batch budget + flag — `test_hermes_foundry_runner.py`

| test | assertion |
|---|---|
| `test_batch_budget_is_a_shared_counter_across_jobs` | jobs share one `ForgeBudget`; total charged calls never exceed `batch_max_llm_calls`, and the batch stops when it is reached |
| `test_batch_cap_holds_when_a_job_raises_mid_sweep` | a job that raises after spending calls still leaves those calls counted in the shared budget → the next job is not over-funded |
| `test_llm_unavailable_aborts_whole_batch` | job1 raises `LLMUnavailable` → job2 **not executed** (explicit branch before `except Exception`) |
| `test_run_without_spend_flag_refuses_and_never_builds_llm` | no flag → refuses; monkeypatched `build_llm` asserted **not called** |
| `test_run_with_spend_flag_builds_llm_and_reconciles` | flag + monkeypatched fake coder → reconcile called, usage printed |

### Honestly-recorded gaps

- **The real OpenRouter endpoint is never hit by a test.** Every test
  monkeypatches the agent `build_llm`. The adapter↔real-API connectivity is
  verified only by the one manual, approved paid run. This is deliberate (tests
  never spend money), and stated so no one mistakes green tests for proof the API
  works.
- **forge is unchanged** — `complete()` sits outside forge's `try`, so
  `LLMUnavailable` propagates cleanly (verified at [forge.py:169]).

---

## Paid-run runbook (manual, user-gated)

Not a test. Run once, by the user, after explicit approval:

```bash
# tiny budget, retries down, model pinned to a cheap coding model
export OPENROUTER_API_KEY=sk-or-...
export MAX_RETRIES=1
python -m research.hermes.foundry_runner run \
  --runs-dir runs --manifests-dir research/manifests \
  --zoo-dir agent/src/factors/zoo --image talos-sandbox:test \
  --model <cheap-openrouter-coding-model> \
  --i-will-spend-real-money
# defaults: batch_max_llm_calls=6, max_factors=2, max_tokens=2048
# prints llm_calls_used + token count at the end
```

The model name is left to the operator (OpenRouter, per the platform's
Anthropic-direct constraint) — a small, cheap coding model. The batch budget and
`max_tokens` bound the spend regardless of which is chosen.

---

## Self-review

- **Placeholders:** none. `<cheap-openrouter-coding-model>` in the runbook is an
  operator choice (model is env/flag-driven, never hardcoded), stated as such.
- **Consistency:** the error classes in §3 match the adapter behaviour in §1 and
  the tests in §4. `LLMUnavailable` is defined once (§2), aborted explicitly in
  §3, tested in §4. The batch cap is one shared `ForgeBudget` in §1/§2/§4 — no
  sum-of-summaries anywhere (the flaw the second review removed).
- **Scope:** one implementation plan. USD accounting, token budgeting, a shared
  module, and real-API tests are explicit non-goals.
- **Ambiguity:** "infra vs repairable" is pinned to concrete classes
  (`LLMUnavailable`, `SandboxError`-not-`SandboxRunFailed`); the spend gate to a
  concrete flag; the cap to a shared `ForgeBudget` charged before each call.
