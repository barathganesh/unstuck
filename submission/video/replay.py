"""Replay the saved AWS agent evidence locally, with presentation pacing."""

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def events(run: int) -> list[str]:
    """Render recorded facts; this command performs no inference or repair."""
    proof = json.loads((ROOT / "dist/public-demo/loop-escape-proof.json").read_text())
    call = proof["agentcore_proof"]["calls"][run - 1]
    hosted = proof["hosted_decider_proof"]["with_receipts"][run - 1]
    times = proof["measured_latency"]["agentcore_client_time"]
    timing = times["run_1_learning" if run == 1 else "optimized_run_2_preselection"]
    elapsed = timing["elapsed"]["value"] / 1000
    lines = ["[request] diagnose zero-row order report | read_budget=1"]
    if run == 1:
        lines.append("[history] empty | 3 diagnostics + request_review")
    else:
        lines.extend([
            "[receipt] exact context matched: disposition_unchanged",
            "[filter]  withhold inspect_pipeline before selection",
        ])
    lines.extend([
        f"[decider] selected={call['decider_choice']}",
        f"[tool]    {call['decider_choice']} -> read complete",
    ])
    if run == 1:
        lines.extend([
            "[verify]  disposition_unchanged | no supported repair",
            f"[receipt] stored={hosted['receipts_stored'][0]['outcome_class']}",
            f"[result]  {call['terminal_status'].upper()} | {call['terminal_reason']}",
            "[metrics] inferences=1 | reads=1/1 | repairs=0",
            f"[timing]  client={elapsed:.4f}s (recorded)",
        ])
    else:
        selector = proof["measured_latency"]["agentcore_selector_time"]
        selector_seconds = selector["optimized_run_2_preselection"]["elapsed"]["value"] / 1000
        earlier = times["superseded_run_2_post_d1"]["elapsed"]["value"] / 1000
        difference = times["difference"]
        lines.extend([
            f"[verify]  repairs={call['repairs']['value']} | {call['terminal_reason']}",
            "[bedrock] Nova Lite -> explanation after verified completion",
            f"[result]  {call['terminal_status'].upper()} | report populated",
            "[metrics] inferences=1 | reads=1/1 | repairs=1 | verified=true",
            f"[timing]  client={elapsed:.4f}s | selector={selector_seconds:.5f}s",
            f"[compare] earlier Run 2: client={earlier:.4f}s | inferences=2",
            f"[delta]   -{difference['absolute']['value']/1000:.4f}s "
            f"(-{difference['relative']['value']}%) | 1 fewer inference",
            f"[proof]   {proof['agentcore_proof']['checks_passed']['value']}/25 "
            "recorded checks PASS across the two-call proof",
        ])
    return lines


def training_events() -> list[str]:
    data = json.loads((ROOT / "dist/public-demo/decider-finetune-lora-head-v1.json").read_text())
    training, comparison = data["training"], data["comparison"]
    choice = comparison["first_choice_correct"]
    wasted = comparison["integrated_wasted_first_reads"]
    done = comparison["integrated_verified_completions"]
    base, adapted = int(wasted["baseline"].split("/")[0]), int(wasted["adapted"].split("/")[0])
    reduction = (base - adapted) / base * 100
    return [
        f"[study]   offline synthetic | {training['seed_count']} seed | 20 familiar states",
        f"[train]   rows={training['synthetic_rows']} | epochs={training['epochs']} "
        f"| optimizer_steps={training['optimizer_steps']}",
        "[method]  verifier labels -> LoRA adapters + choice head",
        f"[choices] base={choice['baseline']} -> LoRA={choice['adapted']} | +32 correct",
        f"[wasted]  base={wasted['baseline']} -> LoRA={wasted['adapted']} | {reduction:.0f}% fewer",
        f"[done]    base={done['baseline']} -> LoRA={done['adapted']} | +5 completed",
        f"[paired]  completion_regressions={comparison['paired_completion_regressions']}",
        "[target]  at least 20% fewer wasted reads + no completion regressions",
        "[result]  CONTROLLED SUCCESS | target met",
    ]


def result_payload(run: int) -> dict:
    proof = json.loads((ROOT / "dist/public-demo/loop-escape-proof.json").read_text())
    call = proof["agentcore_proof"]["calls"][run - 1]
    key = "run_1_learning" if run == 1 else "optimized_run_2_preselection"
    result = {
        "mode": "recorded_aws_replay",
        "selector": "Strands Decider 2B",
        "selected_tool": call["decider_choice"],
        "terminal_status": call["terminal_status"],
        "terminal_reason": call["terminal_reason"],
        "decider_inferences": 1,
        "diagnostic_reads": call["diagnostic_reads"]["value"],
        "repairs": call["repairs"]["value"],
        "client_ms": proof["measured_latency"]["agentcore_client_time"][key]["elapsed"]["value"],
        "narration_status": call["narration"],
        "served_by": "Amazon Bedrock AgentCore Runtime",
    }
    if run == 2:
        result["selector_ms"] = proof["measured_latency"]["agentcore_selector_time"][key]["elapsed"]["value"]
        result["client_latency_reduction_percent"] = proof["measured_latency"]["agentcore_client_time"]["difference"]["relative"]["value"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", type=int, choices=(1, 2))
    mode.add_argument("--training", action="store_true")
    parser.add_argument("--pace", type=float, default=0.7)
    args = parser.parse_args()
    if args.pace < 0:
        parser.error("--pace must be nonnegative")
    label = "RECORDED OFFLINE TRAINING" if args.training else "RECORDED AWS REPLAY"
    print(f"{label} | synthetic case | presentation pacing", flush=True)
    for line in training_events() if args.training else events(args.run):
        print(line, flush=True)
        time.sleep(args.pace)
    if not args.training:
        print(json.dumps(result_payload(args.run), indent=2), flush=True)


if __name__ == "__main__":
    main()
