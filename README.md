# Unstuck

**Fewer wasted checks. Better next choices.**

Unstuck is an AWS diagnostic agent for synthetic order reports that return zero rows. It turns a verified dead end into a **Decision Receipt**, removes that exact repeat before selection, and **Strands Decider 2B chooses from the remaining options**. Amazon Bedrock explains the verified result.

The project delivers two controlled successes: **an AWS agent that escapes a repeated diagnostic dead end**, and **a LoRA fine-tuning study with 25% fewer wasted first reads than the base model** on its evaluated synthetic workload.

[Watch the demo](dist/video/unstuck-demo.mp4) · [Explore the recorded replay](docs/demo.html) · [Reuse the decision pattern](recipes/diagnostic-tool-selection/)

Built by **Barath Ganesh** · `#strands-decider-2b` `#tool-selection`

## The thirty second version

Our target is precise: give a diagnostic agent one read, record what that read actually achieved, and make the next call count. In the AWS case, an order report is empty because the two sides of a join spell their keys differently.

1. **Without receipts:** three independent calls choose `inspect_pipeline`. The partition arrived, but the report remains unresolved on 3 of 3 calls.
2. **Run 1 records the dead end:** the deterministic verifier stores a `disposition_unchanged` receipt: real data, no change to a decision the system may act on. Run 1 ends unresolved after one read.
3. **Run 2 completes:** before the first Decider request, the controller removes that exact execution. The same Decider chooses `inspect_schema_keys`; the evidence supports a repair, and verification confirms a populated report. **One choice. One read. Completed.**

Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The memoryless control stays stuck.

![Top: on a zero-row report with one read per call, the memoryless control repeats inspect_pipeline and stays 3 of 3 unresolved. Bottom: Run 1 ends unresolved and the verifier writes a disposition_unchanged Decision Receipt. Run 2 removes the exact dead end before the first Decider request; the same Decider chooses inspect_schema_keys, leading to verified repair and completed. The footer compares earlier Run 2 with current Run 2: 54.8 to 26.6 seconds, 51.4% lower observed client time; n = 1, one synthetic case, each proof once. No dollar savings measured.](docs/unstuck-flow.svg)

Run 1 records the dead end; Run 2 uses it before selection. The animation shows causal order, not measured elapsed time.

## What was measured

**Controlled AWS workflow success.** The criterion was completion within a one-read budget after a verified dead end, with Decider retaining the tool-selection decision.

| Flow | Decider inferences | Diagnostic reads | Outcome | Client time |
| --- | --- | --- | --- | --- |
| Memoryless control, each of 3 calls | 1 | 1 | Unresolved on all 3 calls | Not compared here |
| Run 1, empty receipt history | 1 | 1 | Unresolved; receipt stored | 21.6 s |
| Run 2, receipt checked before selection | 1 | 1 | **Completed** | **26.6 s** |

The superseded Run 2 checked the receipt after the first choice: two inferences and 54.8 s. Moving the check before selection reduced observed Run 2 client time by **28.2 s (51.4%)** and removed one inference.

**Measurement design:** n = 1, one synthetic case, each proof once, comparing earlier and current Run 2 on the same endpoint. Client time measures the complete runtime invocation. The comparison is between two Unstuck flows. The SageMaker endpoint is billed by uptime; no dollar savings were measured. [Recorded AWS evidence](dist/public-demo/loop-escape-proof.json).

## Controlled fine-tuning success

**We trained Decider to make more productive first choices.** A receipt removes a proven dead end, but a receipt cannot tell Decider 2B which of them is most productive when several tools remain. LoRA targets that ranking without adding a second selector.

**What we trained.** Offline and once, every diagnostic offered in a frozen synthetic corpus was executed, and the deterministic verifier labelled which read moved the supported disposition. Those labels trained LoRA adapters plus the Decider choice head. The checkpoint was then frozen and replayed through the same controller path. Training used **900 synthetic rows, two epochs and 52 optimizer steps**.

**Where it acts.** Exact receipts filter the offer first. The base or adapted Decider 2B ranks what remains. The controller executes the chosen read, and the verifier decides the outcome and writes the next receipt. Nothing learns at runtime. Decider 2B stays the only model that selects a tool; its trained weights have no authority over receipts, repairs or truth.

![Receipts constrain. LoRA ranks what remains. Why: a new call can repeat a choice already proven useless, and a Decision Receipt precludes only that exact repeat; several valid tools then remain, and a receipt cannot rank them. Offline training, once, before any run: a frozen synthetic corpus, every offered read executed, the verifier labels which read moved the disposition, LoRA adapters and the choice head are trained, and the checkpoint is frozen and replayed through the same controller path. Only the frozen checkpoint crosses into runtime; nothing flows back. Runtime decision, every call: the Decision Receipt filters the offer, the remaining tools keep their original order, Decider 2B with the base or adapted checkpoint ranks them, the controller executes the chosen read, and the verifier decides the outcome and writes the next receipt. The animation plays once: the productive read moves from second to first while the receipt, offer and verifier stay fixed; the order is schematic. Offline result, synthetic, one seed: correct first choices 60.6% to 80.6%, wasted first reads 31.3% to 23.4%, verified completions 68.8% to 76.6%; head-only training was rejected because wasted reads did not fall.](docs/unstuck-ml-addon-flow.svg)

Receipts constrain before ranking; the verifier decides after it.

| Measure | Base Decider 2B | LoRA plus head | Change |
| --- | --- | --- | --- |
| Correct first choices | 97 of 160 decisions (60.6%) | **129 of 160 decisions (80.6%)** | **32 more correct choices** |
| Wasted first reads | 20 of 64 full calls (31.3%) | **15 of 64 full calls (23.4%)** | **25% fewer wasted first reads** |
| Verified completions | 44 of 64 calls (68.8%) | **49 of 64 calls (76.6%)** | **5 additional completed workflows** |

The study's target was at least **20% fewer wasted first reads**, with no paired completion regressions. LoRA plus the choice head achieved **25%**, with five improved calls and zero paired completion regressions. Head-only training reached 107 of 160 correct first choices (66.9%) while wasted reads stayed at 20 of 64; the workflow-level criterion distinguished the more useful training result.

**Study design:** offline, synthetic, in-distribution controlled variation; one seed, 160 first-choice decisions and 64 integrated calls replaying recorded answers through the controller. All 20 model-visible test situations also occur in training; 80 rows use a newly renamed tool catalog. The comparison above is base versus fine-tuned Decider. Reference rules on the same rows score 128 of 160 for a constant schema-key preference and 136 of 160 for a two-feature rule.

A separate same-device CPU replay supports the direction: correct first choices reached **132 of 160 (82.5%), up from 96 (60.0%)**; wasted first reads fell from 20 to 14 of 64; completions rose from 44 to 50 of 64. [CPU replay](experiments/decider_finetune/device_parity/FINDINGS.md) · [Training record and evaluation design](docs/research/decider-adaptation-findings.md).

**AWS deployment:** the hosted agent uses the base Decider 2B checkpoint. **Training evaluation:** the LoRA-adapted checkpoint is evaluated offline. These are two distinct demonstrations in the same project.

## How it works

| Component | Responsibility |
| --- | --- |
| **Strands Decider 2B** | Selects the next diagnostic with a typed `choice` response. |
| **Controller on Amazon Bedrock AgentCore Runtime** | Matches receipts, removes exact repeats, validates the choice and executes bounded diagnostics. |
| **Private Amazon SageMaker endpoint** | Serves the base Decider checkpoint for the AWS workflow. |
| **Deterministic verifier** | Checks observed evidence, records the outcome and establishes whether a repair is supported. |
| **Amazon Bedrock Nova Lite** | Generates the user-facing explanation after the verified result is fixed. |

The initial offer contains **three diagnostics plus `request_review`**. After `inspect_pipeline` is removed, two diagnostics plus `request_review` remain in their original order. The receipt constrains the offer; Decider makes the fresh choice.

A receipt binds capability, arguments and decision context: task, query, admissible evidence, candidate membership and pinned versions. `no_progress` and `disposition_unchanged` receipts can exclude an exact repeat. Changed facts or versions break the match. [Matching contracts](recipes/diagnostic-tool-selection/README.md#5-the-receipt-schema) · [AWS infrastructure](infra/).

## Built for diagnostic workflows

The focus is **bounded diagnostic tool selection**: an agent has several checks available and needs each one to advance the investigation. Intended applications include data troubleshooting, developer debugging and service diagnostics.

A report agent might keep checking an already-complete pipeline; a developer assistant might repeat an environment check; a service agent might recheck a healthy dependency. The reusable pattern gives builders a way to turn a verified unproductive execution into a precise constraint on the next choice. Bring read-only diagnostics, a verifier that can judge progress, and context that tracks changing facts.

## Run locally

Use Python 3.12 and [uv](https://docs.astral.sh/uv/):

```sh
uv venv --python 3.12
uv pip install -e .
uv run unstuck doctor --mode offline
uv run python -m unstuck.cascade.scenarios
```

These offline scenarios use a deterministic selector labelled `mock` and exercise the two-read flow. Install the `ui` extra and run `uv run unstuck serve` for the local web view. The recorded AWS proof uses the real checkpoint and a one-read configuration.

The controller supports at most two choices and two diagnostic reads per call. In the current hosted composition, receipts are shared within one runtime process using an in-memory store. The [AgentCore infrastructure definition](infra/agentcore.py) describes the hosted setup.

## Reuse the decision pattern

The [standalone recipe](recipes/diagnostic-tool-selection/) includes the typed [question](recipes/diagnostic-tool-selection/question.json), [state schema](recipes/diagnostic-tool-selection/state.schema.json), [selector](recipes/diagnostic-tool-selection/selector.py), and adoption guide:

```sh
python recipes/diagnostic-tool-selection/example.py
```

Its standard-library example demonstrates matching and bounded selection under two differently named catalogs. Adapt the catalog, model-call adapter and verifier-backed executor to your workflow.

<details>
<summary>Experiment records and development history</summary>

### The offline fine-tune evidence

The renamed-catalog slice moved from 64 of 80 (80.0%) to 70 of 80 (87.5%), recorded as 64/80 to 70/80. Wasted reads moved from 20 of 64 to 15 of 64 calls, recorded as 20/64 to 15/64: five improved calls, four in one family, exact paired p = 0.0625. CPU and CUDA differed on 3 of 160 near-tie decisions. The full [research record](docs/research/decider-adaptation-findings.md) preserves the comparison methods, follow-up studies and hosted-canary outcome.

### Development history

Earlier experiment verdicts remain available: the first local run was UNVALIDATED, the second was FAIL, and the five-case local golden run passed seven frozen criteria. **Superseded as the lead claim:** the post-choice pair, which completed on two inferences in 54.8 s, and the two-read pair, which shows efficiency only because nothing was stuck. [Validation record](docs/hackathon-validation.md) · [Claim register](docs/claim-register.md).

</details>

## Testing

```sh
uv pip install -e '.[dev,ui,cloud]'
uv run pytest -q
uv run python -m publishing.public_scan
```

The configured suite covers runtime and public deliverables. The research tests live separately in `tests/decider_finetune/` and `tests/decider_finetune_v2/`.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for dependency terms and provenance. Strands Decider 2B is a separate upstream model under its own licence and terms.
