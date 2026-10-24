"""Answer the frozen held-out requests with one checkpoint. Isolated environment only.

Usage, with the checkpoint environment's Python, from the repository root:

    MODEL_PY -B experiments/decider_finetune/infer.py run \
        --requests CORPUS/test.requests.jsonl --checkpoint DIR --out ANSWERS.json \
        --device mps [--expect-digest HEX]
    MODEL_PY -B experiments/decider_finetune/infer.py render-check \
        --corpus CORPUS --upstream UPSTREAM_REPO

`run` parses each line with the shipped serving contract and answers it with the
shipped `ChoiceEngine`, which calls
`engine.ask(state_text, {"d1_tool_choice": ChoiceQuestion(...)})`: the same code
a deployed endpoint runs. It imports no `unstuck`, AWS or network module,
executes no tool, makes one model load, never retries, and rewrites the
answers file atomically after every call. The file is marked complete only
when every request was answered.

`render-check` proves the official training `Example.to_question()` renders
the identical prompt to the serving path for every corpus row.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from deployment.sagemaker.server.contract import (  # noqa: E402
    QUESTION_KEY,
    ServeRequest,
)
from deployment.sagemaker.server.engine import (  # noqa: E402
    ChoiceEngine,
    ServingSettings,
)
from experiments.decider_finetune.identity import (  # noqa: E402
    checkpoint_digest,
    file_sha256,
    prompt_digest,
)

ANSWERS_SCHEMA = "decider-answers-v1"


class Refused(Exception):
    pass


def _write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    os.replace(temporary, path)


def run(
    requests: Path, checkpoint: Path, out: Path, device: str, expect: str | None
) -> int:
    if (
        os.environ.get("HF_HUB_OFFLINE") != "1"
        or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
    ):
        raise Refused("HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 are required")
    if out.exists():
        raise Refused(f"{out} exists: one answers file per checkpoint and corpus")
    lines = requests.read_text().splitlines()
    parsed = [ServeRequest.model_validate_json(line) for line in lines]
    revisions = {r.model_revision for r in parsed}
    if len(revisions) != 1:
        raise Refused("requests carry mixed model revisions")
    digest, files = checkpoint_digest(checkpoint)
    if expect is not None and digest != expect:
        raise Refused("checkpoint identity differs from the expected digest")
    engine = ChoiceEngine(
        ServingSettings(
            model_dir=checkpoint, model_revision=revisions.pop(), device=device
        )
    )
    started = time.perf_counter()
    engine.load()
    payload: dict[str, Any] = {
        "schema": ANSWERS_SCHEMA,
        "question_key": QUESTION_KEY,
        "checkpoint": {"digest": digest, "files": len(files)},
        "model_revision": parsed[0].model_revision,
        "requests_sha256": file_sha256(requests),
        "device": device,
        "engine_load_seconds": time.perf_counter() - started,
        "complete": False,
        "records": [],
    }
    _write(out, payload)
    for line, request in zip(lines, parsed, strict=True):
        options = [[o.option_id, o.criterion] for o in request.options]
        record: dict[str, Any] = {
            "prompt_sha256": prompt_digest(
                request.question, request.state_text, options
            ),
            "wire_sha256": hashlib.sha256(line.encode()).hexdigest(),
        }
        try:
            answer = engine.answer(request)
            record |= {
                "status": "ok",
                "choice": answer.choice,
                "probabilities": answer.probabilities,
                "confidence": answer.confidence,
                "latency_ms": answer.latency_ms,
            }
        except Exception as error:  # noqa: BLE001 - recorded, never retried
            record |= {"status": "error", "failure": type(error).__name__}
        payload["records"].append(record)
        _write(out, payload)
    payload["complete"] = all(r["status"] == "ok" for r in payload["records"])
    _write(out, payload)
    print(
        json.dumps(
            {"records": len(payload["records"]), "complete": payload["complete"]}
        )
    )
    return 0 if payload["complete"] else 1


def render_check(corpus: Path, upstream: Path) -> int:
    sys.path.insert(0, str(upstream))
    from strands_decider.prompting import build_prompt
    from strands_decider.schema import ChoiceQuestion
    from training.data.format import read_jsonl  # type: ignore[import-not-found]

    checked = 0
    for partition in ("train", "calibration", "test"):
        examples = list(read_jsonl(str(corpus / f"{partition}.jsonl")))
        wires = (corpus / f"{partition}.requests.jsonl").read_text().splitlines()
        for example, line in zip(examples, wires, strict=True):
            request = ServeRequest.model_validate_json(line)
            served = ChoiceQuestion(
                instructions=request.question, criteria=dict(request.criteria)
            )
            trained, _ = build_prompt(example.state, example.to_question())
            serving, _ = build_prompt(request.state_text, served)
            if trained != serving:
                raise Refused(f"{partition}: training and serving prompts differ")
            checked += 1
    print(f"RENDER_OK {checked}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infer")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--requests", type=Path, required=True)
    r.add_argument("--checkpoint", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--device", required=True)
    r.add_argument("--expect-digest")
    c = sub.add_parser("render-check")
    c.add_argument("--corpus", type=Path, required=True)
    c.add_argument("--upstream", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(
            args.requests, args.checkpoint, args.out, args.device, args.expect_digest
        )
    return render_check(args.corpus, args.upstream)


if __name__ == "__main__":
    sys.exit(main())
