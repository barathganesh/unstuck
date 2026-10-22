# Claim register for the public submission package

Every claim that any public file is allowed to make, with the evidence it rests
on. A claim absent from this table does not go into `README.md`,
`docs/article.md`, `docs/video-script.md`, `docs/demo.html`,
`docs/architecture.svg`, `docs/submission-checklist.md`,
`dist/public-demo/loop-escape-proof.json`, or
`dist/public-demo/final-golden-summary.json`.

The cloud run's write records, its raw provider responses, its invocation bodies
and its rollback records are **private and are not published**. They are
retained locally outside version control. This register describes them in words
rather than naming any file or path, so that this file is itself safe to
publish. Everything a reader can check is in the public artifacts named below.

Two exclusions apply to the lead rows below. The drawn artifacts and the video
(`docs/architecture.svg`, `docs/demo.html`, the pitch deck under
`submission/pitch/`, and the rendered video) still show the **superseded
post-D1 flow** and are pending a rebuild, so the lead rows do not describe
them; what they currently show is recorded in the superseded section. And the
official judging criteria and their weights are recorded once, in
`docs/submission-checklist.md`, framed as external criteria; no weight, score,
estimate or point total appears in this file.

Run 1 happens once: one inference, 21.6 s client time, unresolved, with the
receipt stored. Run 1 is not saved. The superseded post-D1 Run 2 made two
inferences and took 54.8 s. Optimized Run 2 exact-matches the receipt before
selection, precludes only the proven dead end, and lets Decider make one fresh
choice from the remaining tools. It completes with one inference in 26.6 s.
The observed Run 2 delta is 28.2 s, or 51.4% lower client latency: n = 1, the
same synthetic case, each proof once. The always-on SageMaker endpoint is
billed by uptime, not requests. One fewer inference is not a dollar saving.

## Status key

| Status | Meaning |
| --- | --- |
| Supported | Named evidence exists and was read. The claim may be made publicly, in the exact scope stated. |
| Supported, scope-bound | As above, and the stated scope is the whole claim. Reported at n = 1 where the row says so, never as a rate or a general figure. |
| Supported, superseded | The evidence stands and the claim held of an earlier flow. It may appear only inside a section labelled superseded or historical, never as a current claim. |
| Pending | The claim has no evidence yet. It may be named as awaited, never as achieved. |
| Forbidden | The claim is outside what this project will assert at all, regardless of future evidence. |

## Mechanism claims

| # | Claim, in the scope it may be stated | Status | Evidence |
| --- | --- | --- | --- |
| M1 | Decider 2B makes the first decision and the optional re-decision. It is the only model that selects a tool. | Supported | `AGENT.md`; the private cloud-run record (not published), golden-run section |
| M2 | A Decision Receipt is written by the deterministic verifier from observed facts, never by the model. | Supported | `docs/decision-receipt.md`, "What a Decision Receipt is, and is not"; `recipes/diagnostic-tool-selection/selector.py` `classify_outcome`, `make_receipt` |
| M3 | A receipt's only authority is negative: it can refuse one identical repetition and can do nothing else. | Supported | `docs/decision-receipt.md`, "What a Decision Receipt is, and is not"; `docs/public-deliverables.md`, "The recipe" |
| M4 | Preclusion requires an exact match on capability, arguments and decision context, and one of the two disposition-negative outcome classes (`no_progress`, `disposition_unchanged`). | Supported | `docs/hackathon-validation.md`, section "The corrected rule"; `recipes/diagnostic-tool-selection/README.md` "Why a receipt is not a cache" |
| M5 | Receipt identity is content, not time. There is no expiry and no time window anywhere. | Supported | `docs/public-deliverables.md`, "The recipe"; the project rule forbidding any time-window treatment of a receipt |
| M6 | Hard bounds, as a design bound: at most two Decider choices per call (D1 and one optional D2 re-decision) and at most two diagnostic reads per call. No third decision. Both loop-escape proofs configured the read bound to one read per call, and the observed Run 2 used one Decider choice of the two the bound allows. | Supported | `docs/decision-receipt.md`, "Hard bounds"; `dist/public-demo/loop-escape-proof.json` |
| M7 | A precluded capability costs zero diagnostic reads and, when the preclusion lands before the Decider request, zero inferences. | Supported | `docs/decision-receipt.md`, "The bounded loop"; `dist/public-demo/loop-escape-proof.json` |
| M8 | Current authoritative facts dominate history: change the evidence, the candidates or a bound version and the receipt stops matching exactly and stops precluding. | Supported | `docs/hackathon-validation.md`, section "The corrected rule" |
| M9 | Only allowlisted read-only argument-free diagnostics are offerable. | Supported | `docs/hackathon-validation.md`, section "What changed in the system to make this reachable"; `docs/core-runtime.md` |
| M10 | Narration runs after the terminal result is fixed, is handed counted facts, and is refused on a result that was rejected, unresolved or never verified. | Supported | `docs/hackathon-validation.md`, section "What narration cannot affect" |

## Loop-escape claims, denominator one synthetic case

The lead claims. Source for every row: `dist/public-demo/loop-escape-proof.json`
and the private loop-escape proof records (not published). Both proofs ran on
one declared synthetic fixture, one repairable case, with the diagnostic read
bound configured to one read per call, and each was run once. Every row is an
existence result on that one case, not a rate.

The sequence these rows record: Run 1 pays the learning cost once. Run 2 uses
the receipt before selection. The memoryless control stays stuck.

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| E1 | The elevator pitch: "Decider 2B makes tool decisions, but a new call can repeat a decision already proven useless. Unstuck turns that verified dead end into a Decision Receipt, forcing the same Decider to choose a new path and escape the decision loop, without a larger model taking over." Scope: the two proofs below, one synthetic case. | Supported, scope-bound | rows E2 to E9, A1 to A6 and T1 to T7 |
| E2 | Hosted Decider proof: verdict PASS, 13 of 13 checks, 5 Decider 2B choices across 5 calls from one pinned checkpoint on a private Amazon SageMaker endpoint, run once. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| E3 | Memoryless control, without receipts: 3 independent calls. Each call was offered all 4 diagnostics, chose `inspect_pipeline` (3 of 3), spent its 1 allowed read, and ended `unresolved` / `diagnostic_reads_exhausted` (3 of 3). | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| E4 | Run 1, with receipts: decision history empty, all 4 diagnostics offered, Decider 2B chose `inspect_pipeline`, 1 inference, 1 read, ended `unresolved` / `diagnostic_reads_exhausted`. The verifier then stored a `disposition_unchanged` receipt citing that choice. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| E5 | Run 2, a new call on the same problem: the controller matched the Run 1 receipt exactly and precluded only that one proven dead end, before the first Decider request and before any read. Nothing else was removed from the option set. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| E6 | Run 2's single Decider request was offered the 3 remaining diagnostics in their original order and made one fresh choice, `inspect_schema_keys`. 1 inference, 1 read, repair applied, verification passed, `completed` / `populated_after_repair` (1 of 1). No second Decider call was made. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| E7 | 1 preclusion across the 5 hosted choices, and that preclusion cited the receipt Run 1 wrote. 0 false preclusions in the observed sequence of 5 choices. | Supported | `loop-escape-proof.json` |
| E8 | The preclusion cost zero reads and zero inferences, so Run 2's one read and one inference both went to a path not already proven useless. That is the difference between the control and Run 2 on this case. | Supported | `loop-escape-proof.json`; `docs/decision-receipt.md`, "The bounded loop" |
| E9 | The design bound was not reached. Each call allows at most two Decider choices, D1 and one optional D2 re-decision; the observed Run 2 used one, so the escape happened inside the first choice rather than through a re-decision. | Supported | `loop-escape-proof.json`; `docs/decision-receipt.md`, "Hard bounds" |
| A1 | AgentCore proof: the same Run 1 and Run 2 pair through the hosted controller on a private Amazon Bedrock AgentCore Runtime, one session, same one-read bound, same fixture. Verdict PASS, 25 of 25 checks. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| A2 | Run 1: history empty, Decider 2B chose `inspect_pipeline`, executed once, 1 inference, 1 read, `disposition_unchanged` receipt written, `unresolved` / `diagnostic_reads_exhausted`. Narration was refused, because nothing was verified. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| A3 | Run 2: history matched the exact receipt Run 1 wrote, the preclusion landed before the first Decider request and before any read, the one Decider choice was `inspect_schema_keys`, 1 read, 1 repair, `completed` / `populated_after_repair`, and no second Decider call was made. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| A4 | Amazon Bedrock Nova Lite narrated Run 2 only after the deterministic result was fixed. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| A5 | 2 Decider answers served for this pair, one per call, each accepted with HTTP 200 and 0 errors. 1 preclusion, 0 false preclusions. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |
| A6 | Two telemetry timelines, 15 of 15 and 21 of 21 events, both finalized, 0 dropped and 0 refused. | Supported | `loop-escape-proof.json`; the private loop-escape proof records (not published) |

## The one measured comparison, basis n = 1

One comparison per surface between the superseded post-D1 flow and the
pre-selection flow, on the same synthetic case, each proof run once. The
superseded post-D1 Run 2 is a separate earlier run of that flow against the
same endpoint, so these rows compare two single observations and nothing more.
Client time is the caller's wall time for one runtime invocation. Selector time
is the measured Decider inference time, summed over the inferences one call
made. Source for every row: `dist/public-demo/loop-escape-proof.json`, its
`measured_latency` section. The values below retain the recorded milliseconds, with rounded seconds
shown alongside them.

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| T1 | AgentCore client time: Run 1 21,635.7 ms (21.6 s, 1 inference); superseded post-D1 Run 2 54,795.9 ms (54.8 s, 2 inferences); pre-selection Run 2 26,622.5 ms (26.6 s, 1 inference). | Supported, one observation each | `loop-escape-proof.json` `measured_latency.agentcore_client_time` |
| T2 | The two Run 2 client times differ by 28,173.4 ms (28.2 s): observed Run 2 client latency 51.4% lower, n = 1. Stated only as this one observed comparison on one case. | Supported, scope-bound | `loop-escape-proof.json` `measured_latency.agentcore_client_time.difference` |
| T3 | AgentCore selector time, the summed Decider inference time: superseded post-D1 Run 2 52,263.79 ms (52.3 s); pre-selection Run 2 24,781.65 ms (24.8 s); difference 27,482.14 ms (27.5 s), 52.6% lower, n = 1. | Supported, scope-bound | `loop-escape-proof.json` `measured_latency.agentcore_selector_time` |
| T4 | Hosted direct selector time, the experiment harness against the SageMaker endpoint: Run 1 20,651.01 ms (20.7 s, 1 inference); superseded post-D1 Run 2 53,957.49 ms (54.0 s, 2 inferences); pre-selection Run 2 25,229.29 ms (25.2 s, 1 inference); difference 28,728.20 ms (28.7 s), 53.2% lower, n = 1. | Supported, scope-bound | `loop-escape-proof.json` `measured_latency.hosted_selector_time` |
| T5 | What the pair shows, in these words and no stronger: one fewer inference on Run 2, less occupied model time, and a lower observed Run 2 elapsed time on this one case at n = 1. | Supported, scope-bound | rows T1 to T4 |
| T6 | No dollar or cost saving is measured and none is claimed. The Decider is served by a SageMaker real-time endpoint billed per instance-hour of uptime rather than per request, so one fewer inference does not by itself lower the bill. | Supported | `loop-escape-proof.json` `measured_latency.no_dollar_saving` |
| T7 | Run 1 has no saving and is never presented as one. It pays the learning cost once, and the pre-selection preclusion leaves it unchanged: one inference, one read, unresolved, receipt stored. | Supported | `loop-escape-proof.json` `measured_latency.run_1_is_not_saved` |

## The superseded post-D1 Run 2, kept as labelled history

**Superseded as the lead claim, not erased.** These rows describe an earlier run
of the superseded flow, on the same case against the same endpoint, where the
preclusion landed *after* the first Decider choice instead of before it. No
current claim describes Run 2 this way.

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| H1 | In the superseded flow, Run 2's first Decider choice was offered the full set of 4 diagnostics and repeated `inspect_pipeline`, the proven dead end. The controller refused that exact repeat before any read. | Supported, superseded | `loop-escape-proof.json` `earlier_evidence`; the private records (not published) |
| H2 | A second Decider call then chose `inspect_schema_keys`, so that Run 2 spent 2 inferences instead of 1 and finished `completed` / `populated_after_repair`. | Supported, superseded | `loop-escape-proof.json` `earlier_evidence`; the private records (not published) |
| H3 | Its recorded times are the superseded side of rows T1, T3 and T4: 54,795.9 ms AgentCore client, 52,263.79 ms AgentCore selector, 53,957.49 ms hosted selector. | Supported, superseded | `loop-escape-proof.json` `measured_latency` |
| H4 | The drawn artifacts and the rendered video still show this superseded post-D1 sequence and are pending a rebuild. They are not current and must not be described as current. | Supported | `docs/public-deliverables.md`, "The visuals"; `docs/submission-checklist.md` |

## Earlier cloud run claims: the two-read pair, superseded as the lead claim, efficiency only

These rows stand as recorded, as labelled history. Under a two-read bound the
first call already repaired inside one call, so this pair shows one fewer read
on a later call, an efficiency result on n = 1 pair. It does not show an escape,
because nothing was stuck. The loop-escape claims above are the lead.

Source for every row: the private cloud-run record (not published), covering the
golden run and the telemetry and access logs for the exact window, together with
the sanitized bodies in `dist/public-demo/fixture.json`. The raw provider
responses stay private.

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| C1 | Call 1, empty decision history, terminal `completed` / `populated_after_repair`, `verification_status: passed`. | Supported | `fixture.json` call 1; the private cloud-run record |
| C2 | Call 1 the first decision chose `inspect_pipeline` at reported confidence 0.3067; it executed; its read admitted real evidence and moved no supported disposition. | Supported | `fixture.json` call 1 `first_decision`; the receipt's `outcome_class: disposition_unchanged`, in the window's telemetry verification (not published) |
| C3 | The verifier wrote a `disposition_unchanged` receipt, reason `insufficient_evidence`, `blocking_eligible: true`, `stored: true`. | Supported | the window's telemetry verification (not published) |
| C4 | Call 1's re-decision independently chose `inspect_schema_keys` at 0.4746; the repair applied; 2 diagnostic reads total; 1 repair. | Supported | `fixture.json` call 1 |
| C5 | Call 2 is a genuinely new signed call with a new request key, `history_status: matched`, consulting the two receipts call 1 wrote. | Supported | `fixture.json` call 2 `receipts_consulted`; the private cloud-run record |
| C6 | Call 2's first decision chose `inspect_pipeline` again at 0.2158, and controller validation blocked that exact choice before execution with `blocked_reason_code: insufficient_evidence`. | Supported | `fixture.json` call 2; the private cloud-run record |
| C7 | Call 2's re-decision chose `inspect_schema_keys` at 0.6498, the repair applied, and the call finished `completed` / `populated_after_repair` with the same verified rows, on 1 diagnostic read. | Supported | `fixture.json` call 2; the private cloud-run record |
| C8 | Verified rows on both calls: two rows, `IN` 4 units and `US` 5 units, from one declared synthetic fixture. | Supported | `fixture.json` calls 1 and 2, `aggregated_rows` |
| C9 | A repeat of call 2's request key answered `duplicate`, returned the stored result unchanged under the same identity, executed nothing and emitted no second telemetry timeline, in 1030.1 ms. | Supported | `fixture.json` call 3; the private cloud-run record |
| C10 | Every controller invocation routed to the hosted Decider. The telemetry label is `decider` from the first real answer (sequence 10) onward and `mock` before it, because the label is derived from a normalized selection that does not exist yet. | Supported | the private cloud-run record |
| C11 | Amazon Bedrock Nova Lite narrated both runs, in 972.7 ms and 791.5 ms, and no `result` object carries a narration field. | Supported | the private cloud-run record; `fixture.json` narration blocks, where the published latencies are rounded to 970 ms and 790 ms |
| C12 | 49 `UNSTUCK_TELEMETRY_V1` lines over the window; two run timelines of 25 and 22 contiguous events; both finalized with `dropped: 0` and `refused: 0`; one timeline per executed run and none for the duplicate. | Supported | the private cloud-run record; the window's telemetry verification (not published) |
| C13 | All eighteen forbidden raw values were absent from the telemetry, including account identifier, every resource identifier, subnet, security group, Region literal, credential pattern, local path, traceback, idempotency key, session identity and the narration prose. The model revision travels only as a digest placeholder, with the raw revision absent. | Supported | the private cloud-run record |
| C14 | The serving endpoint answered four `POST /invocations` with 200 and zero 500s over the window; `/ping` returned 200 throughout. | Supported | the private cloud-run record |
| C15 | Anonymous access was denied on every path before any signed call: three unsigned agent-runtime probes and one unsigned managed-hosting probe, each a 403 identifiably from the service's own request-signing gate rather than a bare 403. | Supported | the private cloud-run record; the recorded unsigned-access denials (not published) |
| C16 | Zero false blocks: four Decider answers across two runs, and the only block was on an exact-context repeat. | Supported | `docs/hackathon-validation.md`, section "Four judge-facing facts, with their denominators" |
| C17 | The read delta is one fewer read, on **n = 1 pair of calls**. It is an existence result and not a rate. | Supported, scope-bound | `docs/hackathon-validation.md`, section "Four judge-facing facts, with their denominators" |

## Local golden run claims, denominator five cases

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| L1 | Five of five cases passed seven criteria frozen before the run. | Supported | `docs/hackathon-validation.md`, section "The seven frozen criteria, and whether each held" |
| L2 | Denominators: 5 cases, 2 conditions (with receipts and without receipts), 10 complete pairs, 20 terminal episodes, 40 real selector calls, 15 stored receipts, largest selector payload 5,162 characters against an 8,000 bound. | Supported | `docs/hackathon-validation.md`, the golden-run "Denominators" section |
| L3 | Zero false blocks, zero regressions, zero malformed answers, zero provider failures, zero timeouts, zero bound violations. | Supported | `docs/hackathon-validation.md`, section "The seven frozen criteria, and whether each held" |
| L4 | The without-receipts comparison requested no history and repeated the read on all five cases. | Supported | `docs/hackathon-validation.md`, section "What changed in the system to make this reachable" |
| L5 | Reported confidence spanned 0.1946 to 0.6676 and every accepted selection was below the product's default 0.80 floor. | Supported | `docs/hackathon-validation.md`, section "Model behaviour on this cohort" |
| L6 | The checkpoint was chosen because it exhibits a repeatable blind spot, not because it performed well. | Supported | `docs/hackathon-validation.md`, golden-run "Limitations, and what is not claimed" |
| L7 | `python -m unstuck.cascade.scenarios` reproduces both the cold and the cross-call history scenario offline, with no network and no credentials. | Supported | Run in this session, `status: passed` |

## Failed and corrected experiments, which stay in the story

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| F1 | The first local experiment is UNVALIDATED: step 2 never occurred, so the qualifying subset was empty under criteria frozen before the run. | Supported | `docs/hackathon-validation.md`, section "Whether the hypothesis held on the first run" |
| F2 | The first run's measurement located a defect in the rule rather than a gap in the corpus: the outcome taxonomy used evidence novelty alone, so a read that returned real data and resolved nothing was unrefusable by construction. | Supported | `docs/hackathon-validation.md`, section "The corrected rule" |
| F3 | The second local experiment is FAIL on one frozen criterion: the refusal held on seven of seven same-outcome pairs, and the read saving held on three of seven. The criterion measured the whole call rather than the mechanism, and recognising that afterwards did not license changing it. | Supported | `docs/hackathon-validation.md`, section "Whether the hypothesis held on the second run" |
| F4 | The first cloud attempt ended with both invocations `unresolved` on `provider_error_decider` and zero diagnostic reads. | Supported | `docs/hackathon-validation.md`, section "What blocked the first attempt, and what cleared it" |
| F5 | The cause was measured: the real 3,321-character four-option payload took 23,588.8 ms and 23,384.3 ms against the product's own 20 s budget. Latency rises roughly linearly with state size on that instance. | Supported | `docs/hackathon-validation.md`, section "What blocked the first attempt, and what cleared it"; `final-golden-summary.json` `failed_first_cloud_attempt.measured_cause` |
| F6 | Three exits were refused at the time: raising a number to make a run pass, shrinking the state text, escalating the instance. The owner then made both budgets deployment inputs with a 60 s controller ceiling and a 120 s serving ceiling, and **no default moved**: both still ship at 20 s. | Supported | `docs/hackathon-validation.md`, section "What blocked the first attempt, and what cleared it" |
| F7 | The public demo sanitizer failed closed on its first attempt, which is the mechanism working. The revision is now projected through the product's own `public_identity` and reproduces the live telemetry placeholder exactly. | Supported | the private cloud-run record |
| F8 | Two of the telemetry verification checks were wrong on their first pass, including one that passed vacuously on no data. They were corrected in the verification script, not in the product. | Supported | the private cloud-run record |

## Public facts about Decider 2B, from public sources only

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| D1 | Strands Decider is a small decision model for agentic AI. It picks between option sets and rates things on a scale, and it cannot generate text. | Supported | `strandsagents.com/blog/introducing-strands-decider/`; `github.com/strands-labs/strands-decider` |
| D2 | Decider 2B has two billion parameters: a Qwen3.5-2B torso with the language-model head removed, replaced by a pointer head of just over a million parameters, tuned with a rank-16 low-rank adapter. | Supported | `strandsagents.com/blog/introducing-strands-decider/` |
| D3 | The three question primitives are `noul` (yes/no), `choice` (one of N options) and `score` (a level on an ordered scale). Each answer carries a confidence and, for `choice`, a probability per option. | Supported | the upstream Decider 2B model card on `huggingface.co/StrandsAgents` |
| D4 | The model's licence is Apache-2.0. | Supported | Both public pages above |
| D5 | The published guidance is to confirm or ask a person below a confidence of 0.9, and to measure calibration on your own traffic before relying on a threshold. | Supported | `github.com/strands-labs/strands-decider`; the upstream model card's limitations section |
| D6 | The model card names long multi-step documents as the weak spot and says question phrasing matters because questions are read less closely than documents. | Supported | the upstream Decider 2B model card on `huggingface.co/StrandsAgents` |
| D7 | The hackathon describes Decider 2B as an innovation and experimentation starter, not a production product. | Supported | Hackathon about tab |
| D8 | This project pinned one immutable checkpoint revision of Strands Decider 2B and used that same revision for both the local and the cloud work, throughout. The revision value is not published. | Supported | `docs/hackathon-validation.md`, section "Running the real model locally"; `dist/public-demo/final-golden-summary.json` |
| D9 | The pinned checkpoint is described upstream as an earlier model that is still published. The current reference 2B model is a different identifier. | Supported | `github.com/strands-labs/strands-decider` |

## Submission requirements, from the official page

| # | Requirement, quoted | Evidence |
| --- | --- | --- |
| S1 | "An agent that uses Strands Decider 2B for at least one meaningful decision, paired with Amazon Bedrock" | Hackathon about tab |
| S2 | "A written article on Builder Center describing the agent, what it does, and the decision you handed to Decider 2B" | Hackathon about tab |
| S3 | "A link to a public repository with the agent's source code and at least one reusable Decider 2B decision pattern" | Hackathon about tab |
| S4 | "A short, publicly available demo video, under three minutes" | Hackathon about tab |
| S5 | "One decision pattern (below) that your submission demonstrates" | Hackathon about tab |
| S6 | "An original agent that has not been published before" | Hackathon about tab |
| S7 | Tags: `#strands-decider-2b` plus exactly one pattern tag. "Add both before the deadline." The five permitted pattern tags are `#tool-selection`, `#guardrails`, `#policy`, `#model-routing` and `#context-memory`, one per submission. This submission uses `#tool-selection`, whose official description is "The agent decides which of several tools to call from the user's input." | Hackathon about and resources tabs |
| S8 | Official latency or cost bonus eligibility is for the organisers to judge. The submission reports only the scoped latency evidence in rows T1 to T7, with no self-assigned score. | Official criteria in `docs/submission-checklist.md` |
| S9 | Window: October 16 2026 12:00 AM PDT to October 23 2026 11:59 PM PDT. | Hackathon about tab |
| S10 | Eligibility beyond "Open to builders 18 or older with a Builder Center profile" could not be read: the Rules and Terms tabs render client side. One-entry rules, country and employee exclusions, intellectual-property terms and disqualification criteria are **unverified**. | Fetch attempt recorded in this session |

## Package-hygiene claims

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| P1 | The reusable recipe is standard library only. It imports nothing from `unstuck`, `evaluation`, `infra` or `publication`, and nothing from Pydantic, DuckDB, boto3, FastAPI or the AWS Cloud Development Kit. | Supported | `tests/public_deliverables/test_independence.py` |
| P2 | The recipe is imported from a copy of its own directory, with only that directory and the standard library on the import path, so a hidden dependency fails a test. | Supported | `tests/public_deliverables/test_independence.py` |
| P3 | Written-story, link, evidence and public-safety tests check the current submission. Record their result after wording changes. | Pending current validation | `tests/public_deliverables/` |
| P4 | `python example.py` inside the recipe directory runs the whole walk twice under two catalogs whose identifiers differ entirely and whose capabilities are identical, and exits 0. | Supported | Run in this session |
| P5 | `uv run unstuck doctor --mode offline`, `uv run unstuck skeleton --mode offline`, and `uv run python -m unstuck.cascade.scenarios` all succeed with no network and no credentials. | Supported | Run in this session |
| P6 | The licence is the unmodified Apache-2.0 body, byte-identical to a reference copy. | Supported | `docs/public-deliverables.md`, "Licence and notices"; `tests/public_deliverables/test_notices.py` |

## Pending, and stated as awaited rather than achieved

| # | Item | Waiting for |
| --- | --- | --- |
| G1 | Public repository | The owner's publication decision. No push and no publication has occurred. |
| G2 | Builder Center article published | The owner publishing `docs/article.md`. |
| G3 | Public demo video | A rebuild, and then the owner's review and publication. The eight-scene video as built, `dist/video/unstuck-demo.mp4`, passed all nine gates on the file it was built from, but it narrates the **superseded post-D1 sequence** over the eight pitch frames and is therefore not current. The same applies to `docs/architecture.svg`, `docs/demo.html` and the pitch deck under `submission/pitch/`. |
| G4 | Submission tags applied on the entry | Submission time. |
| G5 | Dependency licence verification | Reading each upstream licence text and recording what it says. An owner action with legal consequence. |
| G6 | Clean-install verification on a clean machine | A clean machine. |
| G7 | The nine provider-boundary gate statuses | A human recording each result with its evidence reference in `docs/build-status.md`. Evidence for the provider-boundary gate now exists and the status is deliberately still Pending. |
| G8 | Eligibility, one-entry rule, deadline and link recheck | Reading the client-rendered Rules and Terms at submission time. |

## Forbidden, and asserted nowhere

| # | Claim that will not be made |
| --- | --- |
| X1 | Any statement about Decider 2B's general accuracy. |
| X2 | Any claim that the reported confidences are calibrated, in either acceptance tier. |
| X3 | Statistical significance, or any rate derived from the cloud pair. |
| X4 | A general or guaranteed latency improvement, or a cost saving inferred from fewer requests. Only the scoped Run 2 comparison in rows T1 to T7 is supported. |
| X5 | A dollar spend or dollar saving figure. Observed spend was not measured. The always-on endpoint is billed by uptime. |
| X5a | A dollar saving. The Decider is served by a real-time endpoint billed per instance-hour of uptime rather than per request, so one fewer inference does not by itself lower the bill, and no money figure is derived from the inference count. |
| X5b | Any saving attributed to Run 1. Run 1 pays the learning cost once; it is not saved, has no saving, and is never presented as a saving. |
| X6 | Production readiness, availability, or throughput. |
| X7 | Novelty or prior-art superiority. |
| X8 | Compliance with any regime. |
| X9 | Self-scoring or predicting a judge outcome. Official weights appear only in `docs/submission-checklist.md` as external criteria. |
| X10 | That the cloud run reproduced the local measurements, or that the local figures transfer to the cloud. |
| X11 | Any figure summed across components measured under different conditions, including an end-to-end latency. |
| X12 | Anything about the held-out corpus. It was not read and not run. |
| X13 | That the model learns, in general or between calls. The verifier records a receipt, the pinned checkpoint is unchanged, and what differed on Run 2 is the option set the controller offered it. Generic learning, model improvement over time, and learning from mistakes are all outside what this project asserts. |
| X14 | That a literal infinite loop was observed or prevented. The decision loop is across calls, and each call is bounded at two Decider choices as a design bound. |
| X15 | That repeated decisions are Decider 2B's biggest problem, or any ranking of its problems. |
| X16 | That the earlier two-read pair shows an escape. It shows one fewer read, efficiency only, on n = 1 pair. |
| X17 | Any generalization of the loop-escape result beyond one synthetic case, one fixture and one pinned checkpoint, each proof run once. |
| X18 | That the observed Run 2 first chose the proven dead end and then re-decided. That sequence belongs only to the superseded post-D1 flow, rows H1 to H3, and is labelled superseded wherever it appears. |
| X19 | That the drawn artifacts or the video are current. They show the superseded post-D1 flow and are pending a rebuild. |

## Sanitization rules for every public file

Never present, in any public file:

- AWS account identifiers, any resource identifier, Region literals, endpoint,
  runtime, bucket, repository, image digest, network or security-group
  identifiers
- Credentials, credential patterns, profile names
- Run, session, request, incident, decision, evidence, receipt or verification
  identifiers
- Idempotency keys
- Model revisions, raw or digested. Neither the revision nor a digest of it is
  published
- Raw model replies, prompt bodies, selector payloads, state text
- Evaluator truth, expected answers or held-out material
- Machine-local paths, and any path under a local virtual environment
- Internal Amazon URLs, internal tool names, team names, personal names,
  aliases or assistant names
- Benchmark condition labels and internal process or gate identifiers
- Em dashes, and non-inclusive terms
