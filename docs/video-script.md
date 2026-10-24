# Demo video script

Nine scenes tell two controlled-success stories: the working AWS receipt workflow and the verifier-trained LoRA checkpoint. The styled introduction, AWS flow and LoRA training come first. Highlighted code and terminal replay follow; final metric pages lead into the named closing. The closing screen credits **Barath Ganesh**.

The finished cut is 128.800 seconds, strictly below 180 seconds. Every spoken line uses the original Amazon Polly synthetic narration, edited at sentence boundaries. The agent itself uses Amazon Bedrock Nova Lite to explain verified outcomes. The voiceover and the agent's generative step are separate.

Run 1 pays the learning cost once. Run 2 uses the receipt before selection. The memoryless control stays stuck. Run 1: one inference, 21.6 s, unresolved. Run 2: one inference, 26.6 s, completed. Superseded Run 2: two inferences, 54.8 s. The observed difference is 28.2 s, or 51.4% lower Run 2 client time. Measurement design: n = 1, the same synthetic case, each proof once. The endpoint is billed by uptime; no dollar savings were measured.

The LoRA study compares base and adapted Decider on offline synthetic, in-distribution controlled variation: 160 first-choice decisions and 64 integrated calls, one seed. All 20 model-visible test situations occur in training. The AWS workflow uses the base checkpoint; the training result uses the frozen adapted checkpoint offline. Detailed methods and research records are linked from the README.

Exact source ranges and frame counts: [`submission/video/edit-plan.json`](../submission/video/edit-plan.json). Tags: `#strands-decider-2b` `#tool-selection`.

## Scene 1. The report is empty

**0.000 to 9.500 seconds.** No extra hold: 0 s held.

*Screen:* Styled Unstuck introduction, followed by the three recorded diagnostic calls that establish the problem.

> A report returned zero rows. Without receipts, Decider picked inspect pipeline on three calls. Each spent its one read and ended unresolved.

## Scene 2. The AWS execution path

**9.500 to 17.167 seconds.** No extra hold: 0 s held.

*Screen:* Styled AWS architecture followed by phased agent flow: match, choose, verify, explain.

> Decider serves from a private, always-on Amazon SageMaker endpoint. Amazon Bedrock Nova Lite narrates only after completion.

## Scene 3. Training Decider with verified outcomes

**17.167 to 30.100 seconds.** No extra hold: 0 s held.

*Screen:* Styled LoRA training workflow: verifier labels, LoRA adapters plus choice head, then a frozen checkpoint replay.

> Offline, we ran every diagnostic and trained LoRA plus Decider's choice head on verifier labels. At runtime, the receipt removes one repeat; LoRA ranks what remains; the verifier owns truth.

## Scene 4. A reusable selection pattern

**30.100 to 42.600 seconds.** No extra hold: 0 s held.

*Screen:* Highlighted source excerpts with original line numbers: receipt matching and preclusion, then payload construction, selector invocation and acceptance checks.

> The reusable recipe sends current state and matching history to Decider. Only a proven exact repeat is withheld before selection. A receipt cannot pick a tool. Decider chooses from the remaining tools.

## Scene 5. Run 1 records the dead end

**42.600 to 59.267 seconds.** No extra hold: 0 s held.

*Screen:* Brief workflow phases, then an animated shell command. Logs show selection, tool execution, verification, receipt storage and measured timing; structured JSON ends with a returned prompt.

> Run 1 happens once: one inference, 21.6 seconds of client time, one read, unresolved. The deterministic verifier stored a Decision Receipt: real data, no decision moved. The model is unchanged. The system recorded a fact.

## Scene 6. Run 2 chooses a new path

**59.267 to 78.367 seconds.** No extra hold: 0 s held.

*Screen:* Brief workflow phases, then the shell replay with the exact receipt, fresh tool selection, verified repair, Bedrock narration and recorded metrics. Structured JSON ends with a returned prompt.

> Run 2 is a new call, and the exact receipt matches. Before the first Decider request and before any read, the controller precludes only the proven dead end. Decider makes one fresh choice, inspect schema keys. One inference, one read, a verified repair, completed in 26.6 seconds.

## Scene 7. Controlled AWS success

**78.367 to 97.400 seconds.** No extra hold: 0 s held.

*Screen:* Return to the styled metric page: 51.4% lower observed Run 2 client time, 54.8 to 26.6 seconds, and two to one inference. Earlier versus current Unstuck flow, one synthetic case, each proof once.

> Old Run 2 took 54.8 seconds and two inferences. Optimized Run 2 takes 26.6 seconds and one inference. That is one fewer inference and an observed difference of 28.2 seconds, or 51.4 percent lower client latency. One synthetic case, each proof once.

## Scene 8. Controlled fine-tuning success

**97.400 to 115.433 seconds.** No extra hold: 0 s held.

*Screen:* Styled final training metrics: 97 to 129 correct choices; 20 to 15 wasted reads, a 25% reduction; 44 to 49 completions. The comparison is the base model versus the adapted checkpoint in the controlled offline study.

> Across 160 decisions, correct first choices rose from 97 to 129. Across 64 calls, wasted first reads fell from 20 to 15 and completions rose from 44 to 49; CPU replay confirmed direction, one seed, experimental checkpoint.

## Scene 9. Built by Barath Ganesh

**115.433 to 128.800 seconds.** No extra hold: 0 s held.

*Screen:* Styled closing: better choices, fewer wasted checks. Built by Barath Ganesh. Intended diagnostic applications and the project tags.

> One failed decision becomes a boundary. History can rule out one proven execution under one identical context. It cannot choose the next tool. Decider chooses. The verifier decides what the read found.

## Delivery

The completed MP4 and optional captions are in `dist/video/`. It uses the original Amazon Polly synthetic narration. Recorded evidence is replayed locally; the AWS deployment uses base Decider 2B and the LoRA checkpoint is evaluated offline.
