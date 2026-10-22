/**
 * Assemble unstuck-pitch.pptx from the verified 1280 x 720 frames.
 *
 * Every slide is one full-bleed image of the corresponding HTML slide, so the
 * PPTX is pixel-for-pixel the deck rather than a re-typeset approximation.
 * Speaker notes are read from the deck's own embedded notes block, so the HTML
 * stays the single source for both.
 *
 * Nothing here publishes, uploads, or sends anything.
 */
import { readFile, stat, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import JSZip from "jszip";
import PptxGenJS from "pptxgenjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PITCH = resolve(HERE, "..");
const DECK = join(PITCH, "unstuck-pitch.html");
const FRAMES = join(PITCH, "frames");
const OUT = join(PITCH, "unstuck-pitch.pptx");

const COUNT = 8;
const SLIDE_W = 40 / 3; // inches, LAYOUT_WIDE
const SLIDE_H = 7.5;

/* ---- notes, read from the deck itself ---- */

const html = await readFile(DECK, "utf8");
const block = html.match(
  /<script type="application\/json" id="speaker-notes">([\s\S]*?)<\/script>/,
);
if (!block) throw new Error("the deck carries no speaker-notes block");
const notes = JSON.parse(block[1]);
if (!Array.isArray(notes) || notes.length !== COUNT) {
  throw new Error(`expected ${COUNT} speaker notes, found ${notes.length}`);
}

/* Notes travel into a binary office file. Keep them plain: no bullet glyph,
   no dash that a scrub forbids, no smart quote, nothing outside printable
   ASCII. A breach is a build failure rather than a silent substitution. */
const FORBIDDEN = /[^\x20-\x7E\n]/;
notes.forEach((n, i) => {
  const bad = n.match(FORBIDDEN);
  if (bad) {
    throw new Error(
      `note ${i + 1} carries a non-ASCII character (${JSON.stringify(bad[0])})`,
    );
  }
  if (n.includes("--")) throw new Error(`note ${i + 1} carries a double dash`);
});

/* ---- frames ----
   Each frame is embedded as data rather than referenced by path. A path
   argument would travel into the image's description inside the file, and
   this repository publishes no machine-local path. */

const ALT = [
  "Unstuck gets Decider 2B out of a decision loop. Run 1 pays once. Run 2 uses the receipt before selection. The same Decider chooses a new path, without a larger model taking over. One synthetic case, n = 1.",
  "A new call can repeat a decision already proven useless. A ledger of three memoryless control calls shows each Decider D1 choosing inspect_pipeline, spending 1 of 1 read, and ending unresolved, beside the figure 3 of 3 unresolved and 0 of 3 completed.",
  "Run 1 pays the learning cost once. Empty history, inspect_pipeline, 1 inference, 21.6 s client time, 1 read, unresolved. A stamped Decision Receipt records disposition_unchanged, stored by the deterministic verifier. Run 1 is not saved. The model is unchanged.",
  "Run 2 uses the receipt before selection. On an exact match the controller precludes only inspect_pipeline before the first Decider request and before any read. Three diagnostics remain in original order. Decider makes one fresh choice, inspect_schema_keys, spends one read and completes in 26.6 s client time. One inference. No second Decider call. The verifier owns truth.",
  "Measured Run 2 improvement. Old Run 2, superseded post-choice: 54.8 s client time and 2 inferences. Optimized Run 2, pre-selection: 26.6 s and 1 inference. Observed Run 2 client latency fell 28.2 s, or 51.4%. n = 1, same synthetic case, each proof once. Old Run 2 is not the control. No dollar savings measured.",
  "The private AWS execution path. Left to right: the controller on Amazon Bedrock AgentCore Runtime, Decider 2B on a private Amazon SageMaker endpoint with outbound nothing, the deterministic verifier and repair, and Amazon Bedrock Nova Lite narration after completion only. A rule marks the private execution boundary and signed model calls.",
  "The reusable pattern. Match current context, preclude only the proven exact execution before selection, preserve the original order of the remaining offer, let Decider choose, and verify the outcome. No second model selector. Observed Run 2 uses one inference and one read, with no second Decider call.",
  "One failed decision becomes a boundary. Exact negative authority withholds one proven execution under one identical context. Decider still chooses the next tool. The verifier owns truth. A reusable pattern, with the choice still Decider's. One synthetic case, each proof once. The model is unchanged.",
];

const frames = [];
for (let i = 1; i <= COUNT; i += 1) {
  const file = join(FRAMES, `slide-${String(i).padStart(2, "0")}.png`);
  const info = await stat(file).catch(() => null);
  if (!info || info.size === 0) {
    throw new Error(`frame ${file} is missing or empty; run capture.mjs first`);
  }
  frames.push(`image/png;base64,${(await readFile(file)).toString("base64")}`);
}

/* ---- assembly: one fresh instance, wide layout, hex without a hash ---- */

const pptx = new PptxGenJS();
pptx.layout = "LAYOUT_WIDE";
pptx.title = "Unstuck gets Decider 2B out of a decision loop";
pptx.subject = "Run 1 pays once. Run 2 uses the receipt before selection.";
pptx.company = "Unstuck";

for (let i = 0; i < COUNT; i += 1) {
  const slide = pptx.addSlide();
  slide.background = { color: "FCFBF7" };
  slide.addImage({
    data: frames[i],
    x: 0,
    y: 0,
    w: SLIDE_W,
    h: SLIDE_H,
    altText: ALT[i],
  });
  slide.addNotes(notes[i]);
}

await pptx.writeFile({ fileName: OUT });

/* ---- strip the authoring instants ----
   The writer stamps docProps/core.xml with the moment the build ran. A
   wall-clock instant identifies when a private machine did something, and this
   repository publishes none, so the two date elements are removed rather than
   replaced with an invented date. Both are optional in the format and
   PowerPoint opens the file without them. */

const bundle = await JSZip.loadAsync(await readFile(OUT));
const CORE = "docProps/core.xml";
const core = await bundle.file(CORE)?.async("string");
if (!core) throw new Error(`the written file carries no ${CORE}`);
const stripped = core.replace(
  /<dcterms:(created|modified)[^>]*>[^<]*<\/dcterms:(?:created|modified)>/g,
  "",
);
if (/<dcterms:(created|modified)/.test(stripped)) {
  throw new Error(`${CORE} still carries an authoring instant`);
}
bundle.file(CORE, stripped);
await writeFile(
  OUT,
  await bundle.generateAsync({ type: "nodebuffer", compression: "DEFLATE" }),
);

process.stdout.write(`wrote unstuck-pitch.pptx, ${COUNT} slides\n`);
