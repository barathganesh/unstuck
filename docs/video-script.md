# Demo video script

Eight scenes, one per pitch slide, in deck order. Each scene opens on the exact
1280x720 pitch frame embedded in the PowerPoint. Scenes 2 through 5 also show
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
at the previously measured 148.7 words per minute, with a 1 second allowance
per scene. Actual encoding retains padding only at the outside edges and uses
whole video frames at joins. The final validation records the rendered timing.

Do not read timecodes aloud.

## Scene 1. The hook

**0:00 to 0:29.** Planning budget: 28 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-01.png`.

> Unstuck gets Decider 2B out of a decision loop. Strands Decider 2B makes tool decisions. But a new call can repeat a decision already proven useless. Unstuck turns that verified dead end into a Decision Receipt. The same Decider then chooses a new path, and no larger model takes over. Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The memoryless control stays stuck.

## Scene 2. The control stays stuck

**0:29 to 0:45.** Planning budget: 15 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-02.png`, followed by demo steps 1, 9.

> This is a recorded replay of the measured synthetic run, not a new run. A report returned zero rows. Without receipts, Decider picked inspect pipeline on three calls. Each spent its one read and ended unresolved.

## Scene 3. Run 1 pays the learning cost

**0:45 to 1:03.** Planning budget: 17 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-03.png`, followed by demo steps 2, 3, 4.

> Run 1 happens once: one inference, 21.6 seconds of client time, one read, unresolved. Run 1 is not saved. The deterministic verifier stored a Decision Receipt: real data, no decision moved. The model is unchanged. The system recorded a fact.

## Scene 4. Run 2 escapes

**1:03 to 1:27.** Planning budget: 23 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-04.png`, followed by demo steps 5, 6, 7, 8.

> Run 2 is a new call, and the exact receipt matches. Before the first Decider request and before any read, the controller precludes only the proven dead end. The three remaining diagnostics keep their original order. Decider makes one fresh choice, inspect schema keys. One inference, one read, a verified repair, completed in 26.6 seconds.

## Scene 5. The proof

**1:27 to 1:50.** Planning budget: 22 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-05.png`, followed by demo steps 10.

> Old Run 2 took 54.8 seconds and two inferences. Optimized Run 2 takes 26.6 seconds and one inference. That is one fewer inference and an observed difference of 28.2 seconds, or 51.4 percent lower client latency. One synthetic case, each proof once. These are single observations. Old Run 2 is not the memoryless control.

## Scene 6. The private AWS path

**1:50 to 2:07.** Planning budget: 16 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-06.png`.

> Decider serves from a private, always-on Amazon SageMaker endpoint. It is billed by uptime, not requests. One fewer inference is not a dollar saving. Amazon Bedrock Nova Lite narrates only after completion. Narration was refused on Run 1.

## Scene 7. The reusable pattern

**2:07 to 2:21.** Planning budget: 13 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-07.png`.

> The reusable recipe sends current state and matching history to Decider. Only a proven exact repeat is withheld before selection. A receipt cannot pick a tool. Decider chooses from the remaining tools.

## Scene 8. The boundary, restated

**2:21 to 2:35.** Planning budget: 13 s speech, 1 s held.

*Screen:* `submission/pitch/frames/slide-08.png`.

> One failed decision becomes a boundary. History can rule out one proven execution under one identical context. It cannot choose the next tool. Decider chooses. The verifier decides what the read found.

## What must not appear

No account IDs, resource or correlation identifiers, credentials, private URLs,
local paths, personal names, internal process labels or fabricated metrics.
No rate, significance, accuracy, calibration, generic learning, literal infinite
loop, biggest-problem, novelty, production readiness, scoring or guaranteed
savings claim. No self-assessment or rubric weights.

## Timing, in one table

Planning budget only. Actual duration must be below 180 seconds and independently
verified from the encoded video, including every join.

| Scene | Start | Spoken words | Speech | Hold | Length |
| --- | --- | --- | --- | --- | --- |
| 1 The hook | 0:00 | 69 | 28 s | 1 s | 29 s |
| 2 The control stays stuck | 0:29 | 36 | 15 s | 1 s | 16 s |
| 3 Run 1 pays the learning cost | 0:45 | 40 | 17 s | 1 s | 18 s |
| 4 Run 2 escapes | 1:03 | 55 | 23 s | 1 s | 24 s |
| 5 The proof | 1:27 | 54 | 22 s | 1 s | 23 s |
| 6 The private AWS path | 1:50 | 38 | 16 s | 1 s | 17 s |
| 7 The reusable pattern | 2:07 | 32 | 13 s | 1 s | 14 s |
| 8 The boundary, restated | 2:21 | 32 | 13 s | 1 s | 14 s |
| **Total** | | **356** | **147 s** | **8 s** | **155 s** |

## Build status

The optimized video was built once and promoted locally after all ten gates
passed without skips, warnings or overrides. The finished file is 147.567 s,
1280x720, 30 fps, yuv420p, 5,226,808 bytes, with eight narrated segments and ten
labelled replay steps. The planning budgets above are not encoded timestamps;
the measured timeline is recorded in `dist/video/README.md`.

The contact sheet and encoded replay samples visibly show the empty result,
Run 1 receipt, Run 2 preclusion before the first Decider request, one choice
and verified repair. Final-audio STT matches 91%–100% against an 80% threshold;
92 independent OCR samples are clean, no black span exceeds 0.4 s, and the
longest silence is 0.79 s. Source fingerprint, candidate digest and validation
binding matched before `build_video.py --publish`; distribution bytes match the
verified candidate. Nothing has been publicly hosted or submitted.
