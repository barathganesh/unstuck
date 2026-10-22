# The model kept picking the same useless tool, so I let it fail once and then refused the repeat

Run 1 happens once: 1 inference, 21.6 s client time, unresolved, receipt stored.
Run 1 is not saved. Optimized Run 2 exact-matches that receipt before selection,
precludes only the proven dead end, and lets Decider make one fresh choice
from remaining tools: 1 inference, 26.6 s, completed.

Superseded Run 2 used 2 inferences, 54.8 s. Observed Run 2 delta: 28.2 s,
51.4% lower client latency. n = 1, same synthetic case, each proof once.

Tags: `#strands-decider-2b` `#tool-selection`

Decider 2B makes tool decisions, but a new call can repeat a decision
already proven useless. Unstuck turns that verified dead end into a Decision
Receipt, forcing the same Decider to choose a new path and escape the decision
loop, without a larger model taking over.

Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The
memoryless control stays stuck.

Decider 2B makes every choice in that story, including the one that escapes.
The receipt never picks anything. Its only power is to take one identical
execution off the table under one identical context.

## The bug

A synthetic report came back with zero rows. Somebody asks for units sold by
country, the query runs, nothing comes out, and a human works out what went wrong.

I gave that question to Strands Decider 2B: four read-only options, choose one.

It chose `inspect_pipeline`, which checks whether the upstream partition finished
arriving. It had.

So the read worked, and it was beside the point. The incident turned on whether
both sides of the join spell their country keys alike.

Then, on a new call about the same unresolved incident, it picked
`inspect_pipeline` again. And on a third.

That repetition is what I spent the project on. The first choice is a model
being wrong. The next one is a model being wrong in a way already shown not to
work. In these control calls, each new call starts fresh without a record of
what the first read found.

## What I did not want to build

The obvious fix is memory: store past decisions and feed them back.

I did not want that. If I hand the model its own past decisions, I am handing it
its own narration of what happened, and the model is the thing that got this
wrong. Once past decisions are in the input, the pressure to make them
authoritative only grows. At that point the model has stopped deciding and I
have a lookup table with a language model stapled to the front. A cache is
worse. It answers the question for you, and it is wrong on a schedule unrelated
to whether the facts moved.

What I wanted was narrower. The model keeps choosing freshly, every time, from
whatever candidates are still admissible, and exactly one thing becomes
impossible: executing the identical read, with the identical arguments, under
the identical circumstances, when verified evidence already showed that exact
execution moved nothing.

## The Decision Receipt

The verifier writes the receipts, and the verifier is deterministic Python. When
a diagnostic runs, it records two separate facts about it, and keeping them
separate turned out to be the whole design. **Evidence novelty**: did this read
admit a fact the run did not already hold? **Decision progress**: did that fact
move a disposition the system may act on?

`inspect_pipeline` scored yes on the first and no on the second. The verifier
classes that `disposition_unchanged` and writes a receipt. That receipt becomes
eligible to preclude one thing: the identical repetition.

A receipt's identity is its content, not the time it was made. There is no
expiry anywhere and no time window. This is not a cache that quietly stops being
right after ten minutes.

Preclusion needs an exact match on three things. Same capability. Same
arguments. Same decision context, which binds the task, the query, the
admissible evidence, candidate membership and every pinned version. Move any of
those and the receipt becomes advisory history that prohibits nothing. The label
alone proves nothing either: eligibility is re-derived from the two recorded
facts every time it is asked, so a receipt whose label and facts disagree
carries no authority.

## Why one read matters

Each call in the proofs below may spend one diagnostic read. That bound is the
point. A call cannot afford both the dead end and the answer.

Without receipts, Decider 2B spends the one read on `inspect_pipeline`, the read
settles nothing, and no read is left for a second choice. The call ends
unresolved.

With receipts, the proven dead end is gone from the candidate set before the
Decider is asked anything, and a candidate that is never offered costs zero
reads. The one read is still there. Decider 2B chooses from what remains and
spends it on the read that answers the question.

That ordering matters more than it sounds. Precluding before the request means
the call spends one inference instead of two, and the design bound that allows a
second Decider choice is simply never drawn on.

## The proof, with a real hosted model

One synthetic case. One pinned Decider 2B checkpoint on a private Amazon
SageMaker endpoint. Five signed Decider choices in all. Run once. Verdict PASS,
13 of 13 checks.

**The memoryless control.** Three independent calls without receipts. Each
first choice was `inspect_pipeline`, 3 of 3. Each spent its one read, 3 of 3.
None had a read left for a second choice. Each ended `unresolved` with reason
`diagnostic_reads_exhausted`, 3 of 3. Running it again did not help, and there
is no reason it should: nothing about the third call differed from the first.

**Run 1 pays the learning cost.** With receipts, the first call started with
empty history. Decider 2B was offered the four diagnostics, chose
`inspect_pipeline`, the read returned real data, and the call ended
`unresolved`, exactly like the control. One inference. The difference is what
was left behind: the verifier stored a `disposition_unchanged` receipt that
cites Run 1's own choice. The model learned nothing. The system recorded a
verified fact. This call is the cost of the mechanism, not a saving, and I never
present it as one.

**Run 2 escapes.** A new call. History matched, with an exact receipt. The
controller precluded that one proven dead end before it sent the first Decider
request, and before any read. Decider 2B was offered the three remaining
diagnostics, in their original order, and made one fresh choice:
`inspect_schema_keys`. One read. The key encodings came back, the deterministic
repair applied, verification passed, and the call ended `completed` with
`populated_after_repair`. 1 of 1. One inference, and no second Decider call.

The linkage checks all hold: Run 1's receipt cites Run 1's own decision, Run 2's
preclusion cites Run 1's receipt, the preclusion precedes the first Decider
request and precedes any read, and Run 2's choice differs from the precluded
dead end. One preclusion across 5 choices, and 0 false preclusions in that
observed sequence.

## The same pair, on AgentCore

Then I ran the pair through the hosted controller on a private Amazon Bedrock
AgentCore Runtime, under the same one-read bound and the same synthetic fixture.
One Run 1 and Run 2 pair in one session. Verdict PASS, 25 of 25 checks.

Run 1: history empty, `inspect_pipeline`, executed once, one read, one
inference, a `disposition_unchanged` receipt, `unresolved`. Narration was
refused, because nothing was verified.

Run 2: history matched the exact receipt the first call wrote. The dead end was
precluded before the first Decider request and before the first read. Decider 2B
chose `inspect_schema_keys` as its first and only choice, spent one read,
applied one repair, and ended `completed`. One inference, no second Decider
call.

Amazon Bedrock Nova Lite then wrote a short paragraph about it. Narration runs
after the terminal result is fixed, is handed counted facts rather than rows,
and is refused outright on a result that was rejected, unresolved or never
verified, which is exactly what happened to the first call.

Two Decider answers, 2 of 2 served without error, both accepted with HTTP 200.
One preclusion, zero false preclusions. Two telemetry timelines, of 15 and 21
events, both finalized with 0 dropped and 0 refused.

## One measured comparison, at n = 1

Precluding before the request removes one Decider inference from the escaping
call. I measured that once, on the same synthetic case, against the superseded
flow that blocked after the first Decider answer. Every number below is one
observation, not a rate, and the curated record carries all of them:
`dist/public-demo/loop-escape-proof.json`.

Client time is the caller's wall time for one runtime invocation. Selector time
is the measured Decider inference time, summed over the inferences a call made.

**AgentCore client time.** Run 1, 21,635.7 ms (21.6 s), one inference. The
superseded Run 2, 54,795.9 ms (54.8 s), two inferences. The pre-selection Run 2,
26,622.5 ms (26.6 s), one inference. That is 28,173.4 ms (28.2 s) apart, an
observed Run 2 client latency 51.4% lower (n = 1).

**AgentCore selector time.** The superseded Run 2 summed 52,263.79 ms (52.3 s)
of Decider inference over its two inferences; the pre-selection Run 2 summed
24,781.65 ms (24.8 s) over one. That is 27,482.14 ms (27.5 s) apart, 52.6%
lower, observed once.

**Hosted direct selector time**, measured by the experiment harness calling the
SageMaker endpoint without the runtime in between: Run 1, 20,651.01 ms (20.7 s),
one inference. The superseded Run 2, 53,957.49 ms (54.0 s), two inferences. The
pre-selection Run 2, 25,229.29 ms (25.2 s), one inference. That is 28,728.20 ms
(28.7 s) apart, 53.2% lower, observed once.

Run 1 is unchanged by any of this. It pays the learning cost once, with one
inference and one read, and it has no saving in it at all.

No dollar or cost saving is measured and none is claimed. The Decider is served
by a SageMaker real-time endpoint billed per instance-hour of uptime, not per
request, so one fewer inference does not by itself lower the bill. What the pair
shows is one fewer inference, less occupied model time, and a lower observed
elapsed time on this one case, once.

## Implementation

Hard bounds first, because everything else follows from them. Two Decider
choices per call, a first decision and one optional re-decision. At most two
diagnostic reads per call; both proofs above configured that to one. Every
allowance is charged before the call it pays for, failures included. No
recursion, no third choice, no identical-input retry, and no budget reset by
renaming a tool, reordering an evidence set, or retrying with a new identity. A
caller may only lower the bounds.

The re-decision is still in the design and still bounded at one. It exists for
the case where controller validation rejects what the Decider asked for; the
pre-selection ordering is what made it unnecessary in the proofs above, which
each used a single Decider choice per call.

Only allowlisted read-only, argument-free diagnostics are offerable. The
controller executes the read, the verifier derives the outcome from observed
facts, and the repair is deterministic and authorized only by verifier-held
evidence. The model's job begins and ends at picking one option from an ordered
list, as a typed `choice`: one of N options, each answer carrying a confidence
and a probability per option. That is what Decider 2B is built for. It is a two
billion parameter decision model that cannot generate text. It picks and it
scores, which is as much as I want a model doing near an incident.

## On AWS, and the boundary

The checkpoint runs on a private Amazon SageMaker real-time endpoint with network
isolation on, in private subnets, from a digest-pinned private image. The
controller runs on Amazon Bedrock AgentCore Runtime, and narration is one Amazon
Bedrock Nova Lite call. Every hop is signed and bounded to one action on one
resource, through interface endpoints whose policies name one principal and deny
every other, so placement in those subnets grants nothing by itself. The Decider
itself can reach nothing: no address translation gateway, no internet gateway.

A caller cannot select the scenario, provider, model, tools, mode or acceptance
tier. An extra request field is a reason-coded refusal. No secret is minted,
stored or read anywhere on the path.

## How the story got here

The proofs above were not the first runs, and the earlier ones stay in the
record with their verdicts as written.

**The first experiment is unvalidated.** Ten cases, forty terminal episodes,
sixty-one real model calls, and a qualifying subset of zero. The refusal never
fired once, so under criteria frozen before the run it claimed nothing. The
reason is the useful part. My outcome taxonomy classified a read using evidence
novelty alone, so every read that returned anything was filed as useful, and
only a read that returned literally nothing could ever be refused later. That is
backwards. The dead end is not a tool that returns nothing; it is a tool that
returns real data every time and resolves the incident never. Adding
`disposition_unchanged` as its own class was the correction. The negative
run found it.

**The second experiment is a fail.** The corrected rule was reachable and the
refusal fired on all eight pairs that reached it. But the frozen criterion asked
for two things, the refusal and one fewer diagnostic read per call. The refusal
held on seven of seven comparable pairs. The read saving held on three of seven.
My criterion measured the whole call, dominated by two unrelated confidence
floors, rather than the mechanism. Recognising that afterwards does not license
changing it. The verdict stands as fail.

**The first cloud attempt also failed.** Both invocations ended unresolved on a
provider error with zero diagnostic reads. The real controller payload, replayed
twice against the live endpoint, took 23,588.8 ms and 23,384.3 ms, against my
own 20 second budget. Three exits were refused: raise a number to make the run
pass, shrink the state text, escalate the instance. The budgets became
deployment inputs with explicit ceilings, and **no default moved**.

**Then a five-case local golden run passed** seven criteria frozen before it
ran: 5 of 5 cases, 10 pairs, 20 terminal episodes, 40 real selector calls, zero
false blocks. Local synthetic fixtures, not transferred to the cloud.

**Superseded as the lead flow: the post-D1 block.** Before the pre-selection
ordering, the controller offered the full option set on every call and refused
the repeat afterwards. On that earlier AgentCore run of the same case, D1
repeated `inspect_pipeline`, the controller blocked that exact repeat before any
read, and one D2 re-decision chose `inspect_schema_keys` and repaired, on two
inferences and 54,795.9 ms of client time. That result stands and is superseded
as the lead flow: moving the preclusion in front of the request reaches the same
verified outcome on one inference. The bound that permits a D2 re-decision did
not change; the current proofs simply do not reach it.

**Superseded as the lead claim: the two-read cloud pair.** Before either of
those, I ran one pair in the cloud under a two-read bound. Call one picked
`inspect_pipeline` at a reported 0.3067, the read moved nothing, and a same-call
re-decision picked `inspect_schema_keys` at 0.4746 and repaired, on two reads.
Call two blocked the repeat and repaired on one read. That result still holds,
on n = 1 pair. But it proves efficiency only, one fewer read. It cannot show an
escape, because the first call already repaired; nothing was ever stuck. Two
reads let one call afford both the dead end and the answer. One read does not,
and that is why the current proofs use one.

One smaller failure belongs on the record, because it is the mechanism working:
the public bundle's sanitizer failed closed the first time it ran against a
successful result, and the fix did not weaken the check.

The published visuals have not caught up. The architecture diagram, the pitch
frames, the browser demo page and the rendered video still show the superseded
post-D1 flow, with a repeat blocked after the first Decider answer and a second
Decider call behind it. They are pending a rebuild, and until it happens they
are history rather than a picture of the current sequence.

## The reusable pattern

It is extracted and standalone, in `recipes/diagnostic-tool-selection/`. Standard
library only, with no import from the application it came out of. The duplication
is deliberate: a recipe that imported my project would drag in my contracts, my
controller and my cloud adapters, and an adopter would inherit my architecture
instead of the pattern. The independence is checked rather than promised, by a
test that imports the recipe from a copy of its own directory with nothing but
that directory and the standard library on the import path.

The shape of it is small enough to restate in three lines. The only authority
history carries is negative, and it is exact: one proven execution, under one
identical context, is not offered again. The Decider still chooses, freshly,
from whatever remains. The verifier owns what is true, and nothing the model
says can promote itself into evidence.

## Limitations

One synthetic case, one fixture, one pinned checkpoint, each proof run once.
n = 1. No rate, and nothing here generalizes to other cases, fixtures, models or
workloads.

The checkpoint was chosen because it repeats this dead end on this fixture, not
because it performed well. This choice limits what the example can establish.

The model is unchanged between calls. Run 1 pays a cost and stores nothing in
the model; the receipt is a record deterministic code wrote, and it is the only
thing Run 2 has that the control does not.

This is a decision loop across calls, and each call is bounded at two Decider
choices. It is not an infinite loop, and no infinite loop was observed.

The measured Run 2 latency reduction is one observation per surface. The
comparison is n = 1 against a superseded flow on the same case; I have not
measured it on anything else.

The option wording is fitted: two framings were compared on three development
fixtures, the second was chosen, then frozen. Option order was never permuted.
Reported confidences are telemetry, not calibration.

I am not claiming the model's general accuracy, that its confidences are
calibrated, statistical significance, any rate, a general latency benefit, a cost
benefit, a cost saving, production readiness, novelty, or compliance with
anything.

## Next

Permute option order, since I never did. Run the held-out corpus, which has not
been read. Measure how often an identical repeated context actually occurs in a
workload somebody cares about, because that frequency decides whether this is
worth doing. Run more than one case. Rebuild the diagram, the frames and the
video on the current sequence.

The result I am most confident in is the smallest one. A model that picks tools
does not know what its last pick found out. Giving deterministic code verified
evidence of that, and letting it withhold exactly one identical repetition from
the next set of candidates, was enough, on this one case, for the same model to
choose a different path, without ever taking the decision away from it.

---

Tags: `#strands-decider-2b` `#tool-selection`
