# Decider 2B domain fine-tune: corpus, harness, configs

Offline, synthetic experiment tooling. It changes no runtime behaviour, adds no
classifier, gate, router or second selector, and is imported by nothing under
`src/unstuck`. Decider 2B stays the only model tool selector. No result here is a
claim until an adapted checkpoint passes the frozen test comparison, and no
offline result supports a latency or cost claim: the deployed endpoint is
always on and billed by uptime. The full research record, including the audit
limitations and the later viability stop, is
`docs/research/decider-adaptation-findings.md`.

## Why a new corpus

Earlier screens showed that current production payloads already choose
`inspect_pipeline` on every case, so relabelling those constant-choice payloads
teaches nothing. This corpus is captured from scenarios where different first
reads are objectively productive, and every label comes from executing every
offered candidate through the real controller, verifier and DuckDB engine.

## Files

| File | Role |
| --- | --- |
| `scenarios.py` | Scenario families, catalogs (production plus six renamed), frozen partition assignment |
| `oracle.py` | Real `Controller` runs with a capturing Decider; counterfactual labels |
| `corpus.py` | `build`, `verify`, `scope` |
| `identity.py` | Prompt and checkpoint identities (standard library only) |
| `infer.py` | Checkpoint answers in the isolated model environment; render check |
| `evaluate.py` | `freeze`, `report`, `compare` |
| `configs/head-only.yaml` | The one template to run first |
| `configs/lora-head-fallback.yaml` | Fallback template. Nothing in this repository launches it; it was run once outside the repository, with the official trainer, and produced the LoRA-plus-head result |

## What the test partition is

Families, renamed catalogs, dataset identities and exact prompts are disjoint
across partitions, and `verify` proves it. The model-visible decision is not:
the corpus has 20 distinct model-visible situations, and all 20
model-visible test situations occur in training with the same label. The test partition is
controlled variation, in-distribution. Its 80 production-catalog rows differ
from training prompts only in dataset identity, version and hashes; its 80
renamed-catalog rows use tool identities and descriptions that never appear in
training or calibration, the only new surface.

The harness modules and the two config templates say "held-out" for the test
partition and carry internal criterion labels. They stay byte-identical,
because the freeze record binds their SHA-256; read "held-out" there as "test
partition, in-distribution".

## Labels

A candidate is productive iff the verifier classifies its read
`useful_evidence` and the run reaches a verified terminal reason with the rows
the fixture implies and no false repair. A row is kept only when exactly one
offered option is productive. Rows whose public situation (report facts,
history, preclusions, offered capabilities, budgets) is shared by families that
disagree would fail the build. Three audited families are labelled and then
excluded, with the reason recorded in the manifest:

* `pending_key_drift`: both reads are `useful_evidence`, but the schema read
  leads to a failed repair, and its D1 situation is identical to key drift,
  whose productive read is the schema read.
* `unapproved_key_drift`, `failed_job`: no offered read is productive.

Row kinds: `fresh_d1` (empty history, read budgets 1 and 2) and `receipt_d1`
(Run 2 after Run 1 chose each offered read under the loop-escape budget: a
dead end is precluded by its exact receipt before D1, a productive read is
offered again with its useful receipt as history). D2 rows are not generated:
the loop-escape configuration ends a call after one read, and a D2 payload
would carry a synthetic D1 distribution that production never sends.

Observed production property, unchanged here: a Run 2 state lists each
matched receipt's `selected_capability`, the private capability name, so a
renamed catalog's history still names `inspect_*`.

## Commands

```bash
OUT=PRIVATE_OUT  # any output directory outside version control
python -m experiments.decider_finetune.corpus build --out $OUT/corpus --model-revision scripted-v1
python -m experiments.decider_finetune.corpus verify --corpus $OUT/corpus
MODEL_PY -B experiments/decider_finetune/infer.py render-check --corpus $OUT/corpus --upstream UPSTREAM
python -m experiments.decider_finetune.evaluate freeze --corpus $OUT/corpus --checkpoint PINNED --out $OUT/freeze.json
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 MODEL_PY -B experiments/decider_finetune/infer.py run \
  --requests $OUT/corpus/test.requests.jsonl --checkpoint PINNED --out $OUT/answers/baseline.json --device mps
python -m experiments.decider_finetune.evaluate report --corpus $OUT/corpus --freeze $OUT/freeze.json \
  --answers $OUT/answers/baseline.json --out $OUT/reports/baseline.json
```

Training happens outside this repository, with the official trainer, from the
upstream repository root, after copying `train.jsonl` and `calibration.jsonl`
into `data/unstuck/` unchanged:

```bash
python -m training.train --config configs/head-only.yaml
python -m training.evaluate calibrate checkpoints/unstuck-head-s0 --data data/unstuck/calibration.jsonl --split all
```

Calibration reads only the calibration partition, all of it (`--split all`).
The test partition is never given to `calibrate` or to training. After
calibration, write a calibration record:

```json
{"schema": "decider-calibration-record-v1", "data_sha256": "<sha256 of calibration.jsonl>",
 "split": "all", "checkpoint_digest_after": "<identity.checkpoint_digest of the calibrated directory>"}
```

Then answer the same frozen requests with the adapted checkpoint and compare:

```bash
python -m experiments.decider_finetune.evaluate compare --corpus $OUT/corpus --freeze $OUT/freeze.json \
  --baseline $OUT/answers/baseline.json --adapted $OUT/answers/adapted.json \
  --adapted-digest HEX --calibration-record CAL.json --out $OUT/reports/compare.json
```

`compare` prints `PROMOTION_ELIGIBLE` only when the adapted checkpoint cuts
test integrated wasted first reads to at most 80% of the baseline's, has no
paired completion regression, and makes zero false repairs and zero false
preclusions. Anything else is `PROMOTION_REFUSED`. `compare` applies no
calibration criterion: ECE and NLL are reported descriptively for both
checkpoints, and a checkpoint with any ECE can be `PROMOTION_ELIGIBLE`. ECE
scores the project's normalized confidence, `(n * pmax - 1) / (n - 1)`, not
the top-label probability. Neither verdict deploys, publishes or commits
anything.

## Measured checkpoint comparison

The versioned records under `dist/public-demo/` are sanitized evidence derived
from the measured reports. The baseline scored 97/160 first choices correctly,
20/64 integrated wasted first reads and 44/64 verified completions. The
head-only result moved first-choice correctness to 107/160 but left wasted
first reads at 20/64, so `compare` refused it on that criterion; its ECE of
0.408 is descriptive and was not the reason.

LoRA-plus-head moved first-choice correctness to 129/160, with 26 of the 32
gains on production-catalog rows; on the renamed-catalog slice it moved from
64/80 to 70/80. Integrated wasted first reads moved from 20/64 to 15/64: five
improved calls and none worse, one seed, four of the five in one family, exact
paired p = 0.0625. Verified completions moved from 44/64 to 49/64 on the same
five calls, with zero paired completion regressions. False repairs and false
preclusions were 0 for both, but those errors were structurally unreachable in
these families, so the zeros are not safety evidence. ECE moved from 0.165 to
0.166 and NLL from 0.930 to 0.373, both descriptive. The 64 integrated calls
replay 64 of the 160 row answers through the controller; they are not extra
inferences. A private hosted canary was run later and missed its
pre-registered exact-choice parity gate, so no hosted result is claimed. No
latency or cost claim is made. Decision Receipts remain the deterministic hard
boundary; Decider 2B remains the only model tool selector.
