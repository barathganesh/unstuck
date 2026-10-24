# Unstuck submission video

`unstuck-demo.mp4`: **128.800 seconds**, 1280x720, 30 fps, 3,864 frames, H.264/yuv420p with AAC mono audio at 48 kHz. File size: **4,817,595 bytes**. The video is strictly below 180 seconds.

The finished video opens and closes in the project's cream-and-orange visual style. Two animated workflows reveal the decision phases, with animated terminal runs revealing the replay command and agent steps through to the saved AWS results. The closing screen and MP4 artist field credit **Barath Ganesh**.

The story opens with the styled introduction, AWS agent flow and LoRA training. Highlighted source code and terminal demonstrations follow, then the final metric pages and Barath Ganesh closing. The training result is: 25% fewer wasted first reads versus base Decider 2B on the evaluated synthetic workload. The AWS agent uses the base checkpoint; the training study evaluates the adapted checkpoint offline.

Original Amazon Polly synthetic narration is edited at sentence boundaries. The terminal screens summarize recorded evidence from `dist/public-demo/loop-escape-proof.json`; their animation is paced for presentation. Amazon Bedrock Nova Lite is the agent's separate generative step after verified completion.

## Nine narrated scenes

| Scene | Start | Duration | Visual |
| --- | --- | --- | --- |
| 1 The report is empty | 0.000 s | 9.500 s | Styled introduction and diagnostic problem |
| 2 The AWS execution path | 9.500 s | 7.667 s | Styled AWS architecture and phased agent flow |
| 3 Training Decider with verified outcomes | 17.167 s | 12.933 s | Styled LoRA training workflow |
| 4 A reusable selection pattern | 30.100 s | 12.500 s | Highlighted receipt filtering and selector code |
| 5 Run 1 records the dead end | 42.600 s | 16.667 s | Shell run, metrics, structured JSON result |
| 6 Run 2 chooses a new path | 59.267 s | 19.100 s | Shell run, verification, metrics, structured JSON result |
| 7 Controlled AWS success | 78.367 s | 19.033 s | Styled final AWS metrics |
| 8 Controlled fine-tuning success | 97.400 s | 18.033 s | Styled final LoRA metrics |
| 9 Built by Barath Ganesh | 115.433 s | 13.367 s | Styled closing with Barath Ganesh credit |

## Current checks

`validation.json` records checks on this encoded revision: full decode, geometry, duration, black frames, silence, local speech recognition, public text, sampled OCR and visual review. The original private ten-gate report does not certify this edit.

`unstuck-demo.srt` contains 36 optional caption cues. Captions can be enabled on the video host without covering the workflow.

## Edit sources and reproduction

Editable SVG screens, phased visual timings and the local renderer are in [`submission/video/`](../../submission/video/). The spoken script is [`docs/video-script.md`](../../docs/video-script.md). A rebuild needs the preserved original 178.533-second MP4, Python, Node/Sharp and FFmpeg. The delivered MP4 is complete and ready to upload.

## Measurement design

Run 1 pays the learning cost once: one inference, 21.6 s, unresolved, receipt stored. Run 2: one inference, 26.6 s, completed. The earlier Run 2 took two inferences and 54.8 s. The observed difference is 28.2 s, or 51.4% lower client time: n = 1, one synthetic case, each proof once. This compares two Unstuck flows. SageMaker is billed by uptime; the measured comparison concerns latency and inference count.

The terminal sessions show a working local replay command, progressive agent steps, recorded metrics and structured JSON results before returning to a shell prompt. Two editor views show verbatim source excerpts: receipt filtering and selector invocation. An additional SageMaker excerpt remains available in the editable source kit.
