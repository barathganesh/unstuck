# The Unstuck demo video

`unstuck-demo.mp4`, 147.567 seconds, 1280x720, 5.23 MB (5,226,808 bytes).
Eight narrated scenes. 30 fps, yuv420p, 4,427 frames.

The optimized video has been generated, validated, visually reviewed and
promoted to this distribution directory. **It has not been published publicly.**
Public hosting remains a separate owner action; there is no public video URL.

## What it is

Eight current pitch frames, in deck order, with Amazon Polly Stephen generative
narration. Each scene opens on the exact frame embedded in the PowerPoint.
Scenes 2 through 5 also show ten locally captured steps from the offline demo.
Every capture is labelled as a recorded replay of the measured synthetic run,
not a new run. There is no terminal, AWS console, live endpoint or new experiment
in the video. The synthetic voice is the video's narrator, not a person reading
and not the agent's after-completion Amazon Bedrock Nova Lite narration.

The spoken text matches `docs/video-script.md` and `video/spec.yaml`: 356 words.

| Scene | Start | Duration | What it shows |
| --- | --- | --- | --- |
| 1 The hook | 0.000 s | 26.400 s | Run 1 pays once; Run 2 uses the receipt before selection |
| 2 The control | 26.400 s | 14.000 s | Empty report, then three memoryless calls that stay unresolved |
| 3 Run 1 | 40.400 s | 18.467 s | One inference, one read, unresolved, verifier stores the receipt |
| 4 Run 2 | 58.867 s | 22.700 s | Exact match, preclusion before the first request, one choice, verified repair |
| 5 The proof | 81.567 s | 24.000 s | Old and optimized Run 2, with the n = 1 boundary |
| 6 Private AWS path | 105.567 s | 16.133 s | Private execution; endpoint billed by uptime, not requests |
| 7 Reusable pattern | 121.700 s | 12.500 s | Exact negative authority; Decider chooses the next tool |
| 8 The boundary | 134.200 s | 13.367 s | History rules out one execution; the verifier owns truth |

## Honesty statement

Measurements come only from `dist/public-demo/loop-escape-proof.json`: one
synthetic case, each proof once, n = 1. No new inference experiment was run for
this video. The evidence was regenerated without changing a byte.

- Run 1: one inference, 21.6 s client time, one read, unresolved, receipt stored.
  Run 1 is not saved. The model is unchanged; the verifier records a fact.
- Old Run 2: 54.8 s and two inferences. Optimized Run 2: preclusion before the
  first Decider request, one inference, one read, completed in 26.6 s.
- Observed Run 2 client latency is 28.2 s and 51.4% lower in this single
  comparison. Old Run 2 is not the memoryless control.
- No dollar savings are measured. The endpoint is billed by uptime, not
  requests. No broader rate, significance, accuracy, model-learning, novelty,
  production, score or guaranteed-savings claim is made.
- Public frames, narration and validation contain no account or resource
  identifiers, credentials, internal identity values, local filesystem paths
  or hashes. Literal identity patterns remain private.

## Validation

`validation.json` is the report promoted with the exact verified video.
All ten gates returned `PASS`; no skips, warnings or overrides. The full build
ran once. The first eight gates use the installed `video-quality-gates` skill;
the runner adds joined timing and independent final-artifact verification.

| Gate | Result | What it checked |
| --- | --- | --- |
| `preflight` | PASS | Complete toolchain and required filters |
| `ssml-lint` | PASS | All 8 narration segments clean |
| `prerender-data` | PASS | Narration, visual sources and deck XML content clean; eight exact embedded images |
| `timing` | PASS | All segments inside 110–190 words per minute; no dead air at or above 1.0 s |
| `stt-roundtrip` | PASS | 91%–100% per segment against the unchanged 80% threshold |
| `stream-uniformity` | PASS | All 8 segments: 1280x720, yuv420p, 30 fps |
| `blackdetect` | PASS | No black span over 0.4 s |
| `ocr-leak` | PASS | 74 sampled frames clean |
| `joined-timing` | PASS | Seven joins, 4,427 frames at 30 fps; longest silence anywhere 0.79 s |
| `final-artifact` | PASS | Independent full decode, timestamps, final-audio transcription and visual checks; 92 OCR samples clean |

The source scan combines 49 shape patterns and 5 private identity patterns.
OCR also applies the skill's account-number pattern. Final-artifact checks
cover every visual window plus regular samples of the finished MP4. Minimum
visual similarity to the intended source was 0.989873. Final-audio STT again
matched 91%–100% per scene, above 80% throughout.

The contact sheet and full-size encoded replay samples were inspected. The
timeline visibly shows the empty result at 29.400 s, Run 1 receipt at 53.733 s,
Run 2 exact match at 61.867 s, preclusion before the first Decider request at
66.800 s, one fresh choice at 71.733 s and verified repair at 76.667 s. Each
replay step is held for at least 4.9 s. No new run is implied.

## Public export boundary

The recording and its original ten-gate report ship together. The private
build specification, literal identity patterns and narration cache do not
ship, so this export cannot regenerate the video. No gate was waived.
See the root PUBLIC-VALIDATION.md for the independent offline export checks.
No synthesis, deployment, upload or publication occurs in those checks.
