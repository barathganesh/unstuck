# Unstuck

Run 1 happens once: Unstuck turns a tool choice that Decider 2B has proven useless into a Decision Receipt, so Run 2 removes that dead end before selection and the same model makes a fresh choice.

Run 1: 1 inference, 21.6 s, unresolved, receipt stored. Run 2: 1 inference, 26.6 s, completed. The superseded Run 2 took 2 inferences and 54.8 s, so observed Run 2 client time is 28.2 s, 51.4% lower. n = 1, one synthetic case, each proof run once.

Unstuck has two core mechanisms. Decision Receipts stop a new call from repeating a tool choice already proven useless. A LoRA-adapted Decider 2B then ranks the tools that remain, so the productive read comes first more often. That adapted checkpoint is experimental and is not the currently deployed runtime selector.

Tags: `#strands-decider-2b` `#tool-selection`

![Top: the memoryless control circles the same decision track three times, repeats inspect_pipeline and stays 3 of 3 unresolved. Bottom: Run 1 spends one inference and one read, remains unresolved in 21.6 seconds, and the verifier stamps a disposition_unchanged Decision Receipt. The paper receipt travels into Run 2, which exact-matches it and cuts only inspect_pipeline before the first Decider request; the same Decider chooses inspect_schema_keys from three remaining diagnostics. The loop opens into a forward path: one read and verified repair lead to completed in 26.6 seconds on one inference. Observed Run 2 client time is 28.2 seconds and 51.4% lower than old Run 2 at 54.8 seconds and two inferences; n = 1, same synthetic case, each proof once. No dollar savings measured.](docs/unstuck-flow.svg)

Run 1 records the dead end; Run 2 uses it before selection.

## The thirty second version

A synthetic order report returns zero rows. Decider 2B picks `inspect_pipeline`. The read returns real data (the partition arrived) and settles nothing, because this incident turns on how the two sides of a join spell their keys. Each call may spend one diagnostic read, so three memoryless calls make the same choice and end unresolved, 3 of 3.

**Decider 2B makes tool decisions, but a new call can repeat a decision already proven useless. Unstuck turns that verified dead end into a Decision Receipt, forcing the same Decider to choose a new path and escape the decision loop, without a larger model taking over.**

Run 1 spends its one read on `inspect_pipeline` and ends unresolved. The deterministic verifier stores a `disposition_unchanged` Decision Receipt: real data, no change to any decision the system may act on. Run 2 is a new call on the same problem. The controller exact-matches that receipt and precludes only that execution before the first Decider request. Decider 2B, the only model that selects a tool, picks `inspect_schema_keys` from the three remaining diagnostics in their original order. One read, verified repair, `completed`.

Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The memoryless control stays stuck.

Run 1 is not saved: it is the learning call. No dollar saving is claimed, because the always-on SageMaker endpoint is billed by uptime, not per request. Record: [`dist/public-demo/loop-escape-proof.json`](dist/public-demo/loop-escape-proof.json).

## Why receipts need learned ranking

A receipt removes one proven dead end. After that, several valid tools usually remain, and a receipt cannot tell Decider 2B which of them is most productive. A weak ranking spends the call's one read on a path that moves nothing.

LoRA was added to improve that ranking without adding a second selector. Decider 2B stays the only model that selects a tool, and the adapted weights hold no authority over receipts, repairs or truth.

**What we trained.** Offline and once, every diagnostic offered in a frozen synthetic corpus was executed, and the deterministic verifier labelled which read moved the supported disposition. Those labels trained LoRA adapters plus the Decider choice head. The checkpoint was then frozen and replayed through the same controller path.

**Where it acts.** Exact receipts filter the offer first. The base or adapted Decider 2B ranks what remains. The controller executes the chosen read, and the verifier decides the outcome and writes the next receipt. Nothing learns at runtime.

![Receipts constrain. LoRA ranks what remains. Why: a new call can repeat a choice already proven useless, and a Decision Receipt precludes only that exact repeat; several valid tools then remain, and a receipt cannot rank them. Offline training, once, before any run: a frozen synthetic corpus, every offered read executed, the verifier labels which read moved the disposition, LoRA adapters and the choice head are trained, and the checkpoint is frozen and replayed through the same controller path. Only the frozen checkpoint crosses into runtime; nothing flows back. Runtime decision, every call: the Decision Receipt filters the offer, the remaining tools keep their original order, Decider 2B with the base or adapted checkpoint ranks them, the controller executes the chosen read, and the verifier decides the outcome and writes the next receipt. The animation plays once: the productive read moves from second to first while the receipt, offer and verifier stay fixed; the order is schematic. Offline result, synthetic, one seed: correct first choices 60.6% to 80.6%, wasted first reads 31.3% to 23.4%, verified completions 68.8% to 76.6%; head-only training was rejected because wasted reads did not fall.](docs/unstuck-ml-addon-flow.svg)

Receipts constrain before ranking; the verifier decides after it.

Offline replay on synthetic data, one seed: 160 first-choice decisions, then 64 full calls that replay those choices through the controller. Fewer wasted reads is better.

| Measure | Base Decider 2B | LoRA plus head | Change |
| --- | --- | --- | --- |
| Correct first choices | 97 of 160 decisions (60.6%) | **129 of 160 decisions (80.6%)** | 32 more decisions |
| Wasted first reads | 20 of 64 full calls (31.3%) | **15 of 64 full calls (23.4%)** | 5 fewer calls, 25% fewer |
| Verified completions | 44 of 64 calls (68.8%) | **49 of 64 calls (76.6%)** | 5 more calls |

Head-only training reached 107 correct first choices out of 160 (66.9%), but wasted reads stayed at 20 of 64 calls, so it was rejected. A separate same-device CPU replay of both checkpoints moved correct first choices to 132 of 160 (82.5%), up from 96 (60.0%); wasted first reads to 14 of 64, down from 20; and completions to 50 of 64, up from 44. CPU and CUDA differed on 3 of 160 near-tie decisions ([`FINDINGS.md`](experiments/decider_finetune/device_parity/FINDINGS.md)). Scope, caveats and both stopped follow-up studies: [`docs/research/decider-adaptation-findings.md`](docs/research/decider-adaptation-findings.md) and [the audit record below](#the-offline-fine-tune-evidence).

## A Decision Receipt is not memory, and it is not a cache

A cache answers the question for you. Memory nudges you. A receipt records what already happened and makes the model decide again.

A receipt never selects a tool, changes a probability, scores a candidate, authorizes a repair or decides what is true. It precludes only on an exact match of capability, arguments and decision context: the task, the query, the admissible evidence, candidate membership and every pinned version. Only two outcome classes can preclude: `no_progress`, where the read admitted nothing, and `disposition_unchanged`, where it returned real data and moved no supported disposition. There is no expiry. New evidence, a changed candidate set, tool contract, model revision or policy breaks the exact match, and the receipt stops precluding.

## How it works

- **Decider 2B** answers one typed `choice` question. It is the only model that selects a tool. The base checkpoint serves at runtime; the LoRA-adapted checkpoint is experimental.
- **The controller** matches receipts, precludes before the first Decider request, and runs read-only diagnostics.
- **The deterministic verifier** decides what happened and writes every receipt. A repair runs only on verifier-held evidence.
- **Amazon Bedrock Nova Lite** narrates after the result is fixed and holds no authority.

Each call is bounded at two Decider choices and at most two diagnostic reads; both proofs set the read bound to one. A preclusion costs no read and no inference. A caller cannot select the scenario, provider, model, tools or mode, and no secret is minted, stored or read on any path.

On AWS the controller runs on Amazon Bedrock AgentCore Runtime and the pinned checkpoint on a private, network-isolated Amazon SageMaker endpoint. Every hop is request-signed and scoped to one action. Nothing in this repository creates, deploys or spends; the shape is readable in `infra/` and `deployment/`.

## Local quickstart

You need Python and [uv](https://docs.astral.sh/uv/). No model download, cloud account, credentials or network call.

```sh
uv venv --python 3.12
uv pip install -e .
uv run unstuck doctor --mode offline
uv run python -m unstuck.cascade.scenarios
```

The last command prints the shipped scenarios with a deterministic selector labelled `mock`. Its choices are synthetic and it measures nothing. These are the earlier two-read scenarios, not the one-read proof. Install the `ui` extra and run `uv run unstuck serve` for a loopback-only web view. The real checkpoint needs the `strands-decider` package and a local snapshot.

The pattern on its own, standard library only, is in [`recipes/diagnostic-tool-selection/`](recipes/diagnostic-tool-selection/).

## Limits

- One synthetic case, one fixture, one pinned checkpoint, each proof run once: n = 1, no rate. The checkpoint was chosen because it repeats this dead end.
- The latency comparison is one observation per flow. No general latency benefit and no cost effect follow from it.
- The LoRA ranking result is offline, synthetic, one seed and in-distribution. No claim is made about unseen situations or safety. A constant rule that prefers the schema-key read where offered gets 128 of 160 first choices right (80.0%) on the same rows, one fewer than the adapted checkpoint.
- Not claimed: general model quality, calibrated confidences, statistical significance, production readiness or novelty.

Full record, failed runs and both hosted proofs included: [`docs/hackathon-validation.md`](docs/hackathon-validation.md). Every claim traces to [`docs/claim-register.md`](docs/claim-register.md). ML detail: [`docs/research/decider-adaptation-findings.md`](docs/research/decider-adaptation-findings.md).

### The offline fine-tune evidence

This is an offline synthetic test of controlled variation, in-distribution: one seed, four test scenario families, and 64 integrated calls that replay 64 of the 160 row answers through the controller. The families are disjoint from training by name and fixture, but all 20 model-visible test situations occur in training. The only new surface is 80 rows under a renamed tool catalog that never appears in training or calibration, where correct first choices moved from 64 of 80 (80.0%) to 70 of 80 (87.5%), recorded as 64/80 to 70/80.

Wasted first reads moved from 20 of 64 to 15 of 64 calls, recorded as 20/64 to 15/64: five improved calls and none worse, four of the five in one family, exact paired p = 0.0625. False repairs and false preclusions were 0, but those errors were structurally unreachable here, so the zeros are not safety evidence. ECE uses the project's normalized confidence and is descriptive; no calibration gate was applied. A private hosted canary was run later and missed its pre-registered exact-choice parity gate, so no hosted result is claimed. No latency or cost claim is made. Both v2 follow-up studies stopped before any training, at [`STOP_INSUFFICIENT_HOLDOUT`](experiments/decider_finetune_v2/viability-report.json) and [`STOP_RECEIPT_STATES_COLLAPSE`](experiments/decider_finetune_v2/NEXT-EXPERIMENT.md).

## How the story got here

Earlier runs stay in the record with their verdicts. The first local experiment is UNVALIDATED: the outcome taxonomy judged a read only by whether its evidence was new, which is why `disposition_unchanged` exists. The second is FAIL, on a criterion frozen before it ran. The five-case local golden run passed seven frozen criteria.

**Superseded as the lead claim:** the post-choice pair, which completed on two inferences in 54.8 s, and the two-read pair, which shows efficiency only because nothing was stuck ([`final-golden-summary.json`](dist/public-demo/final-golden-summary.json)).

## Testing

```sh
uv run pytest -q                            # the full suite
uv run python -m publishing.public_scan     # the fail-closed public leakage scan
```

`tests/public_deliverables/test_public_story.py` fails if this opening drifts from the approved story or the curated record leaks an identifier.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE) for pinned dependencies, whose licence verification is recorded as unverified and pending. Strands Decider 2B is a separate upstream model under its own licence and terms.
