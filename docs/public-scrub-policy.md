# Public scrub policy

What a public file in this repository is allowed to say, and what it must never
carry. The scan at `publishing/public_scan.py` enforces this list and fails
closed: any finding is a failure, and the scan refuses to pass if its own
positive controls stop catching a synthetic example.

## The public set

These files are judge-facing and are covered by the scan:

| File | What it is |
| --- | --- |
| `README.md` | The repository front page |
| `docs/article.md` | The written article |
| `docs/video-script.md` | The demo video script |
| `docs/demo.html` | The offline click-through demo |
| `docs/architecture.svg` | The architecture figure |
| `docs/claim-register.md` | Every public claim and its evidence |
| `docs/submission-checklist.md` | What is met and what is pending |
| `docs/hackathon-validation.md` | The full investigation record |
| `docs/public-scrub-policy.md` | This file |
| `dist/public-demo/*.json` | The curated machine-readable record |
| `recipes/diagnostic-tool-selection/**` | The standalone reusable pattern |
| `submission/pitch/README.md` | How the deck is read and regenerated |
| `submission/pitch/unstuck-pitch.html` | The deck, and the single source for its slide text, alt text and speaker notes |
| `submission/pitch/build/capture.mjs` | The frame renderer |
| `submission/pitch/build/make-pptx.mjs` | The PowerPoint assembler |
| `submission/pitch/build/package.json` | The build manifest |

### Two pitch artifacts are covered by a derived reading

`submission/pitch/unstuck-pitch.pptx` and `submission/pitch/frames/slide-01.png`
to `slide-08.png` are not text, so the scan reads them rather than their lines:

| Artifact | What the scan reads |
| --- | --- |
| `unstuck-pitch.pptx` | The inspectable text of its XML parts: the slide text runs, the image alt text, the speaker notes, and the document properties. Zip members only; no machine-local path is opened into the file or read out of it. The build strips the two authoring instants the writer would otherwise stamp into `docProps/core.xml`, because a wall-clock instant is forbidden by the time rule below. |
| `frames/slide-NN.png` | The text a recognizer reads back out of the rendered pixels, which is the one check that sees what a viewer sees rather than what the source says. |

The frame check excludes exactly one pattern, the em dash. Recognition cannot
tell a hyphen from a long dash in a rendered glyph, and it misreads both
`second-decision` and `#tool-selection` in these frames. Typography is authored
in the deck HTML and is checked there, where the character is unambiguous.
Every identity, path, credential and truth pattern still runs over the pixels.

The frame check also needs a recognizer installed. When none is present the scan
says so in its output and does not count the frames as checked, rather than
reporting a pass it did not earn.

`submission/pitch/build/package-lock.json` and `build/node_modules/` are
third-party install state, not authored content: the lock file is excluded by
the build directory's own `.gitignore` and never ships. PowerPoint's `~$` lock
file and the `build/qa/` conversion scratch are ignored for the same reason.

### Two files deliberately outside the scan

`docs/article-outline.md` and `docs/video-outline.md` are **planning
scaffolding, not judge-facing content**. They are rendered from
`publishing/outlines.py`, whose typed model requires each section to name the
requirement it answers to and the gate it waits for, and whose own test asserts
the rendered Markdown matches the module exactly. Their requirement codes are
therefore structural rather than leaked, and the article and script they planned
now exist as real content.

They are excluded from the scan rather than scrubbed, because scrubbing them
means changing the generator's data model. **If this repository is published,
either exclude these two files from the published tree or scrub the generator
first.** That is an owner decision and it has not been taken.

### Pattern-definition files are a third category

A file whose content *is* the denylist cannot be scanned against the denylist.
`publishing/public_scan.py` is the original case: it carries a synthetic
positive control for all 24 of its own patterns, so scanning itself would
report 24 findings and every one of them would be the check working.

Two more files are in that category:

| File | What it is |
| --- | --- |
| `video/spec.yaml` | The demo video's spec. Its `forbidden_patterns` list declares 49 shapes the video gate refuses. |
| `tests/public_deliverables/test_video_forbidden_patterns.py` | The positive and negative controls for that list, 94 of them. |

The test holds its controls in a test file rather than in a frame or in the
narration on purpose: a control string inside a rendered frame would be the
leak it exists to detect.

Both files are held to the standard the policy already sets for generic field
names: **they carry shapes and synthetic examples, and no real value from any
run.** That is asserted, not assumed. A real 12-digit identifier, a real home
directory path, or a real access key in either file is a finding, and the audit
that checks for exactly that is part of producing the video.

The video's own shipped artifacts are **not** in this category and are scanned
clean against all 24 patterns: the two build scripts,
`dist/video/validation.json` and `dist/video/README.md`. Its frames are the
pitch deck's own `submission/pitch/frames/slide-01.png` to `slide-08.png`,
already scanned as part of the deck.

Everything else is private by default, including the whole of `.venv/`, the
private cloud-run record, raw provider responses, invocation bodies, rollback
records, the held-out corpus, and anything under `aidlc/`.

## Allowed in a public file

- **Facts, choices and outcomes.** Tool identifiers that are part of the public
  contract (`inspect_pipeline`, `inspect_schema_keys`, `inspect_scope`),
  outcome classes (`no_progress`, `disposition_unchanged`, `useful_evidence`),
  reason codes (`insufficient_evidence`, `populated_after_repair`,
  `provider_error_decider`), status values (`completed`, `duplicate`, `passed`,
  `matched`, `none`), dispositions, and stage names.
- **Counts with their denominator.** Reads, repairs, selector calls, blocks,
  false blocks, cases, pairs, episodes, telemetry lines, HTTP status counts.
- **Reported confidences**, stated as reported and never as calibrated.
- **Rounded latencies in milliseconds**, where the bound and the availability
  are stated beside them.
- **Verified aggregated rows** from the declared synthetic fixture.
- **Bounds and budgets** as configured values.
- **Whether an invocation was new or a duplicate**, and whether decision
  history was none or matched.
- **Whether a repeated choice was blocked**, and the reason code if it was.
- **Narration status**, its provider, and its rounded latency.
- **Honest negatives**: every Pending, every failed experiment, every verdict
  that stands as written, and every "n = 1, an existence result and not a rate".
- **The upstream model credited by its public name**, "Strands Decider 2B", and
  its public pages.
- **Generic field names inside the reusable recipe's source**, where its public
  API contract requires them (`model_revision`, `receipt_id`, `data_version`,
  `arguments_digest`, `context_digest`). These must hold **no real value**:
  placeholders only.
- **Human wording for the two comparison conditions**: "with receipts" and
  "without receipts".

## Forbidden in a public file

Every item below is a scan pattern. A match is a failure.

### Benchmark arm identity
- The arm labels `fixed`, `llm`, `decider_memory` used as a condition name.
- The fields `baseline_id`, `arm`, `arm_id`.
- Say "with receipts" and "without receipts" instead. Never "arm", never
  "baseline", never a condition label.

### Operational and correlation identity
- Run, request, session, trace, span, correlation, model-call, decision,
  diagnostic, evidence, verification, incident and receipt identifiers.
- Idempotency key values. Internal case identifiers. Execution keys.
- UUIDs in any form.
- Run labels that name a private run (`run-A`, `run-B`).

### Hashes and digests
- Any hex run of 16 characters or more.
- Query, base-query, task, context, decision-context, input, selector-input,
  evidence, disposition, versions, policy, calibration, namespace, identity,
  row or problem-fingerprint hashes, raw or truncated.
- The pinned model revision, raw **or** digested, including a truncated digest
  with an ellipsis.

### Cloud account and resource identity
- AWS account identifiers (any bare 12-digit run).
- ARNs.
- Resource identifiers: `vpc-`, `subnet-`, `sg-`, `i-`, `eni-`, `rtb-`,
  `igw-`, `nat-`, `vpce-`, `ami-`, `snap-`, `vol-`, `acct-`.
- Endpoint names, runtime names, role names, bucket names, repository URIs,
  image digests and Region literals belonging to the private deployment.
- Named AWS services may be described (`Amazon Bedrock AgentCore Runtime`,
  `Amazon SageMaker`, `Amazon Bedrock Nova Lite`): the *service* is public, the
  *resource* is not.

### Paths and locations
- Any absolute local path, including anything beginning `/Users/`, `/home/`,
  `/private/`, `/tmp/`, or a Windows drive path.
- Any path under `.venv/`.
- Private evidence file names, and any reference to "the private ledger" by
  name. Describe the evidence in words and say it is not published.

### Time that identifies the private run
- Any ISO 8601 timestamp carrying a date and a time.
- Any wall-clock window, start instant or end instant of the private run.
- Rounded durations are fine; instants are not.

### Internal process identity
- AI-DLC stage and gate identifiers.
- `PF1` to `PF9`, `GM<n>`, `TST-<id>`, `REQ-<id>`, `AC<n.n.n>`, `FR<n.n>`,
  `NFR<n.n>`.
- Say what the gate or requirement *is*, in plain words.
- A public document's own table numbering (`M1`, `C3`, `D8`) is not a process
  identifier and is allowed.

### People, credentials and truth
- Personal names, aliases, usernames, email addresses, assistant names.
- Internal URLs and internal hostnames.
- Any credential, token, key, secret or session material, and any pattern that
  looks like one (`AKIA`, `ASIA`, `aws_secret_access_key`, a bearer token).
- Evaluator truth: held-out answers, expected labels, graded outcomes, or any
  private capability field that would let a runtime role or a model input see
  what the right answer is.

### Claims
- Novelty, statistical significance, compliance, a latency benefit, a cost
  benefit, a cost saving, production readiness, an achieved rubric score, or a
  completion claim, in any public file, without the recorded evidence the claim
  register names for it.
- No measured outcome may be changed and no failed experiment may be hidden to
  make the record read better.

### Style
- Em dashes.
- Non-inclusive terms: `master`/`slave`, `whitelist`/`blacklist`,
  `whiteday`/`blackday`. Use primary/replica, allowlist/denylist, clear
  day/blocked day.
- Unexplained acronyms. Spell out on first use or replace with words.
- Internal jargon and private implementation labels.

## Running the scan

```sh
uv run python -m publishing.public_scan
```

It prints one line per pattern with its positive-control result, then every
finding with its file, line and matched text. Exit status is 0 only when every
positive control fired and no public file produced a finding.

## Running it over a whole export

The declared set above is what this repository ships to judges. A published
export is a different question, because there every text file ships, source
and tests included:

```sh
python3 -m publishing.public_scan --tree <export root>
```

This mode reads every text file under the given root and prints which of three
classes each one fell into:

| Class | What it covers | Patterns applied |
| --- | --- | --- |
| prose | Documents, curated public data, the front page, the deck | All of them |
| code | Source, configuration, container definitions, package metadata, the shipped package's data and UI assets | The patterns that forbid a field NAME rather than a value are waived, because those names are the public contract of the code that uses them. A line carrying the SHAPE of a value (a validation regex, a format template, a zero-filled example) is allowed through a declared token list. Every pattern that catches a real private value still runs. |
| tests | Everything under `tests/` | Additionally, the patterns that forbid the shape of a value are waived: a leakage test has to carry the shapes it proves are rejected or redacted. This is the same third category the section above describes. |

The claim that a test file holds no real value is enforced by **omission, not
by a shipped denylist of real values**: a list of the real values would publish
exactly what it names. A test file carrying a real value is kept out of the
export.

Declared exceptions are printed on every run with their reason, so a waived
line can be read back out of the scan's own output rather than taken on trust.
