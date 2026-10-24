# Decider 2B adaptation research: v1 findings and the v2 stop

Research record. It adds no public claim: the claim register rows FT1 to FT9
are the only public statements of these results, and this file agrees with
them. Everything here is offline and synthetic. No runtime behaviour changed:
Decision Receipts remain the deterministic hard boundary, and Decider 2B
remains the only model tool selector.

## What v1 was

v1 asked whether adapting Decider 2B on verifier-derived labels lowers the
number of wasted first diagnostic reads. Every label came from executing every
offered read counterfactually through the real controller, verifier and
evidence engine; a row was kept only when exactly one offered read was
productive. Two capabilities were ever productive, `inspect_pipeline` and
`inspect_schema_keys`.

| Partition | Rows | Scenario families | Tool catalogs |
| --- | --- | --- | --- |
| Train | 900 (855 gradient rows, 45 validation rows) | 6 | production plus four renamed |
| Calibration | 120 | 3 | production plus one renamed |
| Test | 160 | 4 | production plus one renamed |

The 64 integrated calls (Run 1 then Run 2 for each test case) replay 64 of the
160 test row answers through the controller. They are not extra inferences.

## The test partition is controlled variation, not new situations

Families, renamed catalogs, dataset identities and exact prompts are disjoint
across partitions, and the build verifies that. The model-visible decision is
not. The whole corpus has 20 distinct model-visible situations, and all 20
test situations occur in training with the same label; 15 of them also occur in
calibration. Every one of the 160 test rows sits in a training situation.

* **Production-catalog slice, 80 rows.** These test prompts differ from
  training prompts only in dataset identity, dataset version and hashes. The
  question and the options are identical. This slice is in-distribution.
* **Renamed-catalog slice, 80 rows.** The test catalog's tool identities,
  descriptions and conditions never appear in training or calibration. This is
  the one genuinely new surface, and it is still a rewording of situations the
  model trained on.

## Measured results

All counts are on the test partition, one training seed. ECE uses the
project's normalized confidence, `(n * pmax - 1) / (n - 1)`, and is reported
descriptively at three decimals. No calibration gate was applied.

| | Baseline | Head-only | LoRA-plus-head |
| --- | --- | --- | --- |
| First choice correct | 97/160 | 107/160 | 129/160 |
| Production-catalog slice | 33/80 | not recomputed | 59/80 |
| Renamed-catalog slice | 64/80 | not recomputed | 70/80 |
| Integrated wasted first reads | 20/64 | 20/64 | 15/64 |
| Integrated verified completions | 44/64 | not reported | 49/64 |
| ECE, normalized confidence | 0.165 | 0.408 | 0.166 |
| NLL | 0.930 | 0.955 | 0.373 |

### Negative result: head-only

Training the head alone raised first-choice correctness to 107/160 and left
integrated wasted first reads at 20/64. The harness refused it on the
wasted-first-read criterion. Its ECE rose to 0.408, which is recorded but was
not the reason for refusal, because the harness has no calibration criterion.

### Qualified positive result: LoRA-plus-head

* First choice moved from 97/160 to 129/160: 32 rows corrected, none broken.
  26 of the 32 are on the production-catalog slice, near-copies of training
  prompts. On the renamed-catalog slice it moved from 64/80 to 70/80
  (6 corrected, none broken, exact paired p = 0.031).
* Integrated wasted first reads moved from 20/64 to 15/64. That is five
  improved calls and none worse, on one seed, with an exact paired p = 0.0625.
  Four of the five are in one family. Leaving that family out, the
  reduction is 12 to 11 (8%), which misses the frozen 20% criterion. The
  production catalog alone also gives 12 to 11. Audit cluster bootstrap 95%
  intervals for the relative reduction are about 8% to 39% by scenario and
  0% to 43% by family.
* Verified completions moved from 44/64 to 49/64 on the same five calls, with
  zero paired completion regressions.
* NLL fell from 0.930 to 0.373. Normalized-confidence ECE moved from 0.165 to
  0.166; a row bootstrap interval for that difference spans roughly -0.09 to
  +0.07, so the change is indistinguishable from zero. A standard top-label
  ECE moves the other way, 0.159 to 0.108. Neither is a gate.

### False repairs and false preclusions

Both checkpoints scored 0 false repairs and 0 false preclusions. Those errors
were structurally unreachable in these families: no test outcome could
false-repair, and a productive read always writes a non-blocking receipt, so
it cannot be falsely precluded. Answers that always choose a non-productive
read also score 0 and 0. The zeros are not safety evidence.

## Audit limitations

An independent audit recomputed every number above. Its limitations stand:

* **One seed.** Seed variance could move the five-call effect across the 20%
  criterion.
* **Device confound.** Baseline answers were produced on one local
  accelerator backend and adapted answers on another. Device numerics can flip
  near-tie choices; no figure here separates that effect.
* **Test reuse.** Head-only and LoRA-plus-head were scored in sequence on the
  same test partition under one freeze. The fallback template asked for a
  second, separately frozen run, and none was made. Nothing corrects for two
  attempts at the gate.
* **Simple comparators.** Every label is a function of one fixture fact. A
  constant preference for the schema-key read where it is offered scores
  128/160, and a two-feature rule scores 136/160, against 129/160 for
  LoRA-plus-head. Neither rule is proposed for runtime; it would be a hidden
  classifier.
* **Self-attested provenance.** The harness takes the adapted checkpoint
  identity and the calibration record as supplied. It refuses a record that
  names the test partition, and accepts a handwritten one.
* **Coverage gap.** The integrated replay refuses answers that choose review in
  Run 1, so it cannot score that behaviour.
* **Public truth.** The deterministic build regenerates test labels from public
  code, so a later adaptation could train on them unnoticed.
* **Unbacked figures removed.** A total parameter count, the trainable
  fraction derived from it, and a peak memory figure had no backing artifact
  and were removed from the evidence record.
* **Frozen wording.** The harness modules and config templates say "held-out"
  for the test partition and carry internal criterion labels. They are left
  byte-identical, because the freeze record binds their SHA-256; read
  "held-out" there as "test partition, in-distribution".

## The later hosted canary

A private hosted canary was run later and missed its pre-registered
exact-choice parity gate, so no hosted result is claimed. No hosted metric or
identifier is recorded here, and the offline figures above are not replaced or
supplemented by it.

## v2 viability check: STOP

v2 was preregistered to find a test partition whose model-visible situations
do not occur in training, before any training or model evaluation. It
inventoried every assigned v1 family with one representative of each of its
six surface variants on the production catalog, labelled decisions only by
executing every offered candidate through the controller and verifier, and
canonicalized the model-visible state with identities, hashes, names, card
wording and option order removed. Families sharing any canonical situation
were joined into indivisible components.

| Measured quantity | Result |
| --- | --- |
| Source families | 13 |
| Surface variants per family | 6 |
| Verifier-labelled decisions | 390 |
| Distinct canonical situations | 20 |
| v1 train / calibration / test situations | 20 / 15 / 20 |
| v1 train and test intersection | 20 |
| v1 test situations absent from training | 0 |
| Productive capabilities | 2 (`inspect_pipeline` 120, `inspect_schema_keys` 270) |
| Indivisible components | 4, holding 2, 3, 4 and 4 families |
| Excluded decisions (absent, ambiguous, no productive read) | 0, 0, 0 |

Verdict: **STOP_INSUFFICIENT_HOLDOUT**. No whole-component allocation gives
nonempty train, calibration and test partitions with at least three test
situations absent from training and two productive capabilities in each. No
split was proposed, no model was trained or evaluated, and no test experiment
was opened. The inventory supports no claim about new observable states.

## What a further experiment would need

The v2 preregistration fixes the conditions for any later attempt:

* new observable evidence in the fixtures, under a separate preregistration,
  so that a test partition can hold situations absent from training;
* several fixed seeds, one inference device and precision for every cell, and
  one sealed test opening across all seeds and cells;
* a fixed-runbook comparator and a training-only simple-rule comparator;
* standard top-label ECE, enforced as a gate rather than reported;
* family-level clustering, cluster bootstrap intervals and
  leave-one-family-out sensitivity, with no confirmatory claim below ten
  independent test clusters;
* scenarios where false repair and false preclusion are genuinely possible,
  with positive controls, before any safety statement.
