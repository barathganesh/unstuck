# Submission checklist, against the official requirements

Nothing in this project has been published and nothing has been submitted. No
push has occurred. Publishing and submitting are two separate owner actions,
and neither one has been taken: approving one is not approving the other, and
this checklist cannot report readiness for either. The eligibility items below
could not be verified at all, because the official Rules and Terms tabs render
client side and the fetch returned no readable rule text. Every requirement
whose completion depends on publication, on submission, or on eligibility is
therefore recorded as `Pending`, and no such item is marked complete anywhere
in this file. The statuses used here are `Met` (named evidence exists in
this repository and was read), `Ready to publish` (the artifact is written and
awaits only an owner's publication decision), `Pending` (the item awaits an
action or a verification that has not happened). Local video promotion is not
public hosting and does not complete the public-video requirement.

The current sequence this checklist is written against: Run 1 pays the learning
cost once. Run 2 uses the receipt before selection. The memoryless control stays
stuck. The written script and outlines follow this sequence. The architecture
figure, offline demo and eight-slide pitch package are current, including the
frames, PowerPoint notes and alt text. The optimized video and its build spec
are current too. All ten video gates passed without skips, warnings or
overrides, and reviewed replay frames show preclusion before selection.
Verified local promotion is complete; public hosting remains Pending.

Run 1 happens once: one inference, 21.6 s client time, unresolved, with the
receipt stored. Run 1 is not saved. The superseded post-D1 Run 2 made two
inferences and took 54.8 s. Optimized Run 2 exact-matches the receipt before
selection, precludes only the proven dead end, and lets Decider make one fresh
choice from the remaining tools. It completes with one inference in 26.6 s.
The observed Run 2 delta is 28.2 s, or 51.4% lower client latency: n = 1, the
same synthetic case, each proof once. The always-on SageMaker endpoint is
billed by uptime, not requests. One fewer inference is not a dollar saving.

## Requirements

| Requirement | Status | Evidence or what it waits for |
| --- | --- | --- |
| "An agent that uses Strands Decider 2B for at least one meaningful decision, paired with Amazon Bedrock" | Met | The meaningful decision is diagnostic tool selection. Decider 2B makes the diagnostic tool choice (D1) and, as a design bound, may make one optional re-decision (D2); it is the only model that selects a tool. In the recorded pair, Run 2 needed no re-decision: the controller matched Run 1's Decision Receipt and precluded only the proven dead end before the first Decider request, so Decider 2B made one fresh choice over the 3 remaining diagnostics and the call completed on one inference and one read. Amazon Bedrock Nova Lite then narrates the deterministic verified result after that result is already fixed, and is refused outright on a result that was rejected, unresolved or never verified. Evidence, lead first: the two loop-escape proofs on one synthetic case, recorded in `dist/public-demo/loop-escape-proof.json` and `docs/claim-register.md`, the loop-escape claim rows (hosted Decider proof, 5 Decider 2B choices, 13 of 13 checks; AgentCore proof, 25 of 25 checks). Earlier record: `docs/hackathon-validation.md`, section "The first cloud deployment, and what it answered"; `dist/public-demo/fixture.json`; `dist/public-demo/final-golden-summary.json`; `docs/claim-register.md`, the earlier two-read cloud-run rows and the superseded post-D1 rows. |
| "A written article on Builder Center describing the agent, what it does, and the decision you handed to Decider 2B" | Ready to publish | `docs/article.md`, written for the current loop-escape sequence: Run 1 pays the learning cost once, Run 2 uses the receipt before selection, the memoryless control stays stuck. It leads with the failed zero-row query and the repeated decision, names the decision handed to Decider 2B, puts the loop-escape proofs first, and keeps all three failed experiments, their frozen verdicts, the superseded post-D1 Run 2 and the earlier two-read pair as labelled history. Every claim in it traces to `docs/claim-register.md`. The rendered `docs/article-outline.md` is current, but the article is the publication artifact. This item waits only on the owner publishing it. |
| "A link to a public repository with the agent's source code and at least one reusable Decider 2B decision pattern": the reusable decision pattern | Met | `recipes/diagnostic-tool-selection/` is the reusable pattern. `tests/public_deliverables/test_independence.py` proves it imports nothing from the application and runs when only its own directory and the standard library are on the import path, so a hidden dependency fails a test. Validation results must be recorded after each written-story change. |
| "A link to a public repository with the agent's source code and at least one reusable Decider 2B decision pattern": the public repository | Pending | The owner's publication decision. No push and no publication has occurred, so there is no public repository and no link to give. |
| "A short, publicly available demo video, under three minutes" | Ready for owner publication; public URL pending | `dist/video/unstuck-demo.mp4` shows the current pre-selection sequence: Run 1 receipt, Run 2 preclusion before the first Decider request, one fresh choice and verified repair. The video is 147.567 s, 1280x720, 30 fps, yuv420p, 5.23 MB, with eight narrated scenes and ten labelled offline replay steps. All ten gates passed without skips, warnings or overrides, including independent final-artifact verification. Final-audio STT is 91%–100% against 80%, 92 independent OCR samples are clean, no black span exceeds 0.4 s and the longest silence is 0.79 s. The contact sheet and encoded replay timeline were inspected; source fingerprint, digest and validation binding matched before explicit local promotion. `dist/video/validation.json` is the gate report; `dist/video/README.md` records the honesty statement and regeneration procedure. Polly supplies a synthetic voice; the replay is not a new run or an AWS console capture. **"Publicly available" is not met** until the owner hosts the video and its link resolves from a logged-out browser. |
| "One decision pattern (below) that your submission demonstrates" | Met | Tool selection. Same evidence as the first row, lead first: `dist/public-demo/loop-escape-proof.json` and `docs/claim-register.md`, the loop-escape claim rows; then the earlier record in `docs/hackathon-validation.md`, `dist/public-demo/fixture.json` and `dist/public-demo/final-golden-summary.json`. |
| "An original agent that has not been published before" | Pending | Verification at submission time. No publication of this agent has occurred, which is consistent with originality, but the requirement is the owner's own attestation and not something an artifact in this repository can prove. |
| Tags: `#strands-decider-2b` plus exactly one pattern tag. This submission uses `#tool-selection`. "Add both before the deadline." | Pending | Applied on the entry at submission time, which has not happened. The repository records both tags in `README.md` (at the top and the foot), in `docs/article.md`, in `docs/video-script.md`, and together in `publishing/outlines.py` as `SUBMISSION_TAGS`, asserted in `tests/public_deliverables/test_outlines.py`. Exactly one pattern tag is permitted, from `#tool-selection`, `#guardrails`, `#policy`, `#model-routing` and `#context-memory` (recorded in `docs/claim-register.md`); this submission's is `#tool-selection`. Confirm the permitted set on the official page at submission time, since it is read from a page that may change. |
| "Open to builders 18 or older with a Builder Center profile" | Pending | The owner's own check. Nothing in this repository can establish age or the existence of a Builder Center profile, and this item is never marked complete here. |
| One-entry rule, country and employee exclusions, intellectual-property terms, and disqualification criteria | Pending, and UNVERIFIED | These could not be read. The official Rules and Terms tabs render client side and the fetch attempt recorded in `docs/claim-register.md` returned no rule text. The content of these rules is unknown, not satisfied. |
| Window: October 16 2026 12:00 AM PDT to October 23 2026 11:59 PM PDT | Pending | Recheck at submission time. The deadline is read off the official page by a person immediately before submitting, because the page is the only authority for it. |

## The official judging criteria, and where each is addressed

These are the organiser's criteria and the organiser's weights, read off the
official page. They are external judging criteria, quoted here so a reader knows
what the entry will be read against. This file is the only place in the
repository where they appear. They are not a self-assessment and do not
predict a judge outcome. The right-hand column names the artifact a judge
would read, and nothing more.

| Official criterion | Official weight | Artifact a judge would read |
| --- | --- | --- |
| Creativity and cool factor | 30% | The Decision Receipt mechanism: `docs/decision-receipt.md`, and the recipe's "Why a receipt is not a cache" section in `recipes/diagnostic-tool-selection/README.md`. |
| Effective use of Decider 2B | 30% | Lead: `dist/public-demo/loop-escape-proof.json` and `docs/hackathon-validation.md`, which record, on one synthetic case, the memoryless control repeating the dead-end choice and ending unresolved on 3 of 3 calls, Run 1 spending its one read on that dead end and the verifier storing the receipt, and Run 2 having that one proven dead end precluded before the first Decider request so Decider 2B chose `inspect_schema_keys` over the 3 remaining diagnostics and completed, 1 of 1, each figure with its denominator. Earlier record: `dist/public-demo/fixture.json` and `dist/public-demo/final-golden-summary.json`. |
| Communication quality | 25% | Current: the written narrative, `docs/architecture.svg`, `docs/demo.html`, the eight-slide pitch package under `submission/pitch/`, and the optimized video and spec. The video has ten passing gates and reviewed replay captures; it was promoted locally after source and artifact binding checks. The deck contains no official criteria or self-assessment. None of these is publicly published yet. |
| Originality | 15% | `recipes/diagnostic-tool-selection/` and the three failed experiments kept in full in `docs/hackathon-validation.md` and `docs/article.md`. No claim of novelty or of prior-art superiority is made. |

A measured latency improvement or a measured cost improvement is a bonus under
the official criteria and not a requirement. The organisers judge
bonus eligibility; this submission does not assign itself a score. Only the
scoped Run 2 client-latency comparison below is claimed, not a cost benefit.

What the record does carry is one measured comparison, at n = 1, between the
superseded post-D1 flow and the current pre-selection flow on the same synthetic
case: Run 2 spends one fewer Decider inference, occupies less model time, and
its observed client elapsed time is 51.4% lower, 26,622.5 ms against 54,795.9 ms.
Those are two single observations, recorded in
`dist/public-demo/loop-escape-proof.json` and registered as rows T1 to T7 of
`docs/claim-register.md`. No dollar or cost saving follows from them: the Decider
is served by a real-time endpoint billed per instance-hour of uptime rather than
per request, so one fewer inference does not by itself lower the bill. Run 1 pays
the learning cost once and is never presented as a saving. Observed spend is
recorded as not measured rather than as zero cost, and the one read difference in
the earlier two-read cloud pair is an existence result on a single pair of calls
and is not a rate.

## Remaining human actions

1. Read the official Rules and Terms tabs in a browser that executes their
   client-side rendering, and record the one-entry rule, the country and
   employee exclusions, the intellectual-property terms, and the
   disqualification criteria.
2. Confirm eligibility personally: 18 or older, with a Builder Center profile.
3. Reread the deadline off the official page and confirm it still reads
   October 23 2026 11:59 PM PDT.
4. Read `docs/article.md` end to end and confirm it says what you want said.
5. Review the completed optimized video in `dist/video/unstuck-demo.mp4` and
   its honesty statement. The single build passed all ten gates; encoded
   replay samples and timeline were reviewed before verified local promotion.
   Watch it end to end before public hosting.
6. Review the current figure, offline demo and eight-slide pitch package before
   publication. The written script, spec and video now use the same sequence.
7. Decide whether to publish, and record that decision explicitly.
8. Publish the repository, the article, and the video, and collect the three
   public links.
9. Open each of those three links from a logged-out browser and confirm each
   one resolves publicly.
10. Decide whether to submit, and record that decision explicitly as a second
    and separate decision.
11. Create the entry, attach the three links, and apply both tags,
    `#strands-decider-2b` and `#tool-selection`.
12. Submit before the deadline.

The judging decision belongs to the organisers.
