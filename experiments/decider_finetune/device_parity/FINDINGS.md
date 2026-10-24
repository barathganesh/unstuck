# Decider fine-tune: same-device CPU parity

The earlier hosted canary of the adapted checkpoint agreed with the offline
adapted answers on 157 of 160 frozen rows. The offline answers came from a
different device (CUDA for the adapted checkpoint, the local accelerator for the
baseline), so that comparison mixed a checkpoint change with a device change.
This study removes the device change. It serves both checkpoints on CPU with the
same pinned server image, instance type, private network placement, network
isolation, serving environment and serving revision label. It then replays the
same 160 frozen requests against each, once per row, with SDK retries disabled,
and scores both answer sets with the frozen harness, unchanged.

Record: `device-parity-cpu-v1.json`.

## Verdict

The adapted checkpoint's improvement holds when both checkpoints run on CPU.
The frozen harness verdict is `promotion_eligible`, and every frozen criterion
is met.

| Metric | Baseline, CPU | Adapted, CPU |
| --- | --- | --- |
| First choice correct | 96/160 | 132/160 (36 corrections, 0 regressions) |
| Row-level wasted first reads | 64/160 | 28/160 |
| Integrated wasted first reads | 20/64 | 14/64 |
| Integrated verified completions | 44/64 | 50/64 (0 completion regressions) |
| False repairs / false preclusions | 0 / 0 | 0 / 0 |
| NLL | 0.9322296 | 0.3739600 |
| ECE, normalized confidence (harness) | 0.1722280 | 0.1962222 |
| ECE, standard top-label | 0.1718450 | 0.1269737 |

The two ECE definitions move in opposite directions. Top-label ECE falls by
0.0449. Normalized-confidence ECE rises by 0.0240. The offline pair shows the
same split: top-label ECE falls by 0.0513 and normalized-confidence ECE rises
by only 0.0011. The adapted checkpoint is better calibrated in its top
probability. It is less well calibrated in the library's normalized confidence.
That normalized confidence is the one each answer is served with, so the rise
is worth watching.

## Device variance

| Pair | Choice agreement | Identical probabilities | Max abs probability difference | Integrated calls differing |
| --- | --- | --- | --- | --- |
| Adapted: CPU vs CUDA | 157/160 | 1/160 | 0.0301 | 1/64 |
| Adapted: CPU vs the earlier CPU replay | 160/160 | 160/160 | 0 | 0/64 |
| Baseline: CPU vs offline accelerator | 159/160 | 0/160 | 0.0104 | 0/64 |

* CPU serving is deterministic. The adapted checkpoint answered all 160 rows
  with the same probabilities as the earlier CPU replay. The three adapted
  flips therefore come from the device, not from run-to-run noise.
* Every flip is a near-tie. The adapted flips are rows 18, 38 and 120. On CPU
  their top-two gaps are 0.0132, 0.0233 and 0.0037. On CUDA they are 0.0202,
  0.0017 and 0.0444. All three land on the verifier label on CPU and miss it on
  CUDA. The one baseline flip, row 83, is a near-tie on both devices. It misses
  the label on CPU and lands on it offline.
* The device shift moves the totals by at most a few rows: adapted first choice
  132 vs 129, integrated wasted reads 14 vs 15, completions 50 vs 49; baseline
  first choice 96 vs 97, integrated counts unchanged.

## Limits

* The holdout is offline and synthetic: one seed, four held-out scenario
  families, 160 rows and 64 integrated calls.
* No threshold was changed. This study makes no production claim and no
  latency or cost claim.
* Each checkpoint was served on one CPU instance for one replay.
* Decision Receipts remain the deterministic hard boundary. Decider 2B remains
  the only model tool selector.
