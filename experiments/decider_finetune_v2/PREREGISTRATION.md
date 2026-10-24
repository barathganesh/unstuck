# Decider fine-tune v2: tracked phase 1 preregistration and result

This tracked file preserves the phase 1 preregistration and its measured
result exactly. The original timestamped record is
`.unlazy/true-generalization-v2/GATES.md`; the count-only machine result is
`viability-report.json` in this directory. Neither is changed by later work.

Result in one line: **STOP_INSUFFICIENT_HOLDOUT**. 390 verifier-labeled
decisions from 13 families collapse to 20 canonical signatures; the existing
train/test intersection is 20 of 20; whole-family components number 4 (2, 3,
4, 4 families); only 2 capabilities are ever productive (pipeline 120,
schema keys 270); no admissible allocation exists. This is evidence about
the current observable inventory, not a failure to force past. No training,
inference, or test opening occurred.

The follow-up hypothesis on receipt-enriched later-call states, and its
result, is in `NEXT-EXPERIMENT.md`.

The phase 1 preregistration and execution record follow verbatim.

---

# Private Unstuck v2: phase 1 preregistration

Status: PREREGISTERED before v2 source implementation. Scope is a local,
scripted viability check, not training, model evaluation, or publication.
The existing generic story and all existing artifacts stay unchanged.

## Evidence and boundary

Read-only inputs are the v1 scenario inventory, corpus signature logic,
counterfactual controller/verifier oracle, and the independent ML audit report
supplied with this task. The audit identifies situation overlap, unreachable
safety outcomes, missing calibration enforcement, device confounding, test
reuse, and missing simple comparators. Do not copy operational identities or
model revisions from those inputs.

Only this file, `experiments/decider_finetune_v2/**`, and
`tests/decider_finetune_v2/**` may be created or changed. No network, cloud
operations, training, source-control operations, deployment, or public edits.
Use the existing local oracle with a generic scripted revision. No checkpoint
or external trainer is loaded.

## Frozen viability protocol

1. Inventory every assigned v1 fixture family, with one representative of each
   of its six existing surface variants under the production catalog. Exercise
   fresh decisions at read budgets 1 and 2 and receipt-aware decisions after
   each offered read. Labels come only from executing all offered candidates
   through the existing controller and verifier. Never use the fixture rule as
   ground truth. Retain only decisions with exactly one productive candidate;
   count absent, ambiguous, and inconsistent labels separately.
2. Canonicalize actual captured model-visible state, not fixture truth. A
   signature retains pass, observation facts, semantic offered capabilities,
   receipt capability/outcome/match, preclusions, and budgets. Remove opaque
   identities and hashes; normalize names, card paraphrases, and option order
   through the existing catalog bindings. Surface variants never establish
   novelty. Validate the supported state shape and fail closed on new fields
   rather than silently dropping new semantics. Preserve relevant history
   status, blocked state, truncation, and any later decision state or refuse it.
3. Keep original fixture families intact. Connect families sharing any
   signature, across both receipt conditions, into indivisible components.
   Do not carve fresh/receipt/budget variants of one fixture family across
   splits, relabel signatures as independent families, or use hidden fixture
   differences as model-visible novelty. This is the conservative independence
   boundary for this protocol, not a theorem about all possible future states.
4. A viable allocation needs nonempty train, calibration, and test components,
   pairwise empty signature intersections, at least 3 truly unseen test
   signatures, and at least 2 verifier-productive capabilities in each split.
   Enumerate whole-component allocations deterministically, without model
   scores. No row-level random split or fallback to name-disjoint families.
5. The legacy train/test signature intersection is a diagnostic only. Report
   its actual counts separately from any proposed v2 split. If no allocation
   satisfies step 4, return STOP_INSUFFICIENT_HOLDOUT, do not emit split rows,
   and stop before any training or test experiment. No fabricated zero
   intersection for an absent v2 split. New observable evidence would require
   a separate preregistration; do not extend runtime or fixtures in this phase.

The viability inventory is development evidence, not an untouched evaluation
test. Reading its verifier outcomes cannot support an untouched-test claim.
Any future experiment requires newly sealed test realizations, inaccessible
to training and selection, under a separately approved freeze.

## Conditional experimental design, not executed in phase 1

- Fixed seeds: 17, 29, 43. One preregistered adaptation recipe, no fallback
  checkpoint selected on test. Train on train only; choose recipe/checkpoint
  on train-development data and fit one temperature on calibration only.
- Four paired cells per seed: baseline/receipts off, baseline/receipts on,
  tuned/receipts off, tuned/receipts on. Use identical fixtures, tasks, budgets,
  options, and exogenous prior-call schedules. Off uses an empty receipt
  repository, not merely hidden history text; on uses actual verified
  receipts and exact-match preclusion. Never reuse on-condition answers for
  off-condition state. Also report natural two-call trajectories separately.
- Fixed-runbook comparator: pipeline, then schema keys, then scope, then
  review, skipping unavailable actions. Freeze this order now; use no labels.
  Training-only simple-rule comparator: a depth-at-most-two decision tree on
  canonical observation and receipt facts, trained only on train verifier
  labels; deterministic lexical feature/capability tie breaks. No test-derived
  rules. Both are offline comparators, never runtime selectors, in both receipt
  conditions with the same budgets and verifier.
- Every model cell uses the same inference device and precision, prospectively
  fixed as CPU/float32. Abort on missing provenance or any mismatch. Scripted
  replay is not evidence of numerical parity or model improvement.
- Seal all three seeds and all four cells, comparators, calibration records,
  inference provenance, analysis code, and test access policy before opening
  test. One locked evaluation batch consumes test once across the entire
  experiment, including all seeds and cells. A partial/failed opening still
  consumes it. No retry, tuning, fallback, or reselection after opening.
  Private integrity records belong only in separately approved private storage.
- Primary endpoint: wasted first reads per eligible call, paired and averaged
  equally over original fixture families, then seeds. Report verifier-backed
  completion rates, review/abstention, absolute changes, and relative changes
  only when the baseline denominator is nonzero. Report tuning effects at
  receipts off and on, receipt effects for each model, and the interaction
  `(tuned,on - baseline,on) - (tuned,off - baseline,off)` in wasted-read rate.
- Statistical unit: original family, clustered further by connected signature
  component if necessary. Keep seeds, variants, catalogs, and repeated calls
  inside clusters. Report per-seed results, family-macro estimates, paired
  cluster bootstrap 95% intervals with 10,000 draws and fixed seed 17, and
  leave-one-family-out sensitivity. Never treat rows or seeds as independent
  families. Fewer than 10 independent test clusters permits descriptive
  results only, no confirmatory improvement claim.
- Standard top-label ECE uses `max(probabilities)` against argmax correctness,
  10 equal-width bins on [0,1], with 1 in the final bin, weighted by bin count.
  NLL is mean `-log(p[verifier_label])`; zero true-label probability means
  infinity. Reject invalid distributions and non-argmax selections. Report
  overall and family-macro metrics; never use normalized-margin confidence.
  A future improvement gate must actually enforce tuned-minus-baseline ECE
  <= 0.05 in each receipt condition for every seed, non-increasing NLL,
  no paired completion regression, and a positive lower confidence bound on
  reduced wasted reads. Descriptive viability is not promotion eligibility.
- Safety: no safety claim in phase 1. A future safety claim requires scenarios
  where false repair or false preclusion is genuinely possible, explicit
  applicable denominators, and adversarial positive controls proving detection.
  Empty denominators are not zero risk. Never infer safety from ordinary
  uniquely productive synthetic cases or from structurally impossible errors.

## Phase 1 acceptance checks

- Focused tests exercise canonicalization invariance, semantic differences,
  unsupported state refusal, verifier-only label extraction, component
  isolation, split minima, overlap refusal, and the real scripted inventory.
- Formatter, lint, strict mypy, and a scripted gate check run only on v2 paths.
  Keep bytecode and tool caches out of protected paths. Gate-check STOP is an
  expected nonzero viability result, not a successful experiment.
- Check created-file boundaries and text for operational identifiers and the
  forbidden dash character. Record measured results below only after running;
  never revise the preregistered criteria to make an observed result pass.

## Execution record

Verdict: **STOP_INSUFFICIENT_HOLDOUT**. The scripted gate completed with the
expected exit status 2. No model was trained or evaluated and no test
experiment was opened. Do not proceed to a corpus export, trainer, or model
experiment under this protocol.

| Measured quantity | Result |
| --- | --- |
| Assigned source families | 13 |
| Surface variants per family | 6 |
| Verifier-labeled decisions | 390 |
| Distinct canonical signatures | 20 |
| Existing train signatures | 20 |
| Existing calibration signatures | 15 |
| Existing test signatures | 20 |
| Existing train/test intersection | 20 |
| Truly unseen existing test signatures | 0 |
| Productive capabilities | 2 |
| Pipeline / schema-key productive decisions | 120 / 270 |
| Indivisible signature-connected components | 4 |
| Families per component | 2, 3, 4, 4 |
| Absent / ambiguous / nonproductive decisions | 0 / 0 / 0 |
| Admissible v2 allocation | None |

The richer v2 canonicalization and v1 canonicalization agree on aggregate
signature counts and intersections for this inventory. Exhaustive allocation
of whole components cannot satisfy all split and capability requirements.
A proposed v2 intersection is **not applicable**, not zero, because no v2
split was emitted. This is a refusal for the assigned current inventory under
the preregistered independence boundary, not a claim that every possible
future observable state is incapable of supporting a holdout.

The factorial, two comparators, family-level statistics, fixed seeds,
device/precision parity, calibration rules, and single-opening test protocol
above remain design only. No performance, generalization, safety, latency,
cost, or deployment claim follows from this prototype. The machine-readable
count-only result is `experiments/decider_finetune_v2/viability-report.json`.
No raw prompts, labels by case, hashes, or model identities were exported.

Validation completed:

- Focused tests: **36 passed**, with no skips or deselections in the final run.
  These include real controller/verifier replay for every assigned family and
  all registered catalog paraphrases, plus structural positive and negative
  controls. They are prototype tests, not an untouched model test experiment.
- Formatter check and lint: pass for all three new Python files.
- Strict mypy: pass for all three new Python files.
- Scripted viability gate: expected STOP with exit 2; count-only result
  assertions pass. No split is exported on refusal.
- Scope, identifier, forbidden-dash, and framing scans: pass for all five
  created files. Protected file modification times remain unchanged since
  preregistration. File creation times confirm preregistration preceded source.
- No training, model inference, cloud operation, source-control operation,
  runtime edit, deployment, or public-artifact edit was performed.

Reproduction uses an existing local environment with the repository's runtime
and development dependencies. Disable bytecode writes; no installation,
network call, or repository configuration change is needed:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=src:.
python -B -m experiments.decider_finetune_v2.viability --workers 4
python -B -m pytest -c /dev/null tests/decider_finetune_v2 -q \
  -p no:cacheprovider --basetemp=tests/decider_finetune_v2/.tmp-final
ruff format --check --no-cache experiments/decider_finetune_v2 tests/decider_finetune_v2
ruff check --no-cache experiments/decider_finetune_v2 tests/decider_finetune_v2
mypy --follow-imports=silent --cache-dir=/dev/null \
  experiments/decider_finetune_v2 tests/decider_finetune_v2
```

The viability command exits 2 intentionally. Imported existing modules are
not linted or modified; strict mypy checks all new Python source and tests.
The focused pytest invocation does not inherit unrelated deselections or
write the repository's default tool caches.
