# The Unstuck demo video

`unstuck-demo.mp4`, 178.533 seconds, 1280x720, 6.42 MB (6,416,193 bytes).
Ten narrated scenes. 30 fps, yuv420p, 5,356 frames. H.264 video; AAC mono audio at 48 kHz.

The project ceiling is strictly below 190 seconds, an approved requirement
change rather than a gate bypass. The exact probed duration is 178.533333 seconds
(5,356 frames at 30 fps). The rebuilt file is byte-identical to the current
candidate present before this ceiling change; the earlier 180.000-second
measurement describes a prior narration revision, not this candidate.
Combined ML scenes must remain strictly below 33 seconds. Every other threshold
is unchanged.

The video was built once, validated, visually reviewed and promoted locally.
**It has not been published publicly.** No deploy, external upload or hosted experiment occurred.

## Scene order and measured timing

The original hook is followed immediately by two ML HIGHLIGHT scenes: process,
then validation. Original scenes 2-8 follow unchanged, including their exact
narration, pitch frames and ten labelled recorded-replay steps. Pitch-slide
numbers still identify the original eight-slide deck, not the new video scene numbers.
The original receipt replay is separate from the experimental ML evaluations.

Amazon Polly Stephen generative supplies the synthetic video voice. It is not
the agent's after-completion Bedrock Nova Lite narration. All eight original
raw and padded narration caches were reused byte-for-byte after renumbering.
This ceiling-change build reused all ten current narration caches, including
both ML scenes; no narration was synthesized. The default profile and proven
Node module path were supplied to the build.

The two ML scenes total 30.967 seconds (929 frames at 30 fps), compared with the prior
23.700-second single ML chapter. No additional scene holds were added. Padding
remains only at the outside edges; scene joins use whole video frames.

| Scene | Start | Duration | Visual |
| --- | --- | --- | --- |
| 1 The hook | 0.000 s | 26.400 s | Original pitch slide 1 |
| 2 Why LoRA is in the decision path | 26.400 s | 12.933 s | ML process |
| 3 What validation showed | 39.333 s | 18.033 s | ML validation |
| 4 The control stays stuck | 57.367 s | 14.000 s | Original pitch slide 2; labelled replay |
| 5 Run 1 pays the learning cost | 71.367 s | 18.467 s | Original pitch slide 3; labelled replay |
| 6 Run 2 escapes | 89.833 s | 22.700 s | Original pitch slide 4; labelled replay |
| 7 The proof | 112.533 s | 24.000 s | Original pitch slide 5; labelled replay |
| 8 The private AWS path | 136.533 s | 16.133 s | Original pitch slide 6 |
| 9 The reusable pattern | 152.667 s | 12.500 s | Original pitch slide 7 |
| 10 The boundary, restated | 165.167 s | 13.367 s | Original pitch slide 8 |

## Labeled ML evidence and authority

The exact narration and complete on-screen evidence are in `docs/video-script.md`.
The process frame explains offline diagnostic execution, deterministic verifier
labels, LoRA adapters plus the Decider choice head and a frozen checkpoint.
At runtime the receipt filters first, adapted Decider 2B ranks the remaining
tools, and the verifier decides truth and repair. There is no online learning
or second selector. The adapted checkpoint is experimental, not the current
hosted selector.

| Metric | Baseline | LoRA-plus-head | Difference |
| --- | --- | --- | --- |
| Correct first choices, offline decisions | 97 out of 160 (60.6%) | 129 out of 160 (80.6%) | 32 more correct decisions |
| Wasted first reads, full calls | 20 out of 64 (31.3%) | 15 out of 64 (23.4%) | 5 fewer calls; 25% reduction |
| Verified completions, calls | 44 out of 64 (68.8%) | 49 out of 64 (76.6%) | 5 more completed calls |
| Correct choices, same-device CPU decisions | 96 out of 160 | 132 out of 160 | Direction confirmed |
| Wasted reads, same-device CPU calls | 20 out of 64 | 14 out of 64 | Direction confirmed |
| Verified completions, same-device CPU calls | 44 out of 64 | 50 out of 64 | Direction confirmed |

Head-only: 107 correct out of 160 decisions, but wasted reads stayed at 20 out
of 64 calls, so rejected. CPU and CUDA differed on 3 out of 160 near-tie decisions.
Percentages were checked before rendering, with decimal half-up rounding.

Controlled synthetic in-distribution evidence, one seed. Exact paired p=0.0625
is not a significance claim. False repair/preclusion zeros are not safety evidence.
The private hosted canary missed exact parity. No unseen-generalization,
hosted ML success, latency or cost claim. Original receipt-case latency
observations remain separate and unchanged; they do not measure ML benefit.

Sources: `docs/research/decider-adaptation-findings.md`,
`experiments/decider_finetune/device_parity/FINDINGS.md` and their published records.

## Validation

All ten gates returned PASS, without skips, warnings or overrides. Ninety narrow
duration, narration and private invariant tests passed before the single full
build; no retry or separate dry run occurred. The first eight gates are
the installed video-quality-gates checks; joined-timing and final-artifact add
independent checks on the encoded candidate. Promotion verifies the source
fingerprint, candidate digest and complete validation binding.

| Gate | Result | Evidence |
| --- | --- | --- |
| `preflight` | PASS | toolchain complete |
| `ssml-lint` | PASS | 10 segments clean |
| `prerender-data` | PASS | ten narrations and visual sources clean; eight exact embedded pitch images and two early ML frames |
| `timing` | PASS | all segments within thresholds |
| `stt-roundtrip` | PASS | narration matches intent |
| `stream-uniformity` | PASS | all 10 segments: 1280,720,yuv420p,30/1,0/0 |
| `blackdetect` | PASS | no black span > 0.4s |
| `ocr-leak` | PASS | 89 frames sampled, clean |
| `joined-timing` | PASS | no dead air >= 1.0s across 9 joins; 5356 frames at 30/1; longest silence 0.78s |
| `final-artifact` | PASS | finished MP4 independently decoded, transcribed and visually sampled |

Independent final-artifact evidence:

- 178.533s; 1280x720; 30 fps; yuv420p; uniform audio and video
- 10 narrated segments; 8 current pitch frames; 2 early ML frames; 10 labelled replay steps
- 109 OCR samples clean; full decode and timestamps clean
- no black span over 0.4s; no dead air at or above 1.0s
- 01-hook: 98% match (41 tokens)
- 02-ml-process: 90% match (21 tokens)
- 03-ml-validation: 100% match (27 tokens)
- 04-control-stays-stuck: 91% match (23 tokens)
- 05-run-1-pays-once: 100% match (25 tokens)
- 06-run-2-escapes: 97% match (36 tokens)
- 07-proof: 97% match (33 tokens)
- 08-private-aws-path: 92% match (25 tokens)
- 09-reusable-pattern: 100% match (23 tokens)
- 10-boundary: 100% match (23 tokens)

Minimum source-frame similarity: 0.989873.
The twenty-window contact sheet, both full-size encoded ML frames, sampled
replay frames and all ten final-audio transcripts were inspected before local
promotion. Every replay step is explicitly a recorded replay, not a new run.
The source scan combines 49 shape patterns and 5 machine-local identity patterns;
OCR also checks the account-number pattern.

## Decoded ML transcripts

These are the independent speech-recognition transcripts extracted from the
finished MP4, not substituted narration text. Scene 2 scored 90% and Scene 3
scored 100% against the unchanged 80% minimum. The narration-cache roundtrip
scored 95% and 100%, respectively. The recognizer renders LoRA as "low RA";
the authored narration and cached audio were not changed.

Scene 2:

> Offline, we ran every diagnostic and trained low RA plus deciders' choice head on Verifier labels. At runtime, the receipt removes one repeat. Low RA ranks what remains. The Verifier owns truth.

Scene 3:

> Across 160 decisions, correct first choices rose from 97 to 129. Across 64 calls, wasted first reads fell from 20 to 15 and completions rose from 44 to 49. CPU replay confirmed direction, one seed, experimental checkpoint.

## Public export boundary

The video and its exact ten-gate report ship together. Private build sources,
literal identity patterns and narration caches stay untracked and are not part
of the public deliverable. No gate was waived. Post-promotion gates-only returned
all ten PASS again, with no skips, warnings or overrides. The promoted MP4 and
validation report match the revalidated candidate and report byte-for-byte.
Public video tests: 37 passed, with no failures, skips or warnings; 592 tests
were deselected by the video/narration selection and unchanged repository
exclusions. Narrow pre-build tests: 90 passed. The script's Build status records
these results. No retry, git mutation, commit, push, deploy or external
publication occurred.
