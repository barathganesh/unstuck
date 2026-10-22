# Decision Receipt tool selection

## Application measurement, not a recipe benchmark

Run 1 happens once: one inference, 21.6 s client time, unresolved, with the
receipt stored. Run 1 is not saved. The superseded post-D1 Run 2 made two
inferences and took 54.8 s. Optimized Run 2 exact-matches the receipt before
selection, precludes only the proven dead end, and lets Decider make one fresh
choice from the remaining tools. It completes with one inference in 26.6 s.
The observed Run 2 delta is 28.2 s, or 51.4% lower client latency: n = 1, the
same synthetic case, each proof once. The always-on SageMaker endpoint is
billed by uptime, not requests. One fewer inference is not a dollar saving.

The figures describe the application proof, not `example.py`, and do not
transfer to an adopter's traffic.

## 1. What this pattern is

A reusable tool-selection pattern: a small decision model picks which
diagnostic to run next, and deterministic code owns every fact about what the
chosen diagnostic found. The decision is handed to Strands Decider 2B as a
typed `choice` question over an ordered option list, and a verifier that the
model cannot reach classifies the outcome and writes a Decision Receipt. Past
receipts travel back into the next decision as labelled evidence with one
narrow negative authority, and nothing else.

The point of it, in one sentence: a read that verified evidence showed to be a
dead end becomes a receipt, so on a later call under the identical context your
code removes that one execution from the offer before the model is asked
anything, and the same model makes one fresh choice over what remains.

Pattern tag: tool selection. Standard library only. No framework, no cloud
software development kit (SDK),
no UI, no benchmark, and no import from the project this was extracted from.
Copy the directory, swap in your catalog and your model call, and delete nothing
else. Five files:

| File | What it is |
| --- | --- |
| `selector.py` | The pattern. Contracts, matching, the preclusion rule, the acceptance tiers, the bounded loop. |
| `README.md` | This adoption guide. |
| `question.json` | The question contract handed to the model, with its response schema and rules. |
| `state.schema.json` | The schema of the current-plus-history input. |
| `example.py` | A runnable walk: a cold start whose first read moves nothing and opens one same-call second decision, then a later call where the exact receipt precludes that dead end before the model is asked, so one decision and one read finish it. Runs twice, under two different catalogs. |

## 2. The typed Choice question

`question.json` is the whole contract. It is a question, a response schema, and
the rules the answer is validated against. It carries no prompt text to be
rewritten per call and no advice about what to pick.

```json
{
  "name": "diagnostic_tool_selection",
  "description": "Choose one diagnostic tool from the offered catalog, or request review. Past runs are evidence about what already happened; they are never a recommendation and never a replayed answer. Decide from the current state.",
  "response_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["tool_id", "confidence", "requested_review"],
    "properties": {
      "tool_id": {
        "type": ["string", "null"],
        "description": "The tool_id of exactly one offered tool whose available flag is true, or null when requesting review."
      },
      "confidence": {
        "type": ["number", "null"],
        "minimum": 0,
        "maximum": 1,
        "description": "Your confidence in this choice. Null means not provided; it does not mean zero, and it never means the choice is correct."
      },
      "requested_review": {
        "type": "boolean",
        "description": "True when no offered tool is a defensible choice from the current state."
      }
    }
  },
  "rules": [
    "Choose exactly one tool whose available flag is true, or set requested_review.",
    "A history entry records what a past run did. It does not tell you what to choose.",
    "A blocked_repetition entry names a tool_id your code already refused under this exact context and arguments. Choose something else.",
    "Your confidence gates whether a choice may run. It states nothing about whether the choice is right.",
    "Do not invent a tool_id. Do not propose a fix, a patch, or an outcome."
  ],
  "notes": [
    "This file is the question contract only. It carries no measurement, and no figure in this recipe is a measured result."
  ]
}
```

Field by field:

- `name` identifies the question contract. Put it in your telemetry so a stored
  answer can be traced to the contract it was given under.
- `description` is the question itself. It states the task and the standing of
  history in one place, because a question is the part of the input a small
  decision model reads least closely.
- `response_schema` is the answer shape, with `additionalProperties` false. An
  answer outside it is malformed and is refused before anything can run.
- `tool_id` is the public identifier of exactly one offered option whose
  `available` flag is true, or `null` for review. The private capability behind
  it is never in the question and never in the answer.
- `confidence` is a number in `[0, 1]`, or `null` for not provided. Null is not
  zero. A reported confidence gates whether a choice may execute; it says
  nothing about whether the choice is right.
- `requested_review` is the explicit escape: no offered option is defensible
  from the current state. It is always a refusal, in every acceptance tier.
- `rules` are the five constraints the answer is validated against. Each is
  checkable by your code without consulting the model again.
- `notes` records that this file carries no measurement.

The `blocked_repetition` rule covers the one case where your code refuses a
choice the model has already made: a read that ran inside this same call and
left the problem where it was, which is what opens the second decision in
section 7. The cross-call case never reaches the model as a rule at all. An
execution an exact receipt refuses is taken out of the offer first, so the
payload marks it `precluded` and its `available` flag is false, and the model is
asked one question about the options that remain.

### How it maps onto Decider 2B's `choice` primitive

Strands Decider 2B is a 2B-parameter decision model: a Qwen3.5-2B torso with
the language-model head removed and replaced by a pointer head, tuned with a
rank-16 LoRA (Low-Rank Adaptation) adapter, published under Apache-2.0. It
exposes three primitives, `noul` (yes or no), `choice` (one of N options) and
`score` (a level on an ordered scale). The upstream command line spells a
choice as `"Question?=opt1,opt2,opt3"`. An answer to a `choice` carries a
confidence and a probability per option.

This question is one `choice`. The option list is the ordered set of offered
tool identifiers followed by one review option, and the pointer head names
exactly one of them. Your adapter translates that answer into the recipe's
`Choice`:

```python
from selector import Choice

def my_selector(payload: dict) -> Choice:
    answer = ask_decider(render_choice(payload))   # your call, one attempt
    return Choice(
        tool_id=None if answer.selected == "request_review" else answer.selected,
        confidence=answer.confidence,
        requested_review=answer.selected == "request_review",
    )
```

`Choice` keeps only the three values the acceptance rule needs. Keep the
per-option distribution in your own record, and refuse an answer whose
distribution does not cover exactly the offered options or does not sum to one,
before it reaches `select_tool`. That check is structural, so it refuses in
both acceptance tiers below.

Two pieces of upstream guidance shape how this question is built. First, the
published advice is to confirm with a person, or to ask one, below a confidence
of 0.9, and to measure calibration on your own traffic before relying on a
threshold; this recipe therefore treats a confidence as recorded telemetry and
makes acceptance an explicit, declared policy rather than a number someone
picked. Second, the model card names long multi-step documents as the weak spot
and says question phrasing matters because questions are read less closely than
documents, which is why the state handed over is a compact digest-bearing
projection with a hard size bound rather than a transcript. No accuracy figure
for the model is claimed here, in either tier.

## 3. Ordered option criteria

The option list is the part of this pattern most easily got wrong, so it has
its own rule.

Each option's rendered criterion has two parts:

1. **What the diagnostic observes.** The facts it returns, named. This is
   `ToolCard.observable`.
2. **One condition sentence saying when observing those facts separates the
   remaining explanations.** This is `ToolCard.preconditions`.

A criterion that only lists what a tool reads does not answer the question
being asked. "Reports the join key encodings" tells the model nothing about
when reading them is the move; "the rows are absent while both sides of the
join are populated" does.

The condition sentence is part of the public card, so a renamed catalog
publishes renamed conditions and the two stay in step. It must name no other
tool, no case class, no category, no expected outcome and nothing a known
answer would tell you. It is an observable condition about the current state,
never advice about what to choose.

```python
from selector import Catalog, ToolCard

catalog = Catalog(cards=(
    ToolCard(
        tool_id="check_feed_state",
        capability="pipeline",
        description="Report whether the upstream feed completed for this scope.",
        observable=("job_state", "watermark", "scope_complete"),
        preconditions=(
            "an unfinished upstream window is still one of the open explanations",
        ),
    ),
    ToolCard(
        tool_id="check_key_shapes",
        capability="schema_keys",
        description="Report the join key encodings and whether they are unique.",
        observable=("key_encoding", "keys_unique"),
        preconditions=(
            "the rows are absent while both sides of the join are populated",
        ),
    ),
))
```

Options are presented in a fixed declared order: `Catalog.public()` renders
`Catalog.cards` in the order you declared them, every call, so the same state
produces the same option list.

Be plain about what that order and that wording are. Option wording here is a
fitted choice. In the project this pattern was extracted from, two framings
were compared on development fixtures and the second was frozen; option order
was not permuted. So the framing is fitted to development observations rather
than neutral, and an adopter inherits a starting point to re-fit on their own
traffic, not a neutral baseline.

`cost_class`, `input_schema` and `contract_version` complete the published
card. The first two are what the acceptance tier in section 4 is decided from.
The third records the shape the card was published under, so a receipt names
the shape the model read it in. Bump it when the public shape of a card
changes.

## 4. The `read_only_structural` acceptance tier

Selection and acceptance are two separate questions. The model selects; your
policy decides whether a structurally valid selection may execute.
`selector.py` offers two tiers, and the tier is a declared argument, never an
inference:

- **`strict_confidence`** is the default. A structurally valid answer must also
  clear the configured floor, which ships at `0.80`.
- **`read_only_structural`** accepts a structurally valid answer naming an
  available, argument-free, read-only diagnostic whatever confidence it
  reported. It is refused outright, at any confidence, unless every executable
  option offered is read-only and argument-free: `read_only_surface()` checks
  each offered card's `input_schema` against `READ_ONLY_INPUT_SCHEMAS` and its
  `cost_class` against `READ_ONLY_COST_CLASSES`, and an option with no
  published card fails the check. A surface that fails it returns
  `unsafe_tier_surface`.

No tier weakens any clause of structural validity. Every one of these refuses
in both tiers, and the tier is reached only by an answer that cleared all of
them. Three of the clauses live in `_unusable` because this file can check
them; the rest belong in your adapter, because only your adapter sees the raw
answer, the deadline and the revision it came back under.

| Refused in both tiers | Why | Checked by |
| --- | --- | --- |
| A malformed answer | It is not an answer to this question. | your adapter |
| A transport or provider failure | Nothing was answered. | your adapter |
| A late answer, past your own deadline | The state it was answered from may no longer hold. | your adapter |
| A wrong model revision | The answer is not bound to the revision the context names. | your adapter |
| A wrong option set | The answer is not about the options that were offered. | your adapter |
| An invalid distribution | The reported probabilities do not describe the offered options. | your adapter |
| The review option | It is an explicit statement that no option is defensible. | `selector.py` |
| An unavailable or unknown option | It is not offerable in this state. | `selector.py` |
| An absent confidence | Nothing was reported to accept or to record. | `selector.py` |

Each returns its own reason code, in order: `review_requested`,
`tool_unavailable` (or an `unknown_tool` refusal for an identifier no card
publishes) and `low_confidence`.

The honest framing of the relaxed tier, which this recipe keeps in the open:
the tier is **not** calibration and **not** the default. Nothing here has
measured reported confidence against outcomes. The tier is sound only because
the entire offerable surface is argument-free read-only observation inside a
two-read bound, so the worst outcome of a wrong accept is one local read that
the verifier then classifies, and that classification is what actually decides
whether anything happened. That reasoning does not transfer to any action that
changes state. For a write, an argument-taking capability, a remote call that
costs money, or anything with a side effect, use `strict_confidence` and
measure your own calibration first.

The tier is part of your frozen policy, so declare it inside
`Versions.policy_version`. It then enters the decision context every receipt is
bound to, which means a receipt written under one tier cannot match a decision
made under the other: the versions digest differs, so the match falls to
`no_match`.

```python
from selector import Versions, resolve_problem

versions = Versions(
    data_version="your-data-version",
    model_revision="your-pinned-revision",
    catalog_version="cards-v1",
    policy_version="policy-v1+read_only_structural",
)

resolution = resolve_problem(
    context, catalog, available, store, my_selector, my_executor,
    tier="read_only_structural",
)
```

## 5. The receipt schema

`Receipt` is verifier-owned. The model has no part in writing one.
`Receipt.receipt_id` is the digest of the complete immutable binding set, and
the table below is that set, field by field, as `receipt_id` names it. The
digest is canonical, with sorted keys and fixed separators, so identity never
depends on the order anything was written in.

| Hashed field | Receipt attribute | What it binds |
| --- | --- | --- |
| `capability` | `capability` | The private capability that ran. Never published. |
| `public_tool_id` | `public_tool_id` | The public identifier it ran under. |
| `arguments` | `arguments_digest` | The identity of the arguments it ran with. |
| `outcome` | `outcome` | The class deterministic code assigned. |
| `reason` | `reason` | Your reason code for that outcome. |
| `problem` | `problem_fingerprint` | The problem identity, stable across calls. |
| `context` | `context_digest` | The complete pre-call decision context. |
| `versions` | `versions_digest` | The digest of every bound version. |
| `changed` | `evidence_changed` | Whether the read admitted a fact not already held. |
| `moved` | `disposition_moved` | Whether that fact moved a gate you act on. |
| `decision` | `decision_id` | Which decision this receipt describes. |
| `model_call` | `model_call_id` | Which model call made it. |
| `read` | `diagnostic_call_id` | Which executed read produced the outcome, or `None` when none ran. |
| `input` | `input_digest` | The identity of the input the decision was made from. |
| `post_call_state` | `post_call_state_digest` | The state the read left behind. |
| `evidence_before` | `evidence_before_digests` | The admissible evidence before the read, sorted. |
| `evidence_after` | `evidence_after_digests` | The admissible evidence after it, sorted. |

Carried beside the identity and **excluded** from it:

| Field | Why it is outside identity |
| --- | --- |
| `observed_at` | The UTC instant your clock observed. Audit information, so the same verified fact recorded twice is one row rather than two. |
| `versions` | The full `Versions` object, carried in readable form beside the `versions_digest` that is hashed. |

There is no expiry field, no time-to-live and no time window anywhere in this
file. Identity is content.

This module has no clock. When you pass no observation instant the receipt
records `UNRECORDED_OBSERVATION` rather than inventing one, and an instant that
is not UTC is refused outright. Three further validations run on construction:
admissible evidence can never be withdrawn by a read, new evidence identities
require an admitted fact, and a verified outcome must name the read that
produced it.

```python
from selector import CallTrace, make_receipt

store.put(
    make_receipt(
        context,
        catalog.card(tool_id),
        result,
        trace=CallTrace(                      # your own call identities
            decision_id=my_decision_id,
            model_call_id=my_model_call_id,
            diagnostic_call_id=my_read_id,
        ),
        input_digest=my_payload_digest,       # the input the model actually saw
        observed_at=my_clock.utcnow().isoformat(),
    )
)
```

Every keyword is optional and every one is worth passing. Without a trace and
an input digest, both are derived deterministically from the bound context,
tool and arguments, which keeps identity stable but tells an auditor less. A
receipt whose observed arguments are not the ones the context is bound to is
refused as `arguments_mismatch`, so a receipt can never describe an execution
that did not happen.

## 6. The exact-match rule

A receipt's only authority is negative, and it is narrow. It is spent before the
first decision, not after it: `preclusions()` walks the offered cards in their
declared order, and an execution an eligible receipt refuses is removed from
`available` with that receipt's own identifier and outcome class recorded as its
proof. Nothing about the remaining cards changes, and the removal costs zero
model calls and zero reads.

Blocking is a conjunction, and every clause has to hold:

- identical **capability**, so a rename cannot evade it and cannot trigger it,
- **and** identical arguments digest, so a receipt recorded for one argument
  set can never refuse a call that would run with different arguments,
- **and** identical decision context, which covers the problem, the task, every
  bound version, the admissible evidence, the candidate set and the capabilities
  already used,
- **and** one of the two disposition-negative outcome classes: `no_progress`,
  where the read admitted nothing, or `disposition_unchanged`, where the read
  returned real admissible data and the decision still did not move.

The second class is the one that matters. A read that returns real data and
leaves the decision exactly where it was is the blind spot a model repeats, and
a taxonomy that records only "did it return anything" cannot express it.

`Receipt.blocking` re-derives eligibility from the two recorded deltas rather
than trusting the outcome label: a receipt labelled `no_progress` that recorded
an admitted fact, or labelled `disposition_unchanged` that recorded none,
carries nothing. A receipt that recorded a moved gate is never eligible,
whatever its label says.

Three classes never block at all. A `provider_error` means nothing was
answered. An `invalid_evidence` read failed your validation. A
`useful_evidence` read moved a gate, which is the opposite of a reason to
refuse it. All three are labelled history.

Current facts dominate. Change the evidence, change the candidate set, or bump
a bound version, and `context_digest` changes: the old receipt stops matching
exactly, so it stops blocking and travels as `compatible` history instead.
Historical failure is never permanent prohibition.

## 7. D1 and D2 wiring

`resolve_problem` is the bounded loop end to end. D1 is the first decision, D2
is the one optional re-decision, and there is no third.

1. **Build the current state.** The authoritative facts, the candidate set, the
   bound versions, and the identity of the arguments the next read would run
   with.
2. **Look up matching receipts.** `ReceiptStore.matching` returns at most 4,
   exact matches first, deterministically ordered.
3. **Preclude the proven dead ends.** Every offered execution an exact
   disposition-negative receipt refuses is removed from the offer before the
   model is asked anything, and costs zero reads and zero model calls. With no
   candidate left, the loop stops with `no_alternative_candidate` and the model
   is never called at all.
4. **D1 decides over what remains.** One model call, one attempt, no retry
   inside it. The payload carries current state, the public cards with the
   precluded ones marked unavailable, and the bounded labelled history. Over the
   size bound it is refused, never truncated.
5. **Validate before anything runs.** Structural validity, then the acceptance
   tier. A refusal is returned with a reason code rather than raised.
6. **Execute.** Your executor is handed the public tool, your private
   capability, the bound arguments identity and the pass index. It returns
   observed facts and labels nothing.
7. **Verify.** `classify_outcome` is deterministic code. `make_receipt` files
   the result against the card that ran.
8. **Finish or re-decide once.** A read that moved a supported gate finishes
   the problem on the `direct` path. Every other verified read opens the one
   second decision, D2, only if a read remains, and travels into it as labelled
   same-call history. D2's
   choice is final: if its read moves nothing either, the loop stops with
   `decisions_exhausted` rather than deciding a third time.

**The refreshed-state rule for D2.** A read changes the state, so D2 decides
from the state the first read left behind, not from the state D1 saw. Your
executor returns that refreshed state as a `StateProjection`, the loop rebuilds
the context from it, and the references D2 is shown are the ones matching the
refreshed state. The same-call receipt therefore reaches D2 labelled
`compatible` rather than as an exact match on a context that no longer holds.
The receipt itself still records the pre-call context it describes, which is
what keeps it evidence about a past call rather than a claim about the present
one. A read never withdraws evidence: a projection that drops a fact the
problem already held is refused.

A preclusion stands for the whole call. It was matched against the call's
original state, so a proven dead end is never re-offered to the second decision
even though that decision is made from the refreshed state.

| Bound | Value | Enforced |
| --- | --- | --- |
| Decisions per problem | 2 | before each model call |
| Tool runs per problem | 2 | before each executed read |
| Receipt references in one payload | 4 | when the payload is built |
| Selector payload | 8000 characters | refused rather than truncated |

A caller may only lower them. `max_decisions` and `max_tool_runs` are passed
through `min()` against the declared constants, so no argument can raise a
bound.

### Escaping a dead end with one tool run

Lower the tool-run bound to one, `max_tool_runs=1`, and a single problem can no
longer afford both the dead end and the answer. What `resolve_problem` then
does, on a fresh store:

- **A cold call stays unresolved.** D1 picks the dead end, it runs, and the
  verifier files a `disposition_unchanged` receipt. The one run is spent, so
  the loop stops with `tool_runs_exhausted` without asking for the second
  decision, D2: a choice no run is left to execute is never paid for.
- **A later call with the exact receipt escapes.** Under the identical context
  and arguments that one execution is precluded before the first decision, so
  D1 is asked one question about the remaining candidates and never gets the
  chance to name the dead end. The one run goes to whatever it chooses, and a
  read that moves a gate finishes the problem on the `direct` path, in one
  decision and one run, with no re-decision spent.

So under a one-run bound the cross-call receipt is what lets a later call
escape: the proven dead end is off the offer at no cost, and the one run goes to
a choice that can still move the problem. The receipt does not make that choice.
It narrows what was offered, and D1 chooses freshly over the rest.

### Wiring your executor

```python
from selector import ExecutionRequest, StateProjection, ToolResult

def my_executor(request: ExecutionRequest) -> ToolResult:
    observed = my_tools.run(request.capability)   # your call
    return ToolResult(
        arguments_digest=request.arguments_digest,
        ran=True,                    # False for a provider or transport failure
        evidence_changed=True,       # did it admit a fact you did not hold?
        disposition_moved=True,      # did that fact move a gate you act on?
        admissible=True,             # did it pass your validation?
        reason="schema evidence admitted",
        admitted_digests=("the-fact-it-admitted",),
        state_after=StateProjection(                 # the refreshed state
            evidence_digests=my_state.evidence_digests(),
        ),
    )
```

`evidence_changed` and `disposition_moved` are two different questions, and a
gate can never move without an admitted fact. A read that admits a fact and
moves nothing is useful evidence that opens the second decision, and it is
exactly the case the second dimension exists to record.

## 8. A minimal end-to-end example

Paste this, replace the two lines marked as yours, and it runs.

```python
from selector import (
    Catalog,
    Choice,
    ExecutionRequest,
    ProblemContext,
    ReceiptStore,
    StateProjection,
    ToolCard,
    ToolResult,
    Versions,
    canonical_digest,
    resolve_problem,
)

catalog = Catalog(cards=(
    ToolCard(
        tool_id="check_feed_state",
        capability="pipeline",
        description="Report whether the upstream feed completed for this scope.",
        observable=("job_state", "watermark"),
        preconditions=(
            "an unfinished upstream window is still one of the open explanations",
        ),
    ),
    ToolCard(
        tool_id="check_key_shapes",
        capability="schema_keys",
        description="Report the join key encodings and whether they are unique.",
        observable=("key_encoding", "keys_unique"),
        preconditions=(
            "the rows are absent while both sides of the join are populated",
        ),
    ),
))

context = ProblemContext(
    problem_id="orders-by-country-empty",
    task_digest=canonical_digest({"dataset": "your-dataset", "metric": "units"}),
    versions=Versions(
        data_version="your-data-version",
        model_revision="your-pinned-revision",
        catalog_version="cards-v1",
        policy_version="policy-v1+strict_confidence",
    ),
    arguments_digest=canonical_digest({"order_date_utc": "2026-01-01"}),
    candidates=("check_feed_state", "check_key_shapes"),
)

def my_selector(payload: dict) -> Choice:
    answer = ask_decider(payload)                 # your call
    return Choice(
        tool_id=answer["tool_id"],
        confidence=answer["confidence"],
        requested_review=answer["requested_review"],
    )

def my_executor(request: ExecutionRequest) -> ToolResult:
    observed = run_read(request.capability)       # your call
    return ToolResult(
        arguments_digest=request.arguments_digest,
        ran=observed["ran"],
        evidence_changed=observed["admitted_a_fact"],
        disposition_moved=observed["moved_a_gate"],
        admissible=observed["passed_validation"],
        reason=observed["reason_code"],
        admitted_digests=observed["fact_digests"],
        state_after=StateProjection(evidence_digests=observed["evidence_digests"]),
    )

store = ReceiptStore()
resolution = resolve_problem(
    context,
    catalog,
    frozenset({"check_feed_state", "check_key_shapes"}),
    store,
    my_selector,
    my_executor,
    observed_at="2026-01-01T00:00:00Z",
)

print(resolution.path)          # "direct", "rechosen" or "unresolved"
print(resolution.tool_id)       # the public id of the tool that ran, or None
print(resolution.decisions)     # never more than 2
print(resolution.tool_runs)     # never more than 2
print(resolution.outcomes)      # one verifier-assigned class per executed read
print(resolution.receipt_ids)   # one receipt per executed read
print(resolution.precluded)     # exact_repeat, removed before the first decision
print(resolution.refusals)      # failed_execution, reads that moved nothing
print(resolution.refused)       # the reason code when it stopped, else None
for receipt_id in resolution.receipt_ids:
    print(store.rows[receipt_id].outcome, store.rows[receipt_id].blocking)
```

`resolution.path` is `direct` when the first read moved a gate, `rechosen` when
exactly one second decision ran, and `unresolved` when it stopped with a reason
code. `resolution.refused` names that reason.

## 9. Why this is not memory and not a cache

A cache answers the question for you. Memory decides for you from what it
remembers. A receipt does neither: it tells the model what already happened and
makes it decide again. Three properties keep that distinction real.

- **Identity is content, not time.** The binding set in section 5 is the
  identity. The observation instant is excluded, there is no expiry, and there
  is no time window, so a receipt can never be served instead of a decision.
- **The authority is negative and singular.** A receipt can refuse one
  identical repetition under one identical context, by taking that one
  execution out of the offer. It cannot select a tool, score a candidate,
  reorder the options, generate an outcome label, authorize a repair, or
  override the second decision. The remaining candidates reach the model in
  their declared order, unranked and unannotated, and the model still answers
  the same question it would have answered without any receipt at all.
- **Current facts dominate.** A receipt is bound to the context it was recorded
  under. The moment the state moves, it is history rather than a rule.

The model also never learns what ran internally. `ToolCard.public()` sends
`tool_id`, the description, the observables, the public preconditions, the
input schema, the cost class and the contract version, and never `capability`.
A history entry names the public `tool_id` plus an opaque `execution_key`, a
digest of the capability and the arguments, so two past executions are
comparable without the capability leaking. `blocked_repetition` names the same
two public values, and `current_state.already_used` names what has already run
as public tool identifiers, or as an opaque execution key for anything your
catalog does not publish.

## 10. Usage and tests

### Run the example

From inside the recipe directory:

```
python example.py
```

It walks one problem through the whole bounded loop twice, under two different
catalogs whose identifiers differ entirely and whose capabilities are identical.
The first walk is a cold start: the model decides, the read runs, the verifier
writes a receipt, and because that read left the problem where it was, one
same-call second decision runs. The second walk is the same problem again, so
the exact receipt from the first walk precludes the identical repetition before
the model is asked, and one fresh first decision over what remains finishes the
problem on the `direct` path, in one decision and one read.

What that exercises: the bounded loop end to end on both catalogs with an exit
code of 0, at most two decisions and two reads, one receipt per executed read,
the preclusion rule taking the exact repetition out of the offer, and the
catalog-independence property. Both walks behave the same way, which is the
point: the pattern does not know or care what your tools are called.

Everything `example.py` prints is synthetic and labelled `"classification":
"synthetic"` in its own output. It measures nothing.

### Check that it is genuinely standalone

Copy the five files into an empty directory and run the example there, with
only that directory importable:

```
cp <recipe-dir>/*.py <recipe-dir>/*.json "$EMPTY_DIR"
cd "$EMPTY_DIR" && python example.py
```

What that exercises: independence. A hidden dependency on anything outside the
directory fails the import there rather than surprising you after adoption.

### The behaviour suite

The repository this pattern was extracted from carries a behaviour suite for it
under `tests/public_deliverables`, run with `uv run pytest
tests/public_deliverables -q`. What it exercises: the import graph and the
standard library restriction, the outcome taxonomy, the declared bounds, the
blocking conjunction in both its forms, the preclusion happening before the one
first decision and leaving an unblocked payload unchanged, the refreshed-state
rule for the second decision, receipt identity over the whole binding set, the
exclusion of the observation instant from identity, and that no payload the
model sees carries a private capability.

## 11. What this recipe does not do, and what is not claimed

**What it does not do.** It does not propose a fix, apply a patch, retry a
failed call with identical input, call the model recursively, open a third
decision, or decide whether an answer is correct. It ships no clock, no store
beyond an in-memory one you are meant to replace, no provider client, and no
retry policy. Those are deliberately yours.

**What is not claimed.** The example's output is synthetic and labelled as
such. This recipe reports no measurement. The application observation above is not a benchmark, a
success rate, a calibration or a cost claim, and no accuracy
figure for Decider 2B is stated in either acceptance tier. Any comparison
against an alternative approach is pending a measurement that does not exist in
this file, so none is offered.

One piece of corroborating context, and only this much: the mechanism was
exercised end to end in the project this was extracted from. Under a one-read
bound, against one hosted model checkpoint on one synthetic case, a control
without receipts repeated the dead-end choice and ended unresolved on 3 of 3
calls. With receipts, one call paid that dead end once and the verifier recorded
it; a later call on the same problem precluded that one execution before the
model was asked, and the fresh first decision over the remaining options
finished on one read, 1 of 1. Two earlier sequences are superseded and kept as
history rather than erased: the same case under a flow that refused the repeat
after the first decision instead of before it, which spent a second model call
to recover, and before that, five development cases (n = 5 cases) and one pair
of calls under a two-read bound (n = 1 pair) where a later call spent one fewer
read. These are existence results on stated denominators. None is a rate and none transfers to your traffic. The separately recorded
application timing comparison appears at the start of this document.
