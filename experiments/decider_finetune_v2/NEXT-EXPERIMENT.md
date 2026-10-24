# Decider fine-tune v2: receipt-enriched later-call states

Verdict: **STOP_RECEIPT_STATES_COLLAPSE**. Receipt-enriched later-call
states built from the current three diagnostics create new signatures but no
new decisions. No factorial prototype was built, no model was trained or
run, and no test was opened. This is a measured limit of the current
observable inventory and is recorded as evidence, not a failure to force
past.

## Starting evidence

Phase 1 (`PREREGISTRATION.md`) returned STOP_INSUFFICIENT_HOLDOUT: 390
verifier-labeled decisions from 13 families collapse to 20 canonical
signatures, the existing train/test intersection is 20 of 20, whole-family
components number 4 (2, 3, 4, 4 families), and only two capabilities are
ever productive. That result is unchanged.

## Hypothesis H2, as preregistered

Pass-one decision states of later calls, enriched with one or two prior
verifier-owned receipts from the current three diagnostics, contain
canonical model-visible signatures absent from training, still offer at
least two executable diagnostics, and carry verifier labels that depend on
receipt content beyond the receipt-free state.

The frozen criteria are in
`.unlazy/true-generalization-v2/RECEIPT-STATES-GATES.md`, written before
`receipt_states.py`. One exploratory replay of variant 0 preceded them and
is disclosed there.

## What ran

The 13 assigned families, six variants each, production catalog, through the
real controller, cascade, verifier, and local receipt store. Depth 0 is a
fresh call at read budgets 1 and 2. Depth 1 is a second call after one
scripted prior read. Depth 2 is a third call after every ordered pair of
prior reads, repeats included. Each call has read budget 1 and all calls in
a chain share one receipt namespace. Every candidate offered at the labeled
call executed counterfactually on a freshly replayed prefix, and every
counterfactual saw the byte-identical request. Labels and signatures use the
unchanged phase 1 `verifier_label` and `signature`.

## Result

| Depth | Rows | Signatures | Signatures by executable choices | Pipeline / schema-key decisions |
| --- | --- | --- | --- | --- |
| 0 | 156 | 8 | three: 8 | 48 / 108 |
| 1 | 234 | 12 | two: 8, three: 4 | 72 / 162 |
| 2 | 546 | 16 | one: 4, two: 8, three: 4 | 168 / 378 |
| All | 936 | 36 | | 288 / 648 |

Excluded: absent 0, ambiguous 0, nonproductive 0, prior not executed 156.
The 156 are the two repeated priors per scenario (78 scenarios) whose first
read was a blocking receipt: exact-match preclusion removes that diagnostic
from the next call, so no second receipt exists.

| Decision-level quantity | Result |
| --- | --- |
| Train signatures (depth 0 and 1) | 20 |
| Depth 2 signatures | 16 |
| Depth 2 with one executable choice | 4 |
| Depth 2 with at least two executable choices | 12 |
| Of those, already seen in train | 0 |
| Held-out signatures | 12 |
| Train/held-out signature intersection | 0 |
| Held-out collapsed to a training core with the same label | 12 |
| Held-out not collapsed | 0 |
| Held-out whose label an exact useful receipt already names | 12 |
| Held-out sharing families with train | yes |
| Distinct receipt-free cores | 8 (each with one label) |
| `H(label given core)` | 0 bits |
| `H(label given signature)` | 0 bits |
| Receipt information `I` | 0 bits |

Family-atomic analysis under the phase 1 boundary gives 4 components of 2,
3, 4, and 4 families for all depths, for receipt depths only, and for
multi-choice depth 2 states only. No component contains two productive
capabilities and no allocation exists. Failing criteria:
`collapse_to_training_core`, `productive_capabilities`,
`no_receipt_information`.

## The information-theoretic limit

1. In this inventory every read of a non-productive diagnostic yields a
   blocking receipt, and an exact blocking receipt precludes that
   diagnostic. With three diagnostics, two such receipts leave exactly one
   executable choice: a forced move, not a ranking decision (4 signatures).
2. A two-receipt state keeps at least two executable choices only when one
   receipt belongs to the productive diagnostic, which is useful and never
   blocking. So every such state already carries verifier-produced evidence
   that names its label (12 of 12).
3. The label is a function of the receipt-free core: 8 cores, one label
   each, `I = 0`. Receipts narrow the offer; they never change which
   diagnostic is productive.
4. Receipt states are identical across all families of one observation
   class, so they only add signature edges inside existing components. The
   component structure, and the single pipeline-productive component, cannot
   change by adding receipt depth.

Together: new signatures, zero new decisions. A lookup from training cores
answers every held-out state, so this is not a true holdout and the
experiment stops here. No tool, runtime fact, fixture, hidden cause, or
story element was added to change that outcome.

## Role of ML, precisely

- The Decider may rank the remaining executable diagnostics only when the
  current state contains discriminating verifier-produced evidence:
  observation facts or receipts whose content differs between situations
  that need different diagnostics.
- In this inventory that evidence is entirely the receipt-free observation
  core. Receipts contribute 0 bits to the label. Their job belongs to the
  controller: exact-match preclusion deterministically removes proven dead
  ends, and the two-read bound keeps the loop finite. Neither is learned.
- The Decider cannot infer a hidden root cause from an observationally
  identical D1 state. Families inside one component differ in hidden fixture
  facts yet render the same state. If two hidden causes under one D1 state
  ever needed different diagnostics, no model could beat the prior over
  them; only a verifier-owned read can separate them, and the verifier, not
  the model, decides what counts as progress.
- On the current inventory a tuned model can at best match a training-core
  lookup. Any claim of learned generalization needs held-out cores, not
  more receipts.

## Next hypothesis H3 (not executed, needs separate approval)

A true holdout needs new observation cores, not new receipt combinations.
Each of train, calibration, and test must contain at least two productive
capabilities, and components are whole observation classes. With the
current two productive capabilities, each needs at least three components
with distinct cores. Today pipeline has 1 such component, schema keys 3,
and scope 0.

H3: new synthetic families, rendered only through the existing observable
fields and the existing three diagnostics, can produce at least three
held-out-capable cores for each of at least two capabilities, with
`H(label given core) = 0` inside each core and zero cross-split core
overlap. H3 is falsified if new families render cores already present (they
would merge into existing components) or if any core carries conflicting
verifier labels. Testing H3 changes the fixture inventory, so it requires
its own preregistration, newly sealed test realizations, and explicit
approval. It is not authorized by this phase.

## Reproduction

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=src:.
python -B -m experiments.decider_finetune_v2.receipt_states --workers 8
```

Exit status 2 is the expected STOP. The count-only result is
`receipt-states-report.json`; it holds no prompts, per-case labels, hashes,
or model identities.
