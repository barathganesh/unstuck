# Public export validation

This is an offline validation of the current public export. Paths in this
document are relative to the export root. Nothing was published or uploaded.
The animated README was finalized on 2026-10-22. Current tests, scanner checks
and integrity checks are distinguished below from retained media validation.

## Current story and measurement boundary

Run 1 pays the learning cost once: one inference, 21,635.7 ms client time
(21.6 s), unresolved, with a Decision Receipt stored. Run 1 has no saving.
Optimized Run 2 exact-matches the receipt before selection, precludes only the
proven dead end, and lets the same Decider choose from the remaining tools.
It completes with one inference and one diagnostic read in 26,622.5 ms
(26.6 s), without a second selector call or a larger model taking over.

The superseded Run 2 made two inferences and took 54,795.9 ms (54.8 s).
The observed client-time difference is 28,173.4 ms (28.2 s), or 51.4% lower.
Selector time in the runtime comparison fell from 52,263.79 ms to
24,781.65 ms: a 27,482.14 ms difference, or 52.6%. The separate hosted-selector
comparison fell from 53,957.49 ms to 25,229.29 ms: 28,728.20 ms, or 53.2%.
These are different measurement scopes, not interchangeable timings.

Every comparison is n = 1, the same synthetic case, each proof run once.
The memoryless control remained unresolved on three independent calls.
These are the curated historical observations in
`dist/public-demo/loop-escape-proof.json`, not new live measurements made
during this validation. There is no general latency, throughput, accuracy or
dollar-savings claim. An always-on endpoint is billed by uptime; one fewer
inference does not itself lower its bill.

## Exact inventory and integrity

- Public files: **263**, including this report and the manifest.
- Manifest entries: **262**, every public file except the manifest itself.
- Bytes covered by the manifest: **10289471**.
- Manifest bytes: **45007**.
- Total public bytes, including the manifest: **10334478**.
- Missing entries, extra public files, duplicate paths, size mismatches and
  digest mismatches: **0** each.

`PUBLIC-MANIFEST.json` contains sorted, stable relative paths, byte counts,
SHA-256 values calculated only from public file bytes, and aggregate totals.
This report is included in the manifest. The manifest does not hash itself.
Environment and repository-metadata directories are outside the public set.
Every entry was re-read from disk and verified after both files were written.
The public path set exactly matches the prior validated export plus
`docs/unstuck-flow.svg` and `tests/public_deliverables/test_readme_flow.py`.
The approved README changed; all other pre-existing public files except this
report and its manifest retain their prior bytes. No stray distribution
ignore file, audit scratch or cache is included.

| Top-level entry | Public files |
| --- | ---: |
| Root files | 7 |
| `src/` | 72 |
| `tests/` | 119 |
| `deployment/` | 15 |
| `infra/` | 7 |
| `publishing/` | 8 |
| `docs/` | 9 |
| `dist/` | 7 |
| `recipes/` | 5 |
| `submission/` | 14 |
| Total | 263 |

## Complete test results

The complete configured export suite collected **2,607** cases:
**2,563 passed, 2 skipped, 42 deselected, 0 failed, 0 errors**.
It ran serially with network access and source-checkout access denied before
this report and manifest were regenerated. The configured selections and
thresholds are unchanged. Only this report and manifest changed afterward;
manifest integrity, scanner, links, XML lint and targeted tests were then rerun.
There was one dependency deprecation warning about the test client's HTTP
transport. It is not a product failure or a dependency-security assessment.

The targeted animation, README/story, scanner and link regression run passed
before and after regeneration: **196 passed, 1 skipped, 33 deselected,
0 failed, 0 errors**. This is a subset of the complete suite, not additional
cases to add to its total. Its skip and deselections are the same existing
private-proof and nonshipping-document boundaries described below.

Commands, run with bytecode disabled and network/source access denied:

```sh
python -B -m pytest -q -rs
python -B -m pytest -q -rs tests/public_deliverables/test_readme_flow.py tests/public_deliverables/test_public_story.py tests/public_deliverables/test_public_scan.py
xmllint --nonet --noout docs/unstuck-flow.svg
```

## Animated README

`README.md`, `docs/unstuck-flow.svg` and
`tests/public_deliverables/test_readme_flow.py` match their approved source
copies byte-for-byte. The source copies were read only for equality checks;
tests used export files with source access denied. The three public files were
not edited during finalization.

All **65 animation/README regression cases passed**, including positive
controls for malformed geometry, unsafe SVG content, wrong README links and
each of the **24 scanner controls** injected into the SVG.

- XML lint passed with external network access disabled. The accessible SVG
  has a **1280 x 720** viewBox and dimensions, linked title and description,
  unique identifiers and a responsive 16:9 presentation.
- All CSS animations use one **28-second** cycle. The top track is a closed
  symmetric circuit with equal **51-unit** circular end radii and tangent
  horizontal joins. Its moving signal follows the exact visible path and
  depicts three separate bounded calls, not an unbounded retry loop.
- Run 1 occurs once and creates the receipt. Run 2 is an open forward fork,
  not a return loop. The receipt arrives before the dead-end gate closes;
  the remaining route opens before the same Decider makes its one choice.
  The branch cut, completed label, readable holds and terminal mark satisfy
  the authored geometry and timing contracts.
- Reduced motion disables animations and hides moving signals while keeping
  the full final comparison, receipt, branch cut and completed route visible.
  The same readable final state is the base fallback without motion support.
- No JavaScript, event handlers, external references, embedded assets, CSS
  imports or remote fonts occur in the SVG. Private-value checks, stale
  second-choice story checks and design/self-scoring checks passed unchanged.
- The README references the local SVG exactly once near the opening, with
  descriptive alt text and the approved caption. Description and alt text
  preserve the causal sequence, the n = 1 measurement boundary and the lack
  of measured dollar savings. Timing choreography is not measured latency.

These are automated XML, CSS, geometry, story and accessibility-contract
checks. No new native-browser or manual accessibility review is claimed.

## Existing test boundaries

Both skips are existing conditional checks, not newly added exclusions:

1. The manual-accessibility record check needs the omitted
   `docs/operator-experience.md`. Automated shipped accessibility tests run;
   this is not evidence of a completed manual accessibility review.
2. The curated-record equality check needs private live-proof inputs. Those
   inputs do not ship. Synthetic generator, rejection, receipt and public
   evidence checks still run; live-proof equality was not re-measured.

### Every deselection was audited

In the prior pre-animation validation, all 42 explicit node selections in
`pyproject.toml` were executed separately with deselection disabled. That audit
record was checked again; configuration and omitted inputs remain unchanged.
Each selected exactly one case and failed only at
an absent, intentionally nonshipping Markdown input: 41 `FileNotFoundError`
exceptions and one assertion that an outline document exists. None passed,
failed on product behavior, or referred to a shipped document.
The intentional failures in that audit are not failures of the configured suite.

| Omitted document input | Explicit cases | Why the input does not ship |
| --- | ---: | --- |
| `docs/core-runtime.md` | 1 | Internal runtime design/reference record; the source-role scan runs separately. |
| `docs/telemetry.md` | 1 | Internal telemetry reference; shipped telemetry security and configuration checks run. |
| `deployment/agentcore/README.md` | 1 | Nonshipping deployment/build reference; container and runtime code remains tested. |
| `deployment/README.md` | 1 | Nonshipping deployment/build reference; executable deployment checks remain active. |
| `deployment/sagemaker/README.md` | 1 | Nonshipping serving reference; serving contracts and containers remain tested. |
| `docs/article-outline.md`, `docs/video-outline.md` | 11 | Planning outlines, not the shipped article or video script. One case compares both outlines and counts once. |
| `PRODUCT.md` | 9 | Internal product/planning record, not the public front page. |
| `docs/decision-receipt.md` | 4 | Internal design reference; receipt behavior and security tests remain active. |
| `docs/agentcore-deployment.md` | 4 | Internal deployment/evidence reference, not a runtime input. |
| `docs/public-deliverables.md` | 5 | Internal deliverable-status record; checks of the shipped video and checklist run separately. |
| `AGENT.md` | 4 | Internal agent/process guidance, not a product artifact. |
| Total | 42 | Nonshipping document checks only. |

No shipped behavior, security, receipt, runtime, recipe, story, visual or video
test is deselected. Mixed checks were separated so missing documentation no
longer hides checks of shipped source, publication modules, notices or video.
Telemetry ownership lists now explicitly enumerate the shipped modules;
the omitted evaluator module is not treated as shipped code. No dynamic
missing-file filter, new skip, dependency change or assertion relaxation was added.

### Negative fixture sanitation

During the prior export sanitation, the private-pattern match in
`tests/confidence_cascade/test_bounds.py` was a
personal-name token in a negative forbidden-vocabulary fixture, not a runtime
value. The same fixture token also appeared in
`tests/core_runtime/test_od02_history.py`. Both were replaced by a synthetic
identity sentinel. Neither test file was omitted. Source and emitted-result
assertions remain active. The exact private token is not reproduced here.
All private patterns and all 24 public scanner controls remain unchanged.

## Clean offline execution

In the prior pre-animation validation, a fresh wheel was built from this export
and installed into a new isolated
environment using only local wheel files, with package-index access disabled.
This was a non-editable install. Installed-module paths were checked to ensure
they came from the new environment rather than the checkout.
The retained successful execution record was checked again. Runtime, package
configuration, deployment and recipe file hashes are unchanged; the wheel
was not rebuilt for this documentation-only finalization.

| Check | Result |
| --- | --- |
| Clean wheel install | Passed, no package download |
| Four import entry points | Passed; no evaluator, publishing package or cloud SDK imported |
| Offline doctor | Passed; mode offline, provider mock, live checks Pending |
| Offline skeleton | Completed |
| Shipped scenarios | 2 passed: cold uses 2 choices/2 reads; history uses 1 choice/1 read |
| Standalone recipe | 2 catalogs, 4 calls passed, standard library only |

The recipe runs from a copied directory with interpreter site packages disabled.
In each catalog the cold call uses two decisions and two tool executions. The
later call precludes the exact repeat before selection and uses one decision
and one execution. These demonstrations are synthetic and measure nothing.
The shipped scenario module is the earlier two-read demonstration; it is not
a reproduction of the curated optimized one-read live proof. The front-page
quick start names the shipped module rather than the omitted evaluator.

## Public scanner, links and private-value checks

- Public scanner controls: **24/24 passed**, with **0 findings** in the declared
  artifact scan and in the complete export-tree scan.
- Tree coverage: **26 prose files, 106 code/configuration files, 119 test files,
  10 binary files**. Binary contents receive the media checks below.
- Local links checked: **33**, with **0 missing or escaping the export root**.
  The **1 external link** was recognized but not contacted.
- Additional private denylist: **5 identity expressions and 1 known private
  digest**, with **0 matches**. No private denylist value is published.
- Internal-URL checks: **0 matches**. Public files contain no source-checkout
  paths. Every public file is regular, with no public symlinks or install caches.

The scanner's existing class policy is preserved. Prose receives all 24
controls. Code may contain public schema names, container paths and declared
synthetic shapes; negative tests must contain the shapes they reject. Public
upstream container pins and public-manifest hashes have existing, named
allowances. Scanner source and scrub-policy examples are not scanned against
their own examples; the separate private-value scan includes both files.
These are existing policies, not new exemptions introduced to pass validation.
OCR cannot reliably distinguish a long dash from a hyphen, so the existing
OCR typography exclusion remains; authored deck text is checked directly.

## PowerPoint and video

`submission/pitch/unstuck-pitch.pptx` has **8 slides and 8 speaker-note parts**.
All **48 XML/relationship parts** parsed and passed private-value checks.
Each slide embeds exactly the bytes of its corresponding shipped frame.
All **8 frames** are **1280 x 720** and passed OCR and private-pattern checks.
The final slide and its notes contain no rubric or self-scoring claims.

`dist/video/unstuck-demo.mp4` is **147.567 seconds** to three decimal places.
The exact ffprobe duration is **147.566667 seconds**, matching **4,427 decoded
video frames at 30 fps**. File size is **5,226,808 bytes**. Video is H.264,
1280 x 720, yuv420p; audio is mono AAC at 48 kHz.

All **10/10 video gates** remain PASS in both the shipped validation report
and the retained independent offline audit record. They were run during the
prior pre-animation validation, not rerun for this README-only finalization.
The MP4, PPTX and shipped validation-report sizes and SHA-256 values exactly
match the prior public manifest. The video and all **12 public visual and
narration inputs** also match the retained audit copies byte-for-byte.
No new transcription, synthesis or expensive video-gate run was performed.
PPTX XML, embedded frames, frame OCR and local video metadata/frame count were
checked again; the table below records the retained ten-gate results.

| Gate | Result |
| --- | --- |
| Preflight | PASS |
| SSML lint | PASS, 8 narrated segments |
| Prerender data | PASS, narration parity, current sources, XML and embedded frames |
| Timing | PASS, 110-190 words/minute bounds retained |
| Speech roundtrip | PASS, prior local transcription; 80% minimum retained |
| Stream uniformity | PASS, all 8 segments |
| Black-frame detection | PASS, no black span longer than 0.4 s |
| OCR leakage | PASS, 74 samples at two-second intervals |
| Joined timing | PASS, 7 joins; no dead air at or above 1.0 s |
| Final artifact | PASS, full decode, timestamps, transcription, visual comparison and 92 OCR samples |

The retained independent final-artifact check includes 18 visual-window samples and
74 interval samples, 8 narrated segments and 10 labelled recorded-replay steps.
Minimum visual similarity to the corresponding current sources is **0.989873**,
above the unchanged 0.92 threshold. Longest detected silence is **0.79 s**.
All **54 merged video patterns** remain active. Gate code and thresholds were
not weakened. Nonshipping validation inputs and tools were copied read-only
into local audit scratch. The gates ran there with network access and source
checkout access denied, and copies of public inputs were compared byte-for-byte
to the export before and after that prior validation. Current hash and
byte-equality checks confirm these records still apply to the unchanged media.
No video was synthesized or rebuilt.

## Other intentional omissions and limitations

- Internal design and planning records are outside the allowlist. They can
  carry process identifiers, private evidence references or deployment pins.
  The table above identifies exactly which missing documents account for the
  42 explicit deselections; none is a required shipped runtime input.
- The evaluator, held-out corpus, raw provider responses, invocation bodies,
  rollback evidence and private live-proof files do not ship. Curated public
  observations remain available, but this export cannot re-measure them.
- Video build specification, machine-local patterns, synthesis/capture tooling
  and builder-only tests do not ship. The video passed the local audit using
  nonshipping inputs; the public export alone cannot reproduce that build or
  its private-pattern gates. The shipped public scanner and artifact tests
  remain runnable with their documented local dependencies.
- Environment state, byte caches, editor settings, machine configuration,
  third-party installation trees, package lock/install artifacts excluded by
  the allowlist, office lock files and conversion scratch do not ship.
  Local audit scratch is excluded from the public file set and manifest.
- No live AWS call, deployment, network request or dependency-advisory lookup
  was performed. External links were not fetched. Optional cloud suites use
  offline synthesis, fakes or injected transports rather than live services.
- Dependency, model and base-image licence verification remains pending as
  stated in `NOTICE`. This audit is not a licence determination.
- No native PowerPoint application review or new manual accessibility review
  was performed. XML, exact embedded image comparison and OCR are automated
  checks, not proof that every aspect of presentation design is ideal.

Validation ran serially. No subagent, orchestration workflow, Git command,
staging, commit, push, visibility change or remote publication was performed.
The source checkout was not modified. This destination is ready for staging,
subject to the measurement and reproducibility limitations stated here.
