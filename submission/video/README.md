# Local video edit sources

The finished file is `dist/video/unstuck-demo.mp4`: 128.800 seconds. It is ready to watch or upload.

The cut combines a styled introduction and closing with phased animated workflows, two shell-style agent replay sequences with metrics and JSON output, a training-results terminal, and highlighted repository code, the AWS integration and the controlled LoRA result. All screens are editable SVGs. `edit-plan.json` records the exact source audio ranges, narration and phase timing at 30 fps. The terminal-style screens summarize saved results in `dist/public-demo/loop-escape-proof.json` and identify themselves as recorded AWS output.

To reproduce, preserve the original 178.533-second, 5356-frame MP4. With Python, Node.js, Sharp and FFmpeg/ffprobe installed, run from the repository root:

```sh
python submission/video/build.py ORIGINAL.mp4 dist/video/unstuck-demo.mp4
```

For Sharp installed elsewhere, pass `--sharp-module` and its package directory. The renderer crossfades between workflow phases, keeps narration aligned to the source frames and refuses to overwrite its source. Rendering is local.

The original Amazon Polly synthetic narration is reused. The spoken script is `docs/video-script.md`, optional captions are in `dist/video/unstuck-demo.srt`, and current encoded-file checks are in `dist/video/validation.json`. Barath Ganesh is credited visually and in MP4 metadata.

The AWS workflow uses base Decider 2B. The training section presents the frozen LoRA checkpoint's offline controlled success against base Decider, including the study criteria and measured counts.

To replay the same terminal sequence locally:

```sh
python submission/video/replay.py --run 1
python submission/video/replay.py --run 2
python submission/video/replay.py --training
```

This reads the saved proof and prints its decision sequence with presentation pacing. Use `--pace 0` for immediate output.

Final chapter order: styled introduction, AWS agent flow, LoRA training, highlighted source code, two terminal demonstrations, AWS metrics, LoRA metrics, Barath Ganesh closing. Total 128.800 seconds. The training replay command and an additional SageMaker code screen remain reusable supporting assets.
