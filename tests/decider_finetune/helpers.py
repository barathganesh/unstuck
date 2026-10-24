"""Helpers for the fine-tune experiment tests: a tiny plan, tampering, fake answers."""

import json
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deployment.sagemaker.server.contract import ServeRequest
from experiments.decider_finetune.corpus import Plan
from experiments.decider_finetune.identity import (
    checkpoint_digest,
    file_sha256,
    prompt_digest,
)

REVISION = "scripted-v1"
#: The smallest plan that still meets every corpus gate: three families and two
#: productive capabilities per partition, three renamed train catalogs, and
#: every capability at every position in train.
TINY = Plan(
    assignment={
        "train": ("arrival_pending_plain", "key_drift_dimension", "empty_plain"),
        "calibration": (
            "arrival_pending_no_orders",
            "empty_prefixed",
            "empty_drift_off_date",
        ),
        "test": (
            "arrival_pending_prefixed",
            "key_drift_fact",
            "empty_filtered_country",
            "empty_drift_fact",
        ),
    },
    variants={"train": 1, "calibration": 1, "test": 1},
    catalogs={
        "train": ("production", "r1", "r2", "r3"),
        "calibration": ("r5",),
        "test": ("production",),
    },
)


def writable_copy(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    for path in target.iterdir():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def reseal(root: Path) -> None:
    """Re-bind every file into the manifest, as a careful tamperer would."""
    manifest = json.loads((root / "manifest.json").read_text())
    for partition, names in manifest["files"].items():
        for name in names:
            filename = {
                "examples": f"{partition}.jsonl",
                "requests": f"{partition}.requests.jsonl",
                "truth": f"{partition}.truth.jsonl",
            }[name]
            names[name] = file_sha256(root / filename)
    manifest["integrated_sha256"] = file_sha256(root / "integrated.json")
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    (root / "manifest.sha256").write_text(file_sha256(root / "manifest.json") + "\n")
    for path in root.iterdir():
        path.chmod(stat.S_IRUSR)


Policy = Callable[[dict[str, Any], list[str]], str]


def label_policy(truth: dict[str, Any], options: list[str]) -> str:
    return str(truth["label_id"])


def pipeline_policy(truth: dict[str, Any], options: list[str]) -> str:
    """Always the arrival read, the constant choice earlier screens observed."""
    from experiments.decider_finetune.scenarios import CATALOGS

    catalog = CATALOGS[truth["catalog"]]
    wanted = catalog.public_id("inspect_pipeline")
    return wanted if wanted in options else options[0]


def dead_end_policy(truth: dict[str, Any], options: list[str]) -> str:
    """Always an offered read that is not the verifier label: the worst case."""
    from experiments.decider_finetune.oracle import REVIEW

    wrong = [o for o in options if o not in (truth["label_id"], REVIEW)]
    return wrong[0] if wrong else str(truth["label_id"])


def fake_checkpoint(path: Path, content: str) -> str:
    path.mkdir(parents=True)
    (path / "head.safetensors").write_text(content)
    return checkpoint_digest(path)[0]


def answers(
    corpus: Path,
    out: Path,
    digest: str,
    policy: Policy,
    top: float = 0.9,
    **overrides: Any,
) -> Path:
    """An answers file shaped exactly as `infer.py run` writes one.

    `top` is the probability on the chosen option; the rest is shared evenly.
    """
    truth = {
        json.loads(line)["prompt_sha256"]: json.loads(line)
        for line in (corpus / "test.truth.jsonl").read_text().splitlines()
    }
    records = []
    requests = corpus / "test.requests.jsonl"
    for line in requests.read_text().splitlines():
        request = ServeRequest.model_validate_json(line)
        options = [[o.option_id, o.criterion] for o in request.options]
        key = prompt_digest(request.question, request.state_text, options)
        ids = [o[0] for o in options]
        choice = policy(truth[key], ids)
        rest = (1.0 - top) / (len(ids) - 1)
        records.append(
            {
                "prompt_sha256": key,
                "status": "ok",
                "choice": choice,
                "probabilities": {i: top if i == choice else rest for i in ids},
                "confidence": 0.867,
                "latency_ms": 1.0,
            }
        )
    payload = {
        "schema": "decider-answers-v1",
        "checkpoint": {"digest": digest, "files": 1},
        "model_revision": REVISION,
        "requests_sha256": file_sha256(requests),
        "device": "cpu",
        "complete": True,
        "records": records,
    } | overrides
    out.write_text(json.dumps(payload))
    return out
