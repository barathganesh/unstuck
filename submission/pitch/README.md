# The pitch deck

Eight plates on the current pre-selection sequence. Run 1 pays the learning
cost once: 1 inference, 21.6 s client time, unresolved, receipt stored. Run 1 is
not saved. Run 2 exact-precludes only the proven dead end before the first
Decider request, preserves the remaining 3 diagnostics in original order, and
lets Decider make one fresh choice: 1 inference, 1 read, completed in 26.6 s.
The memoryless control stays 3 of 3 unresolved.

The measured comparison is confined to slide 5: superseded post-choice Run 2,
54.8 s and 2 inferences, versus optimized Run 2, 26.6 s and 1 inference.
Observed Run 2 client latency fell 28.2 s, or 51.4%. n = 1, same synthetic case,
each proof once. No dollar savings measured. All numbers come from
`dist/public-demo/loop-escape-proof.json`. The closing slide states the boundary:
exact negative authority, Decider still chooses, the verifier owns truth,
a reusable pattern. The deck contains no judging criteria or self-assessment.

Tags: `#strands-decider-2b` `#tool-selection`

The PowerPoint file is optional for the submission. It exists so the spoken
pitch and the demo video have a visual spine that is honest about its
denominators.

## What is here

| File | What it is |
| --- | --- |
| `unstuck-pitch.html` | The deck itself. One self-contained file, no network call and no external asset. |
| `frames/slide-01.png` to `frames/slide-08.png` | Each slide rendered at exactly 1280 x 720, the demo video's capture size. |
| `unstuck-pitch.pptx` | Eight slides, each one verified frame placed full bleed, with speaker notes. |
| `build/` | The two local scripts that produce the frames and the PowerPoint file. |

## The HTML is the source

`unstuck-pitch.html` drives everything else. The frames are screenshots of it,
the PowerPoint file is those frames, and the speaker notes live inside the HTML
in one embedded block that the PowerPoint build reads. Editing the HTML and
regenerating is the only supported way to change the deck. Nothing is authored
twice.

The frames are the video's target capture size. The existing rendered video
still embeds the superseded frames and narration. It must be rebuilt separately
from the current deck and written script before it can be described as current.

## Open it

```sh
open submission/pitch/unstuck-pitch.html     # macOS
xdg-open submission/pitch/unstuck-pitch.html # Linux
```

Any modern browser works, straight off the file system. There is no server, no
build step and no install to read the deck.

| Key | Does |
| --- | --- |
| Right arrow, Down arrow, Space, Page Down | Next slide |
| Left arrow, Up arrow, Backspace, Page Up | Previous slide |
| `1` to `8` | Jump to that slide |
| `Home`, `End` | First slide, last slide |
| `f` | Toggle full screen |

The slide number is in the address bar, so `unstuck-pitch.html#4` opens on
slide four. Motion is one authored moment on slide four and is dropped
entirely when the reader's system asks for reduced motion.

Printing gives eight exact 16:9 pages, one slide per page, with the on-screen
controls removed.

## Regenerate

Node and npm are the only prerequisites. The first command installs a headless
browser and the PowerPoint writer locally, under `build/`.

```sh
cd submission/pitch/build
npm install
npm run frames    # re-render frames/slide-01.png to slide-08.png at 1280 x 720
npm run pptx      # rebuild unstuck-pitch.pptx from those frames
npm run all       # both, in order
```

`npm run frames` fails if the deck tries to reach the network, if a script
error is raised, or if the slide count is not eight. `npm run pptx` fails if a
frame is missing, if the note count does not match the slide count, or if a
note carries a character outside printable ASCII.

## Checking the PowerPoint file

```sh
cd submission/pitch
python3 -m markitdown unstuck-pitch.pptx
soffice --headless --convert-to pdf --outdir build/qa unstuck-pitch.pptx
pdftoppm -png -scale-to-x 1280 -scale-to-y 720 build/qa/unstuck-pitch.pdf build/qa/render
```

`build/qa/` is scratch output and is not part of the deliverable. It is ignored
by Git and deleted after the check, along with PowerPoint's own `~$` lock file
if an editor left one behind.

The extraction should show eight slides, each one image with alt text plus its
speaker note, and no body text. The converted pages should be 960 x 540 points,
which is 16:9, and each rendered page should match its source frame.

## What the deck claims, and what it does not

Every figure on a slide comes from `dist/public-demo/loop-escape-proof.json`
and sits beside its denominator: one synthetic case, each proof run once,
`n = 1`, an existence result and not a rate. The deck claims nothing about the
model's general accuracy, nothing about calibration, no model learning (the
model is unchanged between calls), no literal infinite loop (each call is
bounded at two Decider choices), no statistical significance, no general latency
benefit, no cost benefit, no cost saving, no production readiness, no novelty,
and no achieved score or predicted judge outcome. No account, resource, or Region identifier, no operational identifier,
no hash, and no machine-local path appears in any shipping file here.

That last sentence is checked rather than asserted. Every shipping file in this
directory is in the public set of `publishing/public_scan.py`: the HTML, this
README, the two build scripts and the build manifest are scanned line by line,
the PowerPoint file is scanned through the inspectable text of its XML parts
(slide runs, image alt text, speaker notes, document properties), and the eight
frames are scanned through the text a recognizer reads back out of their pixels.
The PowerPoint build also strips the authoring instants the writer stamps into
the file, because a wall-clock instant identifies when a private machine ran.
Run it with `uv run python -m publishing.public_scan` from the repository root.

Slide eight closes on exact negative authority: Decider still chooses, the
verifier owns truth, and the pattern is reusable. Only slide five carries a
percentage, the measured n = 1 Run 2 client-latency delta.
