# Demo video script

Ten scenes: original hook, ML process, ML validation, then original scenes 2-8.
The eight original scenes retain their exact narration and embedded 1280x720
pitch frames. Scenes 4 through 7 also show
local captures of the current steps in `docs/demo.html`. Every capture is labelled
as a recorded replay of the measured synthetic run, not a new run. No terminal,
console, live endpoint or new experiment is recorded.

Run 1 pays the learning cost once. Run 2 uses the receipt before selection.
The memoryless control stays stuck. Run 1 happens once: one inference, 21.6 s
client time, one read, unresolved, receipt stored. Run 1 is not saved.
Old Run 2 took 54.8 s and two inferences. Optimized Run 2 exact-precludes the
proven dead end before the first Decider request. One fresh choice, one inference,
one read, a verified repair, completed in 26.6 s. The observed Run 2 delta is
28.2 s and 51.4% lower client latency: n = 1, same synthetic case, each proof once.
No dollar savings are measured. The endpoint is billed by uptime, not requests.

Submission tags: `#strands-decider-2b`, `#tool-selection`.

The spoken text is identical to `video/spec.yaml`. Amazon Polly supplies the
video voice. This is separate from Amazon Bedrock Nova Lite's after-completion
narration inside the agent. The timing table is a conservative planning budget
at the previously measured 148.7 words per minute, with no added hold
per scene. Actual encoding retains padding only at the outside edges and uses
whole video frames at joins. The final validation records the rendered timing.

Do not read timecodes aloud.


## Scene 1. The hook

**0:00 to 0:28.** Planning budget: 28 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-01.png`.

> Unstuck gets Decider 2B out of a decision loop. Strands Decider 2B makes tool decisions. But a new call can repeat a decision already proven useless. Unstuck turns that verified dead end into a Decision Receipt. The same Decider then chooses a new path, and no larger model takes over. Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The memoryless control stays stuck.

## Scene 2. Why LoRA is in the decision path

**0:28 to 0:41.** Planning budget: 13 s speech, 0 s held.

*Screen:* Separate ML HIGHLIGHT frame.

LEARNED RANKING  |  One selector, frozen before runtime

**THE GAP**

A Decision Receipt exact-filters one proven repeat.
It cannot rank the remaining tools.

**OFFLINE TRAINING**

Execute every offered diagnostic; deterministic verifier outcomes become labels.
Train LoRA adapters plus the Decider choice head; freeze the checkpoint.

**RUNTIME AUTHORITY**

Receipt filters first; then LoRA-adapted Decider 2B ranks what remains.
The verifier still decides truth and repair. No second selector.
No online learning. The receipt does not update model weights.

Experimental adapted checkpoint, not the current hosted selector.
The original receipt replay follows; its hosted model is unchanged.

> Offline, we ran every diagnostic and trained LoRA plus Decider's choice head on verifier labels. At runtime, the receipt removes one repeat; LoRA ranks what remains; the verifier owns truth.

## Scene 3. What validation showed

**0:41 to 0:56.** Planning budget: 15 s speech, 0 s held.

*Screen:* Separate ML HIGHLIGHT frame.

LoRA-plus-head  |  Controlled synthetic in-distribution evidence  |  One seed

**OFFLINE DECISIONS AND FULL CALLS**

Correct first choices: 129 out of 160 decisions (80.6%); up from 97 (60.6%). 32 more correct decisions.
Wasted first reads: 15 out of 64 full calls (23.4%); down from 20 (31.3%).
5 fewer wasted calls; 25% reduction. Verified completions: 49 out of 64 calls (76.6%).
Completions up from 44 out of 64 calls (68.8%); 5 more completed calls.

**HEAD-ONLY REJECTED**

107 correct out of 160 decisions; wasted reads stayed at 20 out of 64 calls.

**SAME-DEVICE CPU REPLAY CONFIRMED DIRECTION**

Correct choices: 132 out of 160 versus 96. Wasted reads: 14 out of 64 versus 20.
Verified completions: 50 out of 64 calls versus 44.
CPU and CUDA differed on 3 out of 160 near-tie decisions.
Private hosted canary missed exact parity; checkpoint remains experimental.

Exact paired p=0.0625 is not significance. False repair/preclusion zeros are not safety evidence.
No unseen-generalization, hosted ML success, latency or cost claim.
Not the current hosted selector. Receipt filters; Decider ranks; verifier owns truth and repair.

> Across 160 decisions, correct first choices rose from 97 to 129. Across 64 calls, wasted first reads fell from 20 to 15 and completions rose from 44 to 49; CPU replay confirmed direction, one seed, experimental checkpoint.

## Scene 4. The control stays stuck

**0:56 to 1:11.** Planning budget: 15 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-02.png`, followed by demo steps 1, 9.

> This is a recorded replay of the measured synthetic run, not a new run. A report returned zero rows. Without receipts, Decider picked inspect pipeline on three calls. Each spent its one read and ended unresolved.

## Scene 5. Run 1 pays the learning cost

**1:11 to 1:28.** Planning budget: 17 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-03.png`, followed by demo steps 2, 3, 4.

> Run 1 happens once: one inference, 21.6 seconds of client time, one read, unresolved. Run 1 is not saved. The deterministic verifier stored a Decision Receipt: real data, no decision moved. The model is unchanged. The system recorded a fact.

## Scene 6. Run 2 escapes

**1:28 to 1:51.** Planning budget: 23 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-04.png`, followed by demo steps 5, 6, 7, 8.

> Run 2 is a new call, and the exact receipt matches. Before the first Decider request and before any read, the controller precludes only the proven dead end. The three remaining diagnostics keep their original order. Decider makes one fresh choice, inspect schema keys. One inference, one read, a verified repair, completed in 26.6 seconds.

## Scene 7. The proof

**1:51 to 2:13.** Planning budget: 22 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-05.png`, followed by demo steps 10.

> Old Run 2 took 54.8 seconds and two inferences. Optimized Run 2 takes 26.6 seconds and one inference. That is one fewer inference and an observed difference of 28.2 seconds, or 51.4 percent lower client latency. One synthetic case, each proof once. These are single observations. Old Run 2 is not the memoryless control.

## Scene 8. The private AWS path

**2:13 to 2:29.** Planning budget: 16 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-06.png`.

> Decider serves from a private, always-on Amazon SageMaker endpoint. It is billed by uptime, not requests. One fewer inference is not a dollar saving. Amazon Bedrock Nova Lite narrates only after completion. Narration was refused on Run 1.

## Scene 9. The reusable pattern

**2:29 to 2:42.** Planning budget: 13 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-07.png`.

> The reusable recipe sends current state and matching history to Decider. Only a proven exact repeat is withheld before selection. A receipt cannot pick a tool. Decider chooses from the remaining tools.

## Scene 10. The boundary, restated

**2:42 to 2:55.** Planning budget: 13 s speech, 0 s held.

*Screen:* `submission/pitch/frames/slide-08.png`.

> One failed decision becomes a boundary. History can rule out one proven execution under one identical context. It cannot choose the next tool. Decider chooses. The verifier decides what the read found.

## What must not appear

No account IDs, resource or correlation identifiers, credentials, private URLs,
local paths, personal names, internal process labels or fabricated metrics.
No rate, significance, accuracy, calibration, generic learning, literal infinite
loop, biggest-problem, novelty, production readiness, scoring or guaranteed
savings claim. No self-assessment or rubric weights.

## Timing, in one table

The exact narration has a 175-second planning budget at the measured speech rate.
Scene 2 has 30 words and 13 planned seconds; scene 3 has 37 words and 15 planned
seconds. These are planning estimates, not encoded durations.

Planning budgets only; holds are zero, with no added scene pauses. Unchanged narration
caches are reused. Encoded timing must be strictly below 190 seconds; the two ML scenes
together must be below 33 seconds, near the prior 23.700-second ML chapter.
The 190-second project ceiling is an approved requirement change, not a gate
bypass. Narration, visuals, evidence and every other gate threshold are unchanged.
Measured timestamps belong in `dist/video/README.md`, not this planning table.

| Scene | Start | Spoken words | Speech | Hold | Length |
| --- | --- | --- | --- | --- | --- |
| 1 The hook | 0:00 | 69 | 28 s | 0 s | 28 s |
| 2 Why LoRA is in the decision path | 0:28 | 30 | 13 s | 0 s | 13 s |
| 3 What validation showed | 0:41 | 37 | 15 s | 0 s | 15 s |
| 4 The control stays stuck | 0:56 | 36 | 15 s | 0 s | 15 s |
| 5 Run 1 pays the learning cost | 1:11 | 40 | 17 s | 0 s | 17 s |
| 6 Run 2 escapes | 1:28 | 55 | 23 s | 0 s | 23 s |
| 7 The proof | 1:51 | 54 | 22 s | 0 s | 22 s |
| 8 The private AWS path | 2:13 | 38 | 16 s | 0 s | 16 s |
| 9 The reusable pattern | 2:29 | 32 | 13 s | 0 s | 13 s |
| 10 The boundary, restated | 2:42 | 32 | 13 s | 0 s | 13 s |
| **Total** | | **423** | **175 s** | **0 s** | **175 s** |

## Build status

### Current 190-second ceiling revision: promoted locally

The project duration ceiling is now strictly below 190 seconds. The current
Scene 2 and Scene 3 narration, both ML visuals, all evidence and all other scenes
remain unchanged. Local promotion requires all ten gates to PASS without skips,
warnings or overrides, total duration below 190 seconds and combined ML scenes
below 33 seconds.

Narrow duration, narration and private invariant tests: 90 passed, with no
failures, skips or warnings. The single full build passed all ten gates:
preflight, ssml-lint, prerender-data, timing, stt-roundtrip, stream-uniformity,
blackdetect, ocr-leak, joined-timing and final-artifact. No gate was skipped,
warned or overridden; the build was not retried. All ten narration caches were
reused without synthesis.

The exact probed duration is 178.533333 seconds: 5,356 frames at 30 fps,
6,416,193 bytes. This is byte-identical to the current candidate present before
the ceiling change. The earlier 180.000-second measurement below belongs to a
prior narration revision. Scene 2 is 388 frames (12.933333 repeating seconds),
Scene 3 is 541 frames (18.033333 repeating seconds), and combined ML duration
is 929 frames (30.966666 repeating seconds), below 33 seconds.

Both full-size encoded ML frames, the twenty-window contact sheet and the
decoded transcripts were inspected before promotion. Independent final-audio
STT scored 90% for Scene 2 and 100% for Scene 3, against the unchanged 80%
minimum. The exact transcripts and measured timeline are in
`dist/video/README.md`. Independent verification found 109 clean OCR samples,
uniform streams, clean decode and timestamps, and minimum visual similarity
0.989873. Longest joined silence was 0.78 seconds.

Local promotion completed after fingerprint, digest and validation-binding
checks. Hashes confirm the spec, both ML visuals, every existing narration
cache, replay frames and tracked files outside the allowed scope are unchanged.
Post-promotion gates-only: all ten gates PASS again, without skips, warnings or
overrides. Promoted MP4 and validation report remain byte-identical to the
revalidated candidate and report. Public video tests: 37 passed, no failures,
skips or warnings; 592 unrelated or repository-excluded tests were deselected
by the video/narration selection and unchanged repository configuration.
No git mutation, commit, push, deploy, external upload or publication occurred.

### Historical Scene 3 revision: blocked at the former 180-second limit

Scene 3 uses the requested text verbatim. It contains 37 whitespace-separated
words, rather than the requested label of 36; the exact assertion and planning
count reflect the text. Scene 2 narration and all visual content are unchanged.
The planning budget is 176 seconds, including 14 seconds for Scene 2 and
15 seconds for Scene 3; it is not the measured encoded duration.

The narrow test run returned 79 passed and one failed: the new word-count
assertion initially expected 36 words. The assertion was corrected to 37 words
without retrying the tests. The exact-text assertion passed in that run.

One full build ran with the requested environment. Only Scene 3 narration was
synthesized; all nine other narrations reused their caches. Speech recognition
returned a 100% token match for Scene 3:

> Across 160 decisions, correct first choices rose from 97 to 129. Across 64 calls, wasted first reads fell from 20 to 15 and completions rose from 44 to 49. CPU replay confirmed direction, one seed, experimental checkpoint.

The encoded candidate is exactly 180.000000 seconds, with 5,400 frames at
30 frames per second. Scene 2 is 14.400000 seconds and Scene 3 is 541/30 seconds
(18.033333 repeating); combined ML duration is 973/30 seconds (32.433333
repeating), below 33 seconds. Total duration did not meet the former strictly
below 180-second ceiling; that requirement is superseded by the approved
190-second ceiling above.

The first nine gates passed: preflight, ssml-lint, prerender-data, timing,
stt-roundtrip, stream-uniformity, blackdetect, ocr-leak and joined-timing.
Final-artifact verification failed; the independent result records FAIL and
the wrapper records build-integrity FAIL. The candidate was quarantined.

Nothing was promoted or retried. Post-promotion gates-only and public video
tests were not run because promotion was blocked. Hash comparisons confirm
unchanged ML and replay visuals, all files outside the allowed scope, and
all three distribution files. No commit, push, deploy or external publication
occurred. Private build, test and preservation evidence remains in `video/build/`.

### Historical exact narration revision: blocked before synthesis

Scenes 2 and 3 contain the exact requested narration without expansion. The
planning gate measures 179 seconds total: 33 words and 14 seconds for scene 2,
43 words and 18 seconds for scene 3. Exact narration assertions, private build
invariants and the then-current 180-second planning test passed: 80 tests, without failures,
skips or warnings. The dry-run's preflight, SSML lint and pre-render data gates
all pass.

The single full-build attempt used the required proven Node module path and
default AWS profile. It stopped at replay capture with `node failed; build
quarantined`. The wrapper did not expose the underlying Node error, so a
dependency-resolution cause is not established for this attempt. Build integrity
failed before narration synthesis; the other seven required gates did not run.
No new ML audio, transcripts or measured durations exist for this revision.

The build was not retried. Nothing was promoted; post-promotion gates-only and
public video tests were not run. Hash checks confirm both ML slide visuals, all
existing narration caches, replay frames, scene videos and all three distribution
files retain their prior bytes. All other scene content is unchanged; only the
planning start times move. No dependency, build source or gate threshold changed.
Nothing was committed, pushed, deployed or published externally.

### Prior promoted video (before the narration clarity revision)

The ten-scene update was built once with Stephen generative and promoted locally
only after all ten gates passed without skips, warnings or overrides and visual
review completed. The finished file is 177.233 s, 1280x720, 30 fps, yuv420p,
6,353,491 bytes and 5,317 frames. The two early ML scenes total 29.667 s.
All eight original raw and padded narration caches were reused byte-for-byte;
only the process and validation narration was synthesized. Measured scene timing
is recorded in `dist/video/README.md`.

The twenty-window contact sheet, both encoded ML frames, sampled replay frames
and all ten final-audio transcripts were inspected. Percentages recomputed before
rendering. The source fingerprint, candidate digest and full validation binding
matched before local promotion. Distribution bytes match the verified candidate.
Nothing was committed, deployed, uploaded externally or published.

Post-promotion gates-only: all ten gates PASS again, without skips, warnings or
overrides. Promoted MP4 and validation report remain byte-identical to the
revalidated candidate and report. Independent verification covers 109 clean OCR
samples; final-audio STT is 81%-100% against the unchanged 80% threshold
(process 91%, validation 81%). Longest joined silence is 0.79 s.

Narrow contracts: 82 passed. Configured public video suite after promotion:
38 passed, with no failures, skips or warnings. The 591 deselections are the
video-only selection plus unchanged repository export exclusions. An initial
unfiltered broader story run exposed 33 pre-existing failures involving missing
out-of-scope documents; those files and exclusions were not changed.
