# Unstuck

Run 1 happens once: 1 inference, 21.6 s client time, unresolved, receipt stored.
Run 1 is not saved. Optimized Run 2 exact-matches that receipt before selection,
precludes only the proven dead end, and lets Decider make one fresh choice
from remaining tools: 1 inference, 26.6 s, completed.

Superseded Run 2 used 2 inferences, 54.8 s. Observed Run 2 delta: 28.2 s,
51.4% lower client latency. n = 1, same synthetic case, each proof once.

**Decider 2B makes tool decisions, but a new call can repeat a decision
already proven useless. Unstuck turns that verified dead end into a Decision
Receipt, forcing the same Decider to choose a new path and escape the decision
loop, without a larger model taking over.**

Tags: `#strands-decider-2b` `#tool-selection`

![Top: the memoryless control circles the same decision track three times, repeats inspect_pipeline and stays 3 of 3 unresolved. Bottom: Run 1 spends one inference and one read, remains unresolved in 21.6 seconds, and the verifier stamps a disposition_unchanged Decision Receipt. The paper receipt travels into Run 2, which exact-matches it and cuts only inspect_pipeline before the first Decider request; the same Decider chooses inspect_schema_keys from three remaining diagnostics. The loop opens into a forward path: one read and verified repair lead to completed in 26.6 seconds on one inference. Observed Run 2 client time is 28.2 seconds and 51.4% lower than old Run 2 at 54.8 seconds and two inferences; n = 1, same synthetic case, each proof once. No dollar savings measured.](docs/unstuck-flow.svg)

Run 1 records the dead end; Run 2 uses it before selection.

Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The
memoryless control stays stuck.

No dollar savings are claimed. The always-on SageMaker endpoint is billed by
uptime, not per request. One fewer inference does not itself lower the bill.
The measurements come from
[`dist/public-demo/loop-escape-proof.json`](dist/public-demo/loop-escape-proof.json).

## The thirty second version

**The problem.** A synthetic order report returns zero rows. Decider 2B picks
`inspect_pipeline`. The read returns real data: the partition arrived. That is
true and it settles nothing, because this incident turns on how the two sides
of a join spell their keys. Each call may spend one diagnostic read, so the
call ends unresolved. A new call about the same incident starts fresh, and
Decider 2B picks `inspect_pipeline` again.

**The solution.** The deterministic verifier writes down what that read proved:
real data, no change to any decision the system may act on. That record is a
Decision Receipt. It never picks a tool. Its one power is negative: on a later
call with the identical context, the controller takes that identical execution
out of the offer before the first Decider request, and Decider 2B chooses from
what is left.

**The proof**, on one synthetic case, with real hosted Decider 2B choices:

1. **The memoryless control stays stuck.** Three independent calls without
   receipts. Each was offered four diagnostics, picked `inspect_pipeline`,
   spent its one read, and ended unresolved. 3 of 3.
2. **Run 1 pays the learning cost once.** With receipts, the first call had an
   empty decision history, was offered the same four diagnostics, made the same
   choice, spent its one read, and ended unresolved on
   `diagnostic_reads_exhausted`. The verifier stored a Decision Receipt for
   that dead end. One inference. Run 1 is the learning call, not a saving, and
   is never presented as one.
3. **Run 2 uses the receipt before selection.** A new call on the same problem.
   The controller matched the Run 1 receipt exactly and precluded only that
   proven dead end, before the first Decider request and before any read.
   Decider 2B was offered the three remaining diagnostics in their original
   order and made one fresh choice, `inspect_schema_keys`. One read, the repair
   applied, verification passed, `completed` on `populated_after_repair`. One
   inference, and no second Decider call. 1 of 1.

A preclusion spends no read and no inference, so the call's one read goes to a
path nothing has proven useless. That is what separates Run 2 from the control.

## The evidence

Two separate proofs, each on the same declared synthetic fixture, each run
once. Every count sits next to its denominator. These are existence results on
one case, not rates.

### Real hosted Decider 2B, five choices

The experiment harness called one pinned Decider 2B checkpoint on a private
Amazon SageMaker endpoint. Verdict **PASS**, 13 of 13 checks.

| | Control, without receipts | Run 1, with receipts | Run 2, with receipts |
| --- | --- | --- | --- |
| Calls | 3 independent | 1 | 1, a new call |
| Decision history | not requested | empty | matched, exact receipt |
| Diagnostics offered | 4 | 4 | 3, the proven dead end precluded |
| Precluded before the first Decider request | not applicable | no receipt yet | **yes** |
| Decider choice (D1) | `inspect_pipeline`, 3 of 3 | `inspect_pipeline` | **`inspect_schema_keys`** |
| Second Decider call (D2) | none | none | none |
| Decider inferences | 1 of a bound of 2, each call | 1 of a bound of 2 | 1 of a bound of 2 |
| Diagnostic reads | 1 of 1 allowed, each call | 1 of 1 allowed | 1 of 1 allowed |
| Receipt written | not stored | `disposition_unchanged`, stored | `useful_evidence`, stored |
| Terminal result | `unresolved`, 3 of 3 | `unresolved` | **`completed` / `populated_after_repair`** |

Five Decider 2B answers in all: the 3 control choices, Run 1's choice and Run
2's choice. One preclusion, and it cited the receipt Run 1 wrote. **0 false
preclusions** in the observed sequence of 5 choices.

### The controller on Amazon Bedrock AgentCore Runtime

The same pair, run through the hosted controller on a private AgentCore
Runtime under the same one-read bound. Verdict **PASS**, 25 of 25 checks.

- Run 1: history empty, `inspect_pipeline`, one inference, 1 read, a
  `disposition_unchanged` receipt, `unresolved`. Narration was refused, because
  nothing was verified.
- Run 2: history matched the exact receipt Run 1 wrote, and the preclusion
  landed before the first Decider request and before any read. One fresh
  choice, `inspect_schema_keys`, one inference, 1 read, 1 repair, `completed`.
  Amazon Bedrock Nova Lite narrated only after the deterministic result was
  fixed.
- 2 Decider answers for the pair, one per call, each accepted with HTTP 200.
  1 preclusion, 0 false preclusions.
- Two telemetry timelines of 15 and 21 events, both finalized, 0 dropped and
  0 refused.

### The one measured comparison

Basis for every row: n = 1, the same synthetic case, each proof run once. The
superseded post-choice Run 2 is a separate earlier run of the superseded flow
on the same endpoint. Client time is the caller's wall time for one runtime
invocation; selector time is the measured Decider inference time, summed over
the inferences a call made.

| Measured | Run 1, the learning call | Superseded Run 2, precluded after the choice | Run 2, precluded before selection | Difference |
| --- | --- | --- | --- | --- |
| AgentCore client time | 21,635.7 ms (21.6 s), 1 inference | 54,795.9 ms (54.8 s), 2 inferences | 26,622.5 ms (26.6 s), 1 inference | 28,173.4 ms (28.2 s), 51.4% lower, one fewer inference |
| AgentCore selector time | not compared | 52,263.79 ms (52.3 s), 2 inferences | 24,781.65 ms (24.8 s), 1 inference | 27,482.14 ms (27.5 s), 52.6% lower |
| Hosted selector time | 20,651.01 ms (20.7 s), 1 inference | 53,957.49 ms (54.0 s), 2 inferences | 25,229.29 ms (25.2 s), 1 inference | 28,728.20 ms (28.7 s), 53.2% lower |

Run 1 is unchanged by the pre-selection preclusion, because nothing was
precluded on it: one inference, one read, unresolved, receipt stored. It has no
saving and is never presented as one. What the pair shows is one fewer
inference, less occupied model time, and a lower observed Run 2 elapsed time on
this one case, at n = 1. Nothing here is a rate, and a second run of either
flow could land elsewhere.

The curated machine-readable record is
[`dist/public-demo/loop-escape-proof.json`](dist/public-demo/loop-escape-proof.json).
It carries no operational identifier, hash, version or timestamp.

## A Decision Receipt is not memory, and it is not a cache

A cache answers the question for you. Memory nudges you. A receipt tells the
system what already happened and then makes the model decide again.

The model still chooses, every time. A receipt never selects a tool, never
changes a probability, never scores a candidate, never authorizes a repair, and
never decides what is true. In Run 2 the receipt removed one proven execution
from the offer and nothing else: the three diagnostics that remained kept their
original order, and the choice among them came from Decider 2B.

Precluding takes an exact match on three things: the same capability, the same
arguments, and the same decision context, which binds the task, the query, the
admissible evidence, candidate membership and every pinned version. Any of those
moving turns the receipt into advisory history that prohibits nothing.

Two outcome classes are eligible to preclude, and no others. `no_progress`,
where the read admitted nothing, and `disposition_unchanged`, where the read
returned real data and moved no supported disposition. The second is the dead
end a model actually repeats. A read that moved a disposition can never
preclude anything, and neither can a provider failure or inadmissible evidence.

There is no expiry and no time window. A receipt is identified by the content
of its immutable binding set. Current facts dominate history: new evidence, a
changed candidate set, a changed tool contract, a changed model revision or a
changed policy each make the old receipt stop matching exactly, and it stops
precluding.

## How it works

| Role | Who holds it |
| --- | --- |
| Choose the diagnostic | Strands Decider 2B, through one typed `choice` question. The only model that selects a tool, for D1 and for an optional D2. |
| Execute the chosen read | The controller. Read-only, argument-free diagnostics only. |
| Decide what happened | The deterministic verifier, from observed facts. |
| Write the receipt | The verifier, never the model. |
| Match and preclude | The controller, on an exact context match, before the first Decider request. |
| Apply the repair | Deterministic code, authorized only by verifier-held evidence. |
| Narrate the result | Amazon Bedrock Nova Lite, after the result is fixed, holding no authority. |

Hard bounds, enforced before the call they pay for: **two Decider choices per
call** (D1 and one optional D2 re-decision) and **at most two diagnostic reads
per call**. Both loop-escape proofs configured the read bound to **one**, so a
call cannot afford the dead end and the answer. A preclusion costs no read and
no inference. The optional second decision is the design bound for a call whose
first read moves nothing while a read remains; the observed Run 2 never needed
it, because the dead end was gone before the first choice. No recursion, no
third choice, and no budget reset by renaming a tool, reordering an evidence
set, or retrying under a new identity.

A caller cannot select the scenario, the provider, the model, the tools, the
mode, or the acceptance tier. No secret is minted, stored, or read on any path,
telemetry is sanitized at the source, and evaluator truth never reaches a
runtime role or a model input. The full boundary is in
[`docs/hackathon-validation.md`](docs/hackathon-validation.md).

## Local quickstart

Python and [uv](https://docs.astral.sh/uv/). No model download, no cloud
account, no credentials, no network call.

```sh
uv venv --python 3.12
uv pip install -e .
uv run unstuck doctor --mode offline
uv run python -m unstuck.cascade.scenarios
```

The last command prints the shipped cold-start and history scenarios. Its
selector is a deterministic stand-in labelled `mock`. Its choices are
synthetic, not a model's, and it measures nothing. These are the earlier
two-read scenarios, not a reproduction of the optimized one-read proof.
The evaluation runner and private proof inputs are intentionally not shipped
in the public export. The curated proof is in
[`dist/public-demo/loop-escape-proof.json`](dist/public-demo/loop-escape-proof.json).

For the local web view of a run, install the `ui` extra and run
`uv run unstuck serve`. It binds loopback only and publishes no port.

Running the real checkpoint locally needs the `strands-decider` package and a
local snapshot, deliberately not dependencies of the shipped wheel.

## On AWS

The same pinned checkpoint, served privately, reached by the same seam. Every
hop is request-signed and scoped to one action:

- Operator to controller, on the agent runtime's invoke action: one role, one
  runtime, one endpoint.
- Controller to Decider and controller to narrator, each through an interface
  endpoint: one action on one target, and the endpoint policy allows exactly one
  principal and denies every other.
- Decider outbound: nothing at all. Network isolation on, no address
  translation gateway, no internet gateway.

The controller runs on Amazon Bedrock AgentCore Runtime. The checkpoint runs on
a private Amazon SageMaker real-time endpoint in private subnets, from a
digest-pinned private image. Narration is one Amazon Bedrock Nova Lite call.

Nothing in this repository creates, deploys, or spends. The hosting shape is
readable from the definitions themselves: the synthesis-only infrastructure in
`infra/`, the container and serving definitions in `deployment/`, and the
example configuration beside them.

## The reusable pattern

[`recipes/diagnostic-tool-selection/`](recipes/diagnostic-tool-selection/) is
the pattern on its own, for anyone who wants it in their own agent. Standard
library only: no framework, no cloud software development kit, no Pydantic, no
database, and no import from this application. Copy the directory, swap in your
catalog and your model call, then `python example.py`.

The independence is checked rather than promised. A test imports the recipe
from a copy of its own directory with nothing but that directory and the
standard library on the import path, so a hidden dependency fails a test rather
than surprising an adopter.

## How the story got here

The investigation was not tidy, and flattening it would hide the finding. Every
earlier run stays in the record with its verdict as written:

- The first local experiment is **UNVALIDATED**. The refusal never fired,
  because the outcome taxonomy classified a read by evidence novelty alone. The
  measurement located the defect, and `disposition_unchanged` was added.
- The second local experiment is **FAIL**, on one criterion frozen before it
  ran. Noticing afterwards that it measured the whole call rather than the
  mechanism did not license changing it.
- The first cloud attempt failed on a measured provider-latency cause. Three
  exits were refused, including raising a number to make a run pass, and **no
  default moved**.
- The five-case local golden run **PASSED** seven frozen criteria: 5 of 5
  cases, 10 complete pairs, 40 real selector calls, 0 false blocks.
- **Superseded as the lead claim: the post-choice pair.** An earlier run of the
  same case on the same endpoint let Decider 2B see the full option set on the
  second call. It repeated `inspect_pipeline`, the controller refused that exact
  repeat before any read, and a second Decider call chose `inspect_schema_keys`,
  which completed the call on two inferences and 54.8 s of client time. That
  result stands as written, on n = 1. Precluding the dead end before the first
  request reaches the same terminal result on one inference, which is why the
  earlier shape is no longer the lead.
- **Superseded as the lead claim: the two-read cloud pair.** Under a two-read
  bound, the first call already repaired inside one call: its dead-end D1 spent
  a read, then a same-call D2 spent the second. The second call blocked the
  repeat and spent one read. That result still holds, on n = 1 pair, but it
  proves efficiency only, one fewer read. It cannot show an escape, because
  nothing was stuck. Its record is in
  [`dist/public-demo/final-golden-summary.json`](dist/public-demo/final-golden-summary.json).

The demo page, architecture figure, pitch deck and narrated video all show the
current pre-selection sequence: Run 1 stores the receipt; Run 2 precludes the
proven dead end before the first Decider request, then completes on one fresh
choice. The video, [`dist/video/unstuck-demo.mp4`](dist/video/unstuck-demo.mp4),
combines eight current pitch frames with ten labelled offline replay steps:
147.567 seconds, 1280x720, 30 fps, 5.23 MB. All ten build gates passed without
skips, warnings or overrides. The encoded replay and timeline were inspected,
and source and artifact bindings matched before explicit local promotion.
Its honesty statement and gate report are in
[`dist/video/README.md`](dist/video/README.md). None is publicly published yet.

![Three lanes on one synthetic case. The memoryless control repeats inspect_pipeline on 3 calls and stays unresolved. Run 1 spends one inference and one read, stays unresolved in 21.6 seconds, and the verifier stores a Decision Receipt. Run 2 exact-matches the receipt and precludes the dead end before the first Decider request. The same Decider chooses inspect_schema_keys once, and verified repair completes the call in 26.6 seconds.](docs/architecture.svg)

## Limitations, and what is not claimed

- One synthetic case, one fixture, one pinned checkpoint, each proof run once.
  n = 1. No rate and no generalization to other cases, fixtures, models or
  workloads.
- The checkpoint was chosen because it repeats this dead end, not because it
  performed well.
- "Run 1 pays the learning cost once" names a verifier record and a measured
  elapsed time, not a change in the model. The checkpoint is unchanged between
  calls, and on its first exposure to the full option set it chose the dead
  end.
- This is a decision loop across calls, bounded at two Decider choices per
  call. It is not an infinite loop and no infinite loop was observed.
- The measured Run 2 latency reduction applies only to this case. On the
  hosted endpoint Run 1's one inference took 20.7 s and Run 2's took 25.2 s,
  each one observation at n = 1.
- The measured comparison sets one flow against the other on one case, each run
  once. It is an observation, not a rate and not a general result, and no cost
  effect follows from it.

Not claimed anywhere, now or later without new evidence: the model's general
accuracy, calibrated confidences, statistical significance, any rate, a general
latency benefit, a cost benefit, a cost saving, production readiness, novelty, or
compliance with any regime.

The complete record, failed runs included:
[`docs/hackathon-validation.md`](docs/hackathon-validation.md). Every claim
traces to [`docs/claim-register.md`](docs/claim-register.md), and what a public
file may carry is
[`docs/public-scrub-policy.md`](docs/public-scrub-policy.md).

## Testing

```sh
uv run pytest -q                            # the full suite
uv run python -m publishing.public_scan     # the fail-closed public leakage scan
```

Tests come before the behaviour they check for anything touching safety,
contracts, budgets, or security negatives, and no test is weakened to make work
pass. A fixture fails any test that reaches for a socket.
`tests/public_deliverables/test_public_story.py` fails if this opening drifts
from the approved story or the curated record leaks an identifier.

## Status

Not published and not submitted. Publishing and submitting are separate owner
actions and neither has been taken.
[`docs/submission-checklist.md`](docs/submission-checklist.md) records what is
met, ready, and pending.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE) for pinned
dependencies, whose licence verification is recorded as unverified and pending.
Strands Decider 2B is a separate upstream model under its own licence and terms.

---

Tags: `#strands-decider-2b` `#tool-selection`
