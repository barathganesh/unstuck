# Hackathon validation: the loop-escape proof, and the experiments that led to it

Status: **PASS** on the one-read loop-escape proof, against the real hosted
Decider and again on the hosted AgentCore controller. Before it, in order:
**UNVALIDATED** on the first local run, **FAIL** on the second, **PASS** on the
third, a two-read cloud pair that proves efficiency only, and a post-D1
re-decision pair that the current proof replaced. The current proof comes first
below. Every earlier result follows it under "The experiments before it",
labelled superseded and not deleted.

## The loop-escape proof: the receipt acts before selection

Decider 2B can repeat a decision already proven useless on a new call. This
proof asks whether a verifier-owned Decision Receipt lets the same Decider
escape that dead end on the next call, while a memoryless control stays stuck.

Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The
memoryless control stays stuck.

### Setup

- One declared synthetic fixture and one repairable empty-report case. n = 1,
  and each proof was run once.
- The diagnostic read bound is configured to **one read per call**, below the
  absolute ceiling of two. The design bound allows at most two Decider choices
  per call: a first decision (D1) and one optional re-decision (D2). Every call
  observed here used one.
- Four diagnostics exist for this case: `inspect_pipeline`,
  `inspect_schema_keys`, `inspect_scope` and `request_review`.
- `inspect_pipeline` returns real data, that the partition arrived, and moves
  no supported disposition. The verifier classes that read
  `disposition_unchanged`.
- Run 1 and Run 2 are two separate new calls about the same incident. The
  control is three independent calls without receipts.

### The real hosted Decider proof

The experiment harness called one pinned Decider 2B checkpoint on a private
Amazon SageMaker endpoint, 5 signed Decider choices across 5 calls, run once.
Verdict **PASS**, 13 of 13 checks.

| | Control, without receipts | Run 1, with receipts | Run 2, with receipts |
| --- | --- | --- | --- |
| Calls | 3 independent | 1 | 1, a new call |
| Decision history | not requested | empty, no match | matched, an exact receipt |
| Dead end precluded before the first Decider request | no | no receipt yet | yes, 1 of 1 |
| Diagnostics offered to D1 | 4 | 4 | 3, the one proven dead end removed |
| D1 choice | `inspect_pipeline`, 3 of 3 | `inspect_pipeline` | `inspect_schema_keys`, one fresh choice |
| D2 re-decision | none | none | none, the design bound went unused |
| Decider inferences | 1 on each of 3 calls | 1 | 1 |
| Diagnostic reads | 1 of a 1-read bound, on each of 3 calls | 1 of 1 | 1 of 1 |
| Receipt | not stored | `disposition_unchanged`, stored | `useful_evidence`, stored |
| Repair applied | 0 of 3 | no | yes, and verified |
| Terminal result | `unresolved` / `diagnostic_reads_exhausted`, 3 of 3 | `unresolved` / `diagnostic_reads_exhausted` | `completed` / `populated_after_repair` |

The linkage was checked rather than assumed, and each of these held: Run 1's
receipt cites Run 1's own first decision; Run 2's preclusion cites Run 1's
receipt; the preclusion came before the first Decider request and before any
read; and the choice Decider 2B then made differs from the precluded dead end.
Across 5 Decider choices there was 1 preclusion and **0 false preclusions** in
the observed sequence.

### Why one read matters

Under a one-read bound a call cannot afford both the dead end and the answer.
The control spends its only read on `inspect_pipeline` every time, so nothing
is left to spend and all three calls end unresolved. In Run 2 the proven dead
end is removed from the offered set before the Decider is asked anything, so
Decider 2B chooses freshly over what remains and the call's one read goes to
that choice. Removing an option costs no read, and that is what separates Run 2
from the control.

"Run 1 pays the learning cost once" names a verifier record, not a change to
the model. The checkpoint is identical in both runs; what differs is the set of
diagnostics it was offered.

### The same pair on the hosted AgentCore controller

The controller ran on a private Amazon Bedrock AgentCore Runtime under the
same one-read bound, for one Run 1 and Run 2 pair in one session. Verdict
**PASS**, 25 of 25 checks.

- Run 1: history empty; the first decision chose `inspect_pipeline`, executed
  once; 1 read; a `disposition_unchanged` receipt written; `unresolved` /
  `diagnostic_reads_exhausted`. Narration was refused, because nothing was
  verified.
- Run 2: history matched the exact receipt Run 1 wrote, and the controller
  precluded that one dead end before the first Decider request and before any
  read. Over the 3 remaining diagnostics the first decision chose
  `inspect_schema_keys`, which was also the final choice; no second Decider
  call was made; 1 read; 1 repair, verified; `completed` /
  `populated_after_repair`. Amazon Bedrock Nova Lite narrated only after that
  deterministic result was fixed.
- 2 Decider answers served for the pair, one per call, each accepted with
  HTTP 200. 1 preclusion, 0 false preclusions.
- Two telemetry timelines, 15 of 15 and 21 of 21 events, both finalized, with
  0 dropped and 0 refused.

### The measured comparison, and what it is not

Each figure below is a single observation. The basis for every row is the same:
n = 1, the same synthetic case, each proof run once. The superseded post-D1
Run 2 is a separate earlier run of the superseded flow on the same endpoint.
Client time is the caller's wall time for one runtime invocation; selector time
is measured Decider inference time, summed over the inferences a call made.

| Surface | Run 1 | Superseded Run 2, post-D1 | Run 2, pre-selection | Difference |
| --- | --- | --- | --- | --- |
| AgentCore client time | 21,635.7 ms (21.6 s), 1 inference | 54,795.9 ms (54.8 s), 2 inferences | 26,622.5 ms (26.6 s), 1 inference | 28,173.4 ms (28.2 s), 51.4% lower, one fewer inference |
| AgentCore selector time | not compared | 52,263.79 ms (52.3 s), 2 inferences | 24,781.65 ms (24.8 s), 1 inference | 27,482.14 ms (27.5 s), 52.6% lower |
| Hosted direct selector time, harness to endpoint | 20,651.01 ms (20.7 s), 1 inference | 53,957.49 ms (54.0 s), 2 inferences | 25,229.29 ms (25.2 s), 1 inference | 28,728.20 ms (28.7 s), 53.2% lower |

What the pair shows is one fewer inference, less occupied model time, and an
observed Run 2 client latency 51.4% lower (n = 1) than the superseded flow's on
this one case. It is not a general claim about elapsed time.

Run 1 is not saved, has no saving, and is never presented as one. It pays the
learning cost once and is unchanged by the pre-selection preclusion: one
inference, one read, unresolved, receipt stored.

No dollar or cost saving is measured and none is claimed. The Decider is served
by a SageMaker real-time endpoint billed per instance-hour of uptime rather
than per request, so one fewer inference does not by itself lower the bill.

The curated public record carrying every figure in this section is
[`dist/public-demo/loop-escape-proof.json`](../dist/public-demo/loop-escape-proof.json),
and it is the evidence these numbers are cited from.

### Limitations of the loop-escape proof

- One synthetic case, one fixture, one pinned checkpoint, and each proof run
  once. n = 1. No rate, no statistical result, no accuracy figure, and no
  generalization to other cases, fixtures, endpoints, models or workloads.
- The measured Run 2 latency reduction is one observation at n = 1 against one
  earlier run. No general latency benefit or cost benefit is claimed.
- The model did not learn anything. The verifier recorded a fact, and the
  controller removed one proven dead end from one offered set.
- This is a decision loop across calls, bounded at one read and at most two
  Decider choices per call. It is not an infinite loop, and no infinite loop
  was observed.
- The checkpoint was chosen because it repeats this dead end, not because it
  performed well.
- The published visuals and the video still show the superseded post-D1 flow:
  `docs/architecture.svg`, `docs/demo.html`, the pitch deck under
  `submission/pitch/` and the video artifacts. They are pending a rebuild
  against this sequence and are not current.

## The experiments before it

Everything from here down is history. Each result is labelled and kept rather
than rewritten, and none of it is the current lead claim.

### Superseded: the post-D1 re-decision proof

**Superseded** as the lead claim by the pre-selection proof above, and valid as
written. It ran the same synthetic case against the same hosted endpoint and
the same controller, under the same one-read bound, once.

In its Run 2 the first decision was offered the full set of four diagnostics,
repeated `inspect_pipeline`, and controller validation refused that exact
repeat before any read. One D2 re-decision then chose `inspect_schema_keys`
over the remaining options, the repair was applied and verified, and the call
ended `completed` / `populated_after_repair`. That Run 2 spent two Decider
inferences where the current Run 2 spends one.

What changed is only when the receipt acts. The preclusion now lands before the
first Decider request rather than after it, so the second Decider call the
superseded flow needed is no longer spent. The design bound that permits one D2
re-decision per call is unchanged and still applies: a first read that executes
and moves no supported disposition can open one re-decision if a read remains. A
precluded capability is simply not among the options a decision is offered, so
it cannot be chosen, and a request that named one would be refused rather than
executed.

Its measured times are in the comparison table above, in the
"Superseded Run 2, post-D1" column.

### The three local runs, in order

The first two runs and their verdicts stand exactly as written below. They are
not reinterpreted by the third, and the third does not retroactively rescue
either: it is a narrower experiment, on a narrower cohort, under definitions the
first two did not use. Read the two earlier sections first, then
"The third run" at the end.

The first run could not reach the mechanism it was built to test, and the
measurement located the reason: a defect in the rule, not a gap in the cases.
The rule was corrected and the same experiment was run once more. The mechanism
was reached, it behaved as designed on every pair that exercised it, and one of
the criteria frozen before the first run still did not hold. Under that frozen
rule the verdict is FAIL, and that is what is reported. No threshold, case or
criterion was moved to reach a better one.

This is a bounded pilot on synthetic fixtures with an offline local model. No
figure here is a benchmark result, a calibration, a latency or cost claim, or a
statement about the model's general accuracy.

## Why the hack was started

The project asks one narrow question. A model tool selector sometimes repeats a
choice that has already been shown, by verified evidence, to make no progress on
exactly the current problem. The idea under test is that a deterministic
controller can keep a verifier-owned record of what each past diagnostic read
actually produced, hand a bounded set of matching records back to the selector
as labeled history, and refuse only the identical repetition that verified
evidence already proved fruitless. The selector still decides freshly every
time. History is evidence, never a replayed answer.

The hypothesis for this run, written down before the cohort was read or run:

1. The first call exposes a repeatable no-progress selection.
2. The verifier writes an exact no-progress Decision Receipt for it.
3. On a second, genuinely new call, the memory arm hands the selector the
   current state plus that bounded history, refuses the identical repetition
   before it executes, and opens exactly one second decision.
4. Compared with the memoryless arm, the memory arm keeps the same verified
   outcome while spending one fewer diagnostic read.

## The first run: what was tested

- Two arms only: one without cross-call memory, one with it. Everything else is
  shared: the same task, the same canonical initial state, the same tool cards,
  the same controller, the same deterministic verifier, the same budgets, the
  same two diagnostic opportunities.
- Ten cases from the frozen development corpus, declared before the run in
  declared order. The held-out corpus was not read and not run.
- Two ordered calls per arm and case, each pair in its own isolated namespace
  and its own store, the second call always a genuinely new invocation rather
  than an idempotent resume.
- Every selection answered by one real local model checkpoint, pinned by its
  immutable revision, through the application's own typed Choice request and
  validation path. No scripted substitute, no fallback order, and no network
  call of any kind.

### Denominators

| quantity | value |
| --- | --- |
| cases | 10 (8 of which exercise the selector at all) |
| arms | 2 |
| pairs | 20, all complete |
| episodes | 40, all terminal, all reconciled against the 40 declared identities |
| real selector calls | 61 |
| paired comparison keys | 10 |
| qualifying pairs for the hypothesis | **0** |

Two of the ten cases are the declared healthy class: the first report already
answers, so they never call the selector. Three second-pass calls never happened
because the first-pass answer fell below the frozen acceptance threshold, which
ends the incident immediately.

## The first run: what changed between the two calls

In the memory arm, every one of the eight selector-exercising pairs behaved as
designed on the second call: the record written during the first call was
retrieved, classified as an exact match for the current decision context,
carried into the selector input as labeled history, and the selector still made
its own fresh choice. No record selected a tool, authorized a repair, or decided
what was true.

What did not happen is the refusal. Not one first call produced a no-progress
record, so there was nothing a second call could refuse.

## Whether the hypothesis held on the first run

It did not hold, and it was not refuted on its own terms either: step 2 never
occurred, so steps 3 and 4 were unreachable. Under the criteria frozen before
the run, an empty qualifying subset is reported as UNVALIDATED.

The cause is structural, and it is the useful finding of this run. In the real
controller a diagnostic read admits a fact the run did not already hold, so the
verifier's own evidence delta is never stalled and its closed taxonomy cannot
classify the read as no-progress. The no-progress path is reachable in the
offline scenario harness only because a scripted execution authority can report
that nothing changed. The refusal mechanism is therefore implemented, tested and
correct, but unreachable on this corpus through the real evidence path.

Measured side by side, with the same verified terminal outcome on all ten
comparison keys:

- pairs where memory spent one fewer diagnostic read: **0 of 10**
- pairs where memory spent one more: 4 of 10, none of which improved the outcome
- terminal regressions: **0**
- refusals that were not backed by an exact no-progress record: **0**
- episodes exceeding two selector calls or two diagnostic reads: **0**

The extra reads have an honest explanation rather than a flattering one. The two
arms' first-call inputs are not byte-identical: the memory arm performs the
bounded lookup and reports an empty history where the other arm reports that no
history was requested. That single declared difference moved three first-pass
confidences across the frozen threshold, which is the whole of the read-count
difference between the arms on this cohort.

## Model behaviour on the first cohort

Reported because it bounds what the experiment could show, not as an accuracy
claim:

- Every first-pass call chose the same tool, and every second-pass call chose
  the same other tool. The selector did not discriminate between case classes.
- All 61 answers were strictly valid against the offered options: zero malformed
  answers, zero transport failures, zero timeouts.
- Model-measured call latency over 61 calls: about 1.5 s minimum, 1.9 s median,
  3.4 s maximum, against a 20 s provider budget. This is one local machine with
  one process and is not a served latency.
- The checkpoint was chosen for this pilot **because** it exhibits a repeatable
  blind spot, not because it performed well. Its measured development tool
  correctness is below random expectation.

## What the first run framed as a corpus gap

The first run framed this as a gap in the corpus: the refusal needed a case in
which a diagnostic read genuinely admits nothing. Reading the code again
against the measurement showed that framing was wrong, and the defect was in
the rule rather than in the cases.

## The corrected rule

The implementation had already recorded two separate facts about every read.
*Evidence novelty* is whether the read admitted a canonical fact the run did
not already hold. *Decision progress* is whether that fact moved a supported
disposition. The outcome taxonomy then used only the first of them, so every
read that returned anything was classified as useful, and only a read that
returned nothing could ever be refused on a later call.

That is backwards for the problem under test. A real diagnostic almost always
returns something. The selector's blind spot is not a tool that returns
nothing; it is a tool that returns real data every time and resolves the
incident never. Under a single-dimension taxonomy that choice is unrefusable by
construction, which is exactly what the first run measured.

The correction adds one outcome class for the combination that previously had
nowhere to go. A read that admitted real, admissible evidence and moved no
supported disposition is now its own class, distinct from a read that admitted
nothing and distinct from a read that made progress. Only those two
disposition-negative classes are eligible to refuse an exact repetition.
Nothing else changed:

- A read that moved a supported disposition can never refuse anything.
- A provider failure and inadmissible evidence keep their own classes and are
  never relabelled as either blocking class.
- Eligibility is still not a refusal. A refusal needs an exact match on the
  identical capability, the identical arguments, and the identical decision
  context, which binds the task, the query, the admissible evidence, candidate
  membership, and every bound version. Any of those moving makes the receipt
  advisory history, so current authoritative facts still dominate.
- The class label alone proves nothing. Eligibility is re-derived from the two
  recorded deltas, so a stored record whose label and recorded facts disagree
  carries no authority.
- A record written before this correction stays readable and stays
  non-blocking, so nothing already stored acquires authority it was not written
  with.
- The selector is still the only thing that selects. History never replays a
  choice, changes a probability, authorizes a repair, or decides what is true.

## Why the first experiment is reported as unvalidated rather than repaired

The first run's verdict stands as written. Its qualifying subset was empty
under the criteria frozen before it ran, so it claimed nothing, and it is not
retroactively reinterpreted here. What it produced was a measurement that
located a defect in the rule, which is the useful result of a negative run.

The corrected rule is a change to the mechanism under test, so the experiment
was re-run once on the identical definitions: the same pinned checkpoint and
revision, the same two arms, the same ten declared cases in the same declared
order, the same frozen pilot thresholds, the same denominators, and the same
predeclared pass and fail criteria. No threshold was moved, no case was added
or dropped, and no criterion was changed after a result was seen.

## The Decider fine-tune experiment

The versioned evidence records [`decider-finetune-baseline-v1.json`](../dist/public-demo/decider-finetune-baseline-v1.json), [`decider-finetune-head-only-v1.json`](../dist/public-demo/decider-finetune-head-only-v1.json), and [`decider-finetune-lora-head-v1.json`](../dist/public-demo/decider-finetune-lora-head-v1.json) preserve the measured comparison. This was an offline synthetic test of controlled variation, in-distribution, with one seed, four test scenario families, 160 choice rows, and 64 integrated calls that replay 64 of those row answers through the controller. The test families are disjoint from training by name and fixture, but all 20 model-visible test situations occur in training. The 80 rows under a renamed tool catalog that never appears in training or calibration are the only new surface.

The baseline scored 97/160 first-choice correctness, 20/64 integrated wasted first reads, and 44/64 verified completions. Head-only moved correctness to 107/160 but left wasted first reads at 20/64, so the harness refused it on that criterion.

LoRA-plus-head moved correctness to 129/160, with 26 of the 32 gains on production-catalog rows that differ from training prompts only in dataset identity; on the renamed-catalog slice it moved from 64/80 to 70/80. Integrated wasted first reads moved from 20/64 to 15/64: five improved calls and none worse, one seed, four of the five in one family, exact paired p = 0.0625. Verified completions moved from 44/64 to 49/64 on the same five calls, with zero paired completion regressions. False repairs and false preclusions were 0 for the baseline and for LoRA-plus-head, but those errors were structurally unreachable in these families, so the zeros are not safety evidence. ECE, measured with the project's normalized confidence, is descriptive only and no calibration gate was applied: 0.165 baseline, 0.408 head-only, 0.166 LoRA-plus-head. NLL moved from 0.930 to 0.373. Decision Receipts remain the deterministic hard boundary and Decider 2B remains the only model tool selector.

These results do not claim production behavior, hosted latency, or dollar improvement. A private hosted canary was run later and missed its pre-registered exact-choice parity gate, so no hosted result is claimed. The full research record, including the audit limitations and the later viability stop, is [`docs/research/decider-adaptation-findings.md`](research/decider-adaptation-findings.md).

## The second run: what it measured

Same denominators as the first run, with one fewer selector call because more
first-pass answers fell below the frozen acceptance floor:

| quantity | first run | second run |
| --- | --- | --- |
| cases | 10 (8 exercise the selector) | 10 (8 exercise the selector) |
| arms | 2 | 2 |
| pairs, all complete | 20 | 20 |
| episodes, all terminal | 40 | 40 |
| real selector calls | 61 | 58 |
| paired comparison keys | 10 | 10 |
| qualifying pairs | **0** | **8** |

The mechanism was reached on every pair that could reach it. On all eight
qualifying pairs the first call recorded a read that returned real data and
moved nothing, the second call's selector chose that identical tool again, and
the controller refused it before it ran:

- blocked repetitions: **8 of 8** qualifying pairs
- refusals not backed by an exact matching record: **0**
- each refusal consumed **zero** diagnostic reads and opened **exactly one**
  second decision
- terminal regressions: **0**
- episodes exceeding two selector calls or two diagnostic reads: **0**
- records that selected a tool, changed a probability, authorized a repair or
  decided what was true: **0**

## Whether the hypothesis held on the second run

It did not, on one criterion, and the criterion is reported as frozen rather
than reinterpreted.

The frozen criterion asked that on every qualifying pair reaching the same
verified terminal outcome, the memory arm both refuse the repetition **and**
spend exactly one fewer diagnostic read than the other arm on the second call.
Seven of the eight qualifying pairs reached the same terminal outcome. The
refusal held on all seven. The read saving held on **three of seven**.

The four that did not divide cleanly into two causes, and neither is a fault in
the refusal:

- On two pairs the freed read was immediately spent by the second decision,
  which was accepted and ran its own tool. The refusal redirected the read
  rather than removing it, so the total for the call is unchanged.
- On two pairs the comparison arm never spent a second-call read at all: its
  own first answer fell below the frozen acceptance floor and ended the
  incident. Against a baseline of zero there is nothing to save.

The honest reading is that the criterion measured the wrong quantity. "The
refused execution costs no read" is a property of the mechanism and it held
everywhere. "The arm's total read count is one lower" is a property of the
whole call, and on this cohort it is dominated by whether each arm's answers
clear two fixed confidence floors. That comparison cannot isolate the refusal,
and the criterion assumed it could. Recognising that after the fact does not
license changing it, so the verdict stands as FAIL.

One pair is excluded from that subset by the same-outcome rule, and the
direction it moved is worth recording: the memory arm ended in a more resolved
state than the comparison arm, not a worse one.

## Model behaviour on the second cohort

Reported because it bounds what the experiment could show, not as an accuracy
claim:

- Every first-pass call chose the same tool and every second-pass call chose
  the same other tool, exactly as in the first run. That non-discrimination is
  why the blind spot is reproducible at all.
- All 58 answers were strictly valid against the offered options: zero
  malformed answers, zero transport failures, zero timeouts.
- Model-measured call latency over 58 calls: about 1.4 s minimum, 1.8 s
  median, 2.7 s maximum, against a 20 s provider budget. One local machine,
  one process; not a served latency.
- Reported confidence spread was wider than the first run's on both passes.
  The cause is declared rather than smoothed over: the corrected rule changes
  what the selector is shown, because the history it carries names the new
  class and a refused pass carries the refusal. The prompt is therefore not
  byte-identical to the first run's. This is the same class of confound the
  first run recorded between its own two arms, and it is precisely why a
  total-read-count comparison against a threshold-sensitive baseline is a poor
  test of the refusal.
- The checkpoint was chosen for this pilot **because** it exhibits a
  repeatable blind spot, not because it performed well. Its measured tool
  correctness on the development set is below random expectation, and this
  cohort's own first-pass tool correctness was zero.

## Limitations, and what is not claimed

- Synthetic fixtures, one local machine, one offline checkpoint, ten
  development cases, two runs. Nothing here generalizes.
- The acceptance thresholds used are a pilot floor derived from development
  observations so that the mechanism could be reached at all. They are labeled
  inadequate, they are not calibration, and they are not the product defaults.
- The harness labels these episodes synthetic because no live provider boundary
  was observed. That label means there was no live cloud provider call; it does
  not mean no model ran. A real checkpoint answered all 61 calls of the first
  run and all 58 of the second.
- No claim is made about general model accuracy, calibration, statistical
  significance, latency benefit, cost benefit, production readiness, or contest
  submission readiness. In particular, no saving is claimed: the frozen saving
  criterion did not hold, and the three pairs where the read count did fall are
  reported as measured rather than as a result.
- The second run's refusal is exercised on one checkpoint against one corpus.
  That it fired on every pair that reached it says the rule is now reachable
  through the real evidence path; it says nothing about how often a real
  workload would present an identical repeated context.
- Raw model output, run snapshots and machine-local paths are kept out of this
  summary and out of every published artifact.

## The third run: the golden path, end to end

Status for this run: **PASS**, on all five cases, against seven criteria frozen
before it started.

This run is the narrow one. The second run proved the refusal fires and
measured that its read saving could not be isolated, because two unrelated
gates ended most episodes before the redirected decision could do anything.
The third run asks the single question those gates were hiding: when the
refusal redirects the decision, does the redirected decision actually fix the
incident, and does it cost one read less?

### What one case looks like, start to finish

The same sequence held on all five cases:

1. The report returns zero rows.
2. The first decision picks a diagnostic. It runs, and it returns real data:
   the requested partition has arrived and covers the requested range.
3. That real data moves no supported disposition. It cannot: nothing about
   partition readiness decides whether the two sides of the join spell their
   keys the same way, which is what this incident turns on. The deterministic
   verifier records the pair of facts it observed and classifies the read as
   disposition-neutral.
4. The second decision picks a different diagnostic, which observes the
   declared key encodings. That evidence opens the supported repair, the
   controller applies it deterministically, and verified rows appear.
5. On a second, genuinely new call, the first decision picks the same
   unproductive diagnostic again.
6. The memory arm refuses that exact repetition before it executes, backed by
   the receipt the first call wrote, and opens exactly one second decision.
   That decision again picks the key-encoding read, and the repair again
   produces verified rows.
7. The comparison arm, which has no cross-call memory, runs the unproductive
   read again and then reaches the same repair. Same verified rows, one more
   diagnostic read.

The two things being distinguished here are the point of the whole project. The
model chose freshly on every one of those decisions. The record did not choose
for it, score for it, or authorize the repair; the only thing it did was refuse
one identical repetition that verified evidence had already shown to be
fruitless.

### Denominators

| quantity | value |
| --- | --- |
| cases | 5, every repair-class case in the development split |
| arms | 2 |
| pairs | 10, all complete |
| episodes | 20, all terminal, all reconciled against the 20 declared identities |
| real selector calls | 40 |
| provider replies | 40 |
| stored receipts | 15 |
| largest selector payload | 5,162 characters against an 8,000 bound |

No held-out case was read or run.

### The seven frozen criteria, and whether each held

All seven were written down before the run and none was changed after a result
was seen.

| criterion | result |
| --- | --- |
| the first call records a disposition-neutral receipt for the first decision's read, and it is the same read on every case | 5 of 5 |
| on the second call the first decision selects that identical read again | 5 of 5 |
| the memory arm refuses it before it executes, backed by an exact matching receipt | 5 of 5 |
| the second decision independently selects the key-encoding read | 5 of 5 |
| both arms finish with the same verified terminal outcome, and that outcome is verified repaired rows | 5 of 5 |
| the memory arm's second call spends exactly one fewer diagnostic read | 5 of 5 |
| zero false blocks, regressions, malformed answers, provider failures, timeouts or bound violations | all zero |

The refusals were checked against each run's own refusal event and the stored
receipt that event names, not against a summary field: on every case the
receipt that refused the repetition is the receipt the first call wrote, and
its own two recorded deltas prove the disposition-neutral conjunction rather
than merely carrying the label. One further check was added after the run,
which can only make the verdict harsher: the comparison arm must *not* refuse,
and on all five cases it requested no history and repeated the read.

Where the read saving comes from is worth being exact about, because the second
run's failure was precisely that this could not be isolated. Both arms spend
two reads on the first call. On the second call the comparison arm spends two
and the memory arm spends one, and the one it does not spend is the refused
execution. The refusal costs no read and the redirected decision reuses the
read the refusal freed.

Verified rows were compared against the evaluator's expected digest only after
every episode was confirmed terminal, through one handle per case. They matched
on all five. No evaluator fact entered a model input, a receipt, telemetry or
this summary.

### What changed in the system to make this reachable

Two changes, and they fix two different things the second run had tangled
together into one number.

The first is acceptance. A single confidence floor was doing two unrelated
jobs: it was the product's risk default, and it was also the only thing between
a well formed answer and an argument-free local read. The second run's
threshold rejected most selections before any diagnostic ran, which is why its
episodes ended unresolved. Acceptance now has an explicit tier. The strict tier
is unchanged, is still the product default, and still requires the configured
floor. A second tier accepts a structurally valid answer naming an available,
argument-free local read whatever confidence it reported, and it is refused
outright unless every executable option offered is read-only and argument-free.
Every structural refusal still refuses in both tiers: a malformed answer, a
transport failure, a late answer, the review option, an unavailable option, a
wrong revision, a wrong option set, and an invalid distribution are all refused
exactly as before. The tier is part of the frozen policy, so it enters the
decision context every receipt is bound to and a receipt written under one tier
cannot match a decision made under the other.

The second is how the options are described. Each option's criterion stated
what the diagnostic reads and nothing about when reading it would distinguish
anything, so for the question actually asked no option answered it. Every
published tool card now carries one condition sentence saying when observing
its facts separates the remaining explanations, and the rendered criterion ends
on that condition. The sentence is part of the public card, so a renamed catalog
publishes renamed conditions, and it names no other tool, no case class, no
category, no expected outcome and no evaluator fact.

Neither change touches who is allowed to do what. Only allowlisted read-only
diagnostics are offerable at all; the controller executes the read; the
verifier derives the outcome from observed facts; the repair is deterministic
and authorized only by verifier-held evidence; two selector calls and two
diagnostic reads remain hard bounds; and a receipt's only authority is still
negative and still requires an exact current-context match.

### Model behaviour on this cohort

Reported because it bounds what this run could show, not as an accuracy claim:

- All 40 answers were strictly valid against the offered options: zero
  malformed answers, zero transport failures, zero timeouts.
- Reported confidence spanned 0.1946 to 0.6676. Every accepted selection was
  below the product's default floor. That is the point of the read-only tier
  and it is not a statement that the number is meaningful.
- Model-measured call latency over 40 calls: about 1.8 s minimum and 2.9 s
  maximum, against a 20 s provider budget. One local machine, one process; not
  a served latency.
- The first decision chose the same diagnostic on every call and the second
  chose the same other one. That non-discrimination is why the blind spot is
  reproducible at all, and it is the same checkpoint property the earlier runs
  recorded.

### Limitations, and what is not claimed

- Synthetic fixtures, one local machine, one offline checkpoint, five
  development cases, one run. Nothing here generalizes.
- The option wording was selected on development data. Two framings were
  compared on three development fixtures before the cohort was run, the second
  was chosen, and then it was frozen and not touched again. That makes the
  framing fitted to development observations, exactly as the thresholds in the
  earlier runs were, and it is reported as such rather than as a neutral
  choice. Option order was not permuted in either framing.
- The read-only acceptance tier is not calibration and is not the product
  default. It is sound here only because the entire offerable surface is
  argument-free read-only observation inside a two-read bound, so the worst
  outcome of a wrong accept is one local read the verifier then classifies.
  That reasoning does not transfer to any action that changes state, and the
  tier is structurally unable to apply to one.
- Reported confidence is recorded as observable telemetry. No claim is made
  that it is calibrated, in either tier.
- The checkpoint was chosen because it exhibits a repeatable blind spot, not
  because it performed well.
- The two arms' first-call inputs are still not byte-identical, for the reason
  the first run declared: the memory arm performs the bounded lookup and the
  other reports that no history was requested. On this cohort both arms made
  the same choices on the first call and spent the same two reads, so the
  second-call difference is the refusal rather than that confound, but the
  confound has not been removed.
- Five cases of one class is a demonstration that the mechanism works end to
  end on that class. It says nothing about how often a real workload presents
  an identical repeated context, and it is not a benchmark result.
- No claim is made about general model accuracy, calibration, statistical
  significance, latency benefit, cost benefit, production readiness, or contest
  submission readiness.
- The harness labels these episodes synthetic because no live provider boundary
  was observed. That label means there was no live cloud provider call; it does
  not mean no model ran. A real pinned checkpoint answered all 40 calls.
- Raw model output, prompts, run snapshots and machine-local paths are kept out
  of this summary and out of every published artifact.

## The production seams, and why they are not evidence

Status: **implemented locally, not deployed.** This section describes code that
exists and tests that pass offline. It describes no run, no measurement, and no
AWS resource. Nothing in it changes any number above, and nothing in it is a
claim about latency, cost, throughput, availability, or accuracy.

### What was added

Two seams, for the already-passing golden path:

1. A hosted Decider adapter. The pinned checkpoint moves from a local process
   to a private SageMaker real-time endpoint, behind the same
   `unstuck.cascade.choice` seam the golden path already used. The payload is
   built by the shipped `choice_request` and validated by the shipped
   `validate_choice`, so the criteria, the option order, the bounded state
   text, and the acceptance shape are the same ones that produced the evidence
   above.
2. A narrator. After the controller and the verifier finish, one Amazon Nova
   call writes one short paragraph about a bounded sanitized projection of the
   terminal result.

### Why neither is evidence

The golden path's five cases were run against a local checkpoint on `mps`, in
one process, offline. Moving the same checkpoint behind an endpoint changes the
transport and nothing about the decision, but it changes the transport, and a
transport that has never been exercised has produced no observation. Concretely:

* No endpoint exists, so no selector call has been answered over one. Every
  provider latency number above is the local one and stays that way.
* No Bedrock call has been made, so no narration has ever been produced. The
  narrator's own tests drive injected stubs.
* No image has been built or pushed and no model artifact has been packaged, so
  neither container has ever started.
* The provider-boundary gate is unmet for the same reason it was unmet before: no provider boundary
  event has been observed from a deployment. The adapter's classification stays
  `synthetic`.

### One thing a reader should not infer

The golden-path evidence was produced under acceptance tier
`read_only_structural`, and the checkpoint reported confidences between 0.1946
and 0.6676 on that cohort. The product default is `strict_confidence` with a
0.80 floor, and that default is unchanged by this work. A hosted deployment
left on the default would reject those selections rather than execute them, so
the hosted path is **not** a configuration under which the seven frozen
criteria above have been shown to hold. Which tier a hosted deployment runs is
the owner's recorded decision, and reproducing the golden path on a deployed
endpoint would be a new run requiring its own frozen plan and its own gates.

### What narration cannot affect

Narration holds no authority and is structurally incapable of reaching any
number above. It runs after the terminal result is fixed, it is handed counted
facts rather than rows, it is given no tool configuration, and it is refused
outright on a result that was rejected, unresolved, or never verified. The
reported confidences are not in its projection at all, so it cannot present a
confidence as correctness. A narration failure leaves the deterministic result
exactly as the controller produced it and is reported as
`narration_unavailable`.

### Where to look

* `docs/agentcore-deployment.md`, "The real provider path": configuration, the
  security boundary, the infrastructure shape, rollback, and the full list of
  unresolved deployment inputs.
* `deployment/sagemaker/README.md`: the container contract, the model artifact
  packaging step, and why the artifact must carry the base model as well as the
  adapter.

## Superseded: the first cloud deployment, and what it answered

Superseded as the lead claim by the loop-escape proof at the top of this file.
This is the earlier two-read pair, and everything in this section describes the
superseded post-D1 flow rather than the current sequence. It is still valid,
and it proves efficiency only: one fewer read on n = 1 pair. It cannot show an
escape, because under a two-read bound the first call already repaired inside
one call, so nothing was stuck.

Status: **DEMONSTRATED**. The production seams described above are no longer
offline-only. They were deployed to the owner's own private account on
2026-10-21, a real pinned checkpoint answered real requests from a private
endpoint, and on the second attempt that day the two-call golden recovery
reproduced end to end in the cloud. The first attempt did not, for a measured
reason that is kept below rather than deleted, because the fix it led to is
part of the result.

Nothing in this section changes any number in the three runs above. Those are
local offline measurements on synthetic fixtures and they stand exactly as
written. This section does not transfer them to the cloud and does not claim
the cloud reproduced them; the cloud run is a separate measurement with its own
much smaller denominator, stated with it.

Account-scoped identifiers are deliberately absent here. The write record,
every raw provider response, the invocation bodies, the applied policies and
the rollback records are part of the private cloud-run record and are not
published. `docs/agentcore-deployment.md`, "The first real deployment",
carries the full deployment detail.

### What the cloud run established

| Claim | Evidence |
| --- | --- |
| The pinned checkpoint serves from a private endpoint | one signed direct call, HTTP 200, answer strictly valid against the shipped contract, the answer's reported checkpoint revision equal to the one pinned revision, the call identity echoed back, choice one of the offered options, 4,645 ms end to end and 3,798 ms of inference |
| The real provider path is wired, not simulated | every controller invocation routed to the hosted Decider; the deterministic stand-in did not answer, and the telemetry labels `decider` from the first real answer onward |
| **The two-call golden recovery runs in the cloud** | two fresh signed calls in one session, both `completed` with `populated_after_repair` and `verification_status: passed`; call one with empty history spent two diagnostic reads, call two with matching receipt history spent one |
| **An exact-context repeat is detected and prevented, live** | in call two, history matched, D1 chose `inspect_pipeline` again, and controller validation blocked that exact choice (`blocked_reason_code: insufficient_evidence`) before execution; one D2 re-decision then selected `inspect_schema_keys` |
| **Decider 2B remains the only model tool selector** | both the first and the re-decision came from the hosted checkpoint; the receipt supplied history as references and selected nothing |
| Idempotency holds under a repeated key | the repeat of call two's key answered `duplicate` from the durable record in 1,030 ms, returned a byte-identical result under the same run identity, acquired nothing, executed nothing, and emitted no second telemetry timeline |
| The serving layer answered every call in time | the endpoint's own log stream shows four `POST /invocations` returning **200** and **zero** 500s over the window, with `/ping` 200 throughout |
| Telemetry stays sanitized in a real account | 49 `UNSTUCK_TELEMETRY_V1` lines over the window, two run timelines of 25 and 22 contiguous events, both finalized with `dropped: 0` and `refused: 0`, the pinned checkpoint revision travelling only as a digest with the raw revision absent, and all eighteen forbidden-value checks absent, including the account identifier, every resource identifier, region literals, credential patterns, machine local paths, tracebacks, request keys, caller identity and the narration prose |
| Narration is reported beside the result, never inside it | both runs `narrated` by Nova Lite in 973 ms and 791 ms, with the result object carrying no narration field |
| Anonymous access is denied on every path, verified before every signed call | three unsigned AgentCore probes and one unsigned SageMaker probe, each a 403 identifiably from the service's own SigV4 gate rather than a bare 403 |

### What blocked the first attempt, and what cleared it

The first attempt that day ended with both invocations `unresolved` on
`provider_error_decider` and **zero** diagnostic reads. The cause was measured
rather than inferred, and it was a budget rather than a fault: the endpoint
stayed healthy throughout and refused with a reason-coded
`serve_inference_failed`, never a traceback.

A 278-character hand-written state answers in 3.8 s. The real controller sends
**3,321 state characters with four options** and 924 characters of criteria.
That exact payload, replayed against the live endpoint twice with a 180 s
client timeout, took **23,588.8 ms and 23,384.3 ms**. Latency rises roughly
linearly with state size on this instance: 4.2 s at 300 characters, 10.5 s at
2,000. The SageMaker service limit of 60 s was met comfortably. What was
exceeded was the product's own 20 s budget, which appeared twice: as
`MAX_ANSWER_SECONDS` in the model server and as `MAX_PROVIDER_MS` in the
controller's Decider adapter.

Three exits were available and all three were refused at the time: raising
either number to make a run pass, shrinking the state text, or escalating to
GPU or a larger instance. The measurement was recorded and the decision was
left to the owner.

The owner then directed that both budgets become deployment inputs rather than
constants, on the reasoning that how long a hosted checkpoint takes to answer
is a property of the instance type it is served on rather than of the product,
and raised the run allowance to two minutes. Two variables now carry it:

* `UNSTUCK_DECIDER_TIMEOUT_SECONDS` on the controller, bounded at 60 s, which
  is the 120 s run ceiling halved because D1 and D2 are the only two Decider
  calls a run may make.
* `UNSTUCK_SERVING_TIMEOUT_SECONDS` on the model server, bounded at the same
  120 s run ceiling, because no single answer may outlive the run waiting for
  it.

**No default moved.** Both ship at 20 s, an absent variable keeps 20 s, every
offline path keeps the budget it had, and every local number above is
untouched. What changed is the ceiling a deployment may name. Both readers
accept a plain positive decimal and nothing else; `nan`, `inf`, `1e400`,
`6_0`, `0x3c`, `60s`, `0` and anything past the ceiling refuse at startup,
before a listener exists. Neither value is reachable from a request.

This is the honest shape of the result: the first attempt failed on an
instance-specific budget baked into an image, and the fix was to stop baking
it in, not to widen an invariant until a run passed. The 60 s actually
deployed is still a cap: a 65 s answer at that setting is still discarded.

### Four judge-facing facts, with their denominators

From the cloud run. The denominator is **one pair of calls**. These are
existence results, the mechanism working against a real hosted checkpoint in a
real account, and nothing here is a rate, a statistical result, or a
generalization.

| Fact | Value | Denominator |
| --- | --- | --- |
| Repeated bad choices detected | 1 | 1 of 1 call that had matching receipt history; the first call's history was empty by construction |
| Repeated executions prevented | 1 | 1 of 1 exact-context repeat; the blocked choice never executed |
| False blocks | 0 | 4 Decider answers across 2 runs, 2 first decisions and 2 re-decisions; the only block was on an exact-context repeat |
| Extra decisions, read delta | 1 fewer read | the call with empty history spent 2 reads, the call with matching history spent 1. **n = 1 pair.** |

Those are now zeros and ones of measurement rather than of absence. The read
delta in particular is a single observation and is reported as one; the
five-case local result below is the one with a denominator worth reading.

From the local golden run, which stands on its own and is not a cloud
measurement:

| Fact | Value | Denominator |
| --- | --- | --- |
| Repeated bad choices detected | 5 | 5 of 5 cases, 10 complete pairs, 20 episodes, 40 selector calls |
| Repeated executions prevented | 5 | 5 of 5 memory-arm second calls blocked the repetition before execution |
| False blocks | 0 | 40 selector calls |
| Read delta | 1 fewer read, 5 of 5 cases | the comparison arm spent 2 where the memory arm spent 1 |

The cloud pair agrees in direction with the local five. One pair agreeing with
five cases is corroboration, not confirmation.

### Latencies, kept apart

Each component is reported separately and never summed into an end-to-end
claim, because they were measured under different conditions.

| Component | Measurement |
| --- | --- |
| Decider inference, 278-char state | 3,798 ms server-reported |
| Decider inference, the real 3,321-char four-option payload | 23,589 and 23,384 ms, measured under the 20 s cap it exceeded |
| SageMaker transport overhead on the signed direct call | 847 ms, end to end minus server-reported |
| Controller invocation, end to end, two Decider calls plus reads plus narration | 52,572 ms with 2 reads, 60,004 ms with 1 read |
| Controller invocation, measured at the client | 54,277 ms and 61,052 ms |
| Controller duplicate resume | 1,030 ms |
| Bedrock narration | 973 ms and 791 ms |
| Endpoint config update to InService | 3 m 18 s |
| AgentCore runtime update to READY | 21 s |
| Endpoint creation to InService, first deployment | 4 m 21 s |

Per-answer Decider latency inside a controller run is **not** separately
reported, because the controller does not measure it as its own value; what is
known is that all four serving invocations returned 200, so each answer
arrived inside the 60 s cap it was measured against.

### Limitations, and what is not claimed

* The upstream evidence is still a declared synthetic fixture. A real provider
  answering about synthetic evidence is a real provider call, not a live
  measurement, and `classification` stays `synthetic`.
* The cloud denominator is one pair of calls. No rate, no significance, no
  generalization, and no comparison across cohorts is claimed from it.
* No claim is made about the model's general accuracy, about statistical
  significance, about production readiness, about latency or cost benefit, or
  about any saving.
* Delayed billing observations and unit-price estimates remain private. The
  always-on SageMaker endpoint is billed by uptime, not per request; a missing
  charge in delayed reporting does not mean it is free.
* The 60 s cap that made this run succeed is a property of `ml.m5.xlarge`
  serving a 3,321-character state, not a property of the product. A different
  instance or a larger state needs its own measurement; the cap is a
  deployment input precisely so that measurement is not an image rebuild.
* The run was made on CPU. No GPU and no larger instance was used, so nothing
  here says what the latency would be on either.
* Both local and cloud runs use the same pinned checkpoint revision. Which
  revision that is, raw or digested, is private deployment identity, and no
  public file states it. Nothing was retrained, refitted,
  or re-thresholded for the cloud, and the golden framing, acceptance tier and
  receipt semantics are unchanged from the local runs.

## Two smaller failures, both the mechanism working

Neither of these is an experiment, and neither is hidden. They are recorded
because in both cases a check refused something, and the refusal was correct.

* **The public bundle's sanitizer refused to build.** The first time it was run
  against a successful result it would not produce a bundle, because the
  pinned checkpoint revision reached it in a form the sanitizer would not
  publish. The fix was to project the revision through the product's own
  public-identity function, not to remove the check. A sanitizer that refuses a
  real result is doing its job.
* **Two telemetry verification checks were wrong on their first pass.** One of
  them passed vacuously on no data, which means it would have reported success
  against an empty window. Both were corrected in the verification script and
  not in the product, because the product's behaviour was never what the checks
  had got wrong.

## Running the real model locally

The offline path uses a deterministic stand-in, labelled `mock`, which holds no
authority and measures nothing. Three things change when a real checkpoint
answers, and nothing else does.

`evaluation/decider_provider.py` is the adapter. It builds the payload with the
shipped `unstuck.cascade.choice.choice_request` and validates the answer with
the shipped `validate_choice`, so the criteria, the option order, the bounded
state text and the acceptance shape are the product's and not the adapter's. It
substitutes nothing: a transport failure, a late answer and a malformed answer
each become a typed refusal carrying no choice, no distribution and no
confidence. There is no fallback order anywhere in that file.

Running it needs the `strands-decider` package and a local snapshot of the
pinned checkpoint, which are deliberately not dependencies of the shipped
wheel. The five-case golden run was produced through this adapter against one
immutable revision of Strands Decider 2B, on one machine, in one process,
offline. Which revision it was is private deployment identity, and no public
file states it.

The checkpoint was chosen **because** it exhibits a repeatable blind spot on
this fixture, not because it performed well.

## The security boundary, in full

* A caller cannot select the scenario, the provider, the model, the tools, the
  mode, or the acceptance tier. An extra field is a reason-coded refusal.
* Only allowlisted read-only, argument-free diagnostics are offerable at all.
  The repair is deterministic and authorized only by verifier-held evidence.
* The local web view binds loopback only and publishes no port.
* No secret is minted, stored, or read on any path. There is no application
  programming interface key, no bearer token, and no custom credential
  anywhere.
* Telemetry is sanitized at the source. The pinned checkpoint revision travels
  as a digest and never raw, and that digest is not published either. All
  eighteen forbidden raw values were verified absent from the cloud run's
  telemetry, including every account, resource and network identifier, every
  credential pattern, machine local paths, tracebacks, request keys, caller
  identity, and the narration prose itself.
* Evaluator truth is never exposed to a runtime role or a model input, and it
  was not exposed in any run reported here. The held-out corpus was not read
  and not run.
