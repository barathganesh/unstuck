"""Score a checkpoint on exactly the frozen held-out set, and compare two of them.

Usage, from the repository root:

    python -m experiments.decider_finetune.evaluate freeze \
        --corpus CORPUS --checkpoint PINNED_DIR --out FREEZE.json
    python -m experiments.decider_finetune.evaluate report \
        --corpus CORPUS --freeze FREEZE.json --answers BASELINE.json --out REPORT.json
    python -m experiments.decider_finetune.evaluate compare \
        --corpus CORPUS --freeze FREEZE.json --baseline BASELINE.json \
        --adapted ADAPTED.json --adapted-digest HEX --calibration-record CAL.json \
        --out COMPARE.json

`freeze` binds the corpus manifest, the config templates, this harness and the
pinned checkpoint identity before any held-out answer exists. `report` scores
one answers file two ways:

* row level, over every frozen test request: first-choice correctness against
  the verifier-derived label, what the chosen read would have done (useful,
  wasted, or no read), NLL, and ECE with the library's own confidence formula;
* integrated, through the real controller: each test case runs Run 1 then
  Run 2 in the loop-escape configuration over one isolated receipt namespace,
  with every decision answered from the answers file. Every request the
  replay reaches must be a frozen test row, so the replay cannot wander off
  the frozen set.

`compare` pairs two reports and applies the frozen criteria. It reports no
latency or cost figure: offline answers support neither claim, and the
deployed endpoint is billed by uptime rather than by request.

Everything refuses rather than scores when the inputs are not exactly what was
frozen: leakage, manifest drift, a wrong checkpoint identity, an incomplete or
malformed answers file, or a prompt, revision or catalog other than the frozen
one.
"""

import argparse
import json
import math
import stat
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from experiments.decider_finetune.corpus import (
    Corpus,
    check_calibration,
    check_files,
    check_ids,
    check_labels,
    check_leakage,
    check_order,
    check_prompt_shape,
    prompt_version,
)
from experiments.decider_finetune.identity import checkpoint_digest, file_sha256
from experiments.decider_finetune.oracle import (
    REVIEW,
    Answer,
    Config,
    Prompt,
    false_repair,
    run_call,
    verified,
)
from experiments.decider_finetune.scenarios import CATALOGS
from unstuck.cascade.projector import SelectorInput
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.selection import Provider

FREEZE_SCHEMA = "decider-finetune-freeze-v1"
ANSWERS_SCHEMA = "decider-answers-v1"
CALIBRATION_SCHEMA = "decider-calibration-record-v1"
HERE = Path(__file__).resolve().parent
HARNESS = (
    "evaluate.py",
    "infer.py",
    "corpus.py",
    "oracle.py",
    "scenarios.py",
    "identity.py",
)
CONFIGS = ("configs/head-only.yaml", "configs/lora-head-fallback.yaml")
SUM_TOLERANCE = 1e-3
ECE_BINS = 10
#: A8: adapted wasted first reads must be at most this share of the baseline's.
WASTED_RATIO_MAX = 0.8


class Refused(Exception):
    """A typed refusal. The harness scores nothing it cannot vouch for."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def load_corpus(root: Path) -> Corpus:
    """Load the frozen corpus, refusing drift and leakage before anything is scored."""
    drift = check_files(root)
    if drift:
        raise Refused("manifest_drift", "; ".join(drift[:3]))
    corpus = Corpus.load(root)
    checks = {
        "prompt_version": check_prompt_shape(corpus),
        "leakage": check_leakage(corpus)
        + check_order(corpus)
        + check_ids(corpus)
        + check_labels(corpus, replay=False, workers=1)
        + check_calibration(corpus),
    }
    for reason, failures in checks.items():
        if failures:
            raise Refused(reason, "; ".join(failures[:3]))
    if corpus.manifest["prompt_version"] != prompt_version():
        raise Refused("prompt_version", "the corpus was built under another prompt")
    return corpus


def _readonly(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def freeze(corpus_root: Path, checkpoint: Path, out: Path) -> dict[str, Any]:
    """Bind everything a held-out score depends on, before any held-out answer."""
    if out.exists():
        raise Refused("frozen", f"{out} exists")
    corpus = load_corpus(corpus_root)
    digest, files = checkpoint_digest(checkpoint)
    record = {
        "schema": FREEZE_SCHEMA,
        "corpus_manifest_sha256": file_sha256(corpus_root / "manifest.json"),
        "test_requests_sha256": corpus.manifest["files"]["test"]["requests"],
        "calibration_examples_sha256": corpus.manifest["files"]["calibration"][
            "examples"
        ],
        "test_examples_sha256": corpus.manifest["files"]["test"]["examples"],
        "model_revision": corpus.manifest["config"]["model_revision"],
        "configs_sha256": {name: file_sha256(HERE / name) for name in CONFIGS},
        "harness_sha256": {name: file_sha256(HERE / name) for name in HARNESS},
        "pinned_checkpoint": {"digest": digest, "files": len(files)},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    _readonly(out)
    return record


def load_freeze(path: Path, corpus_root: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(path.read_text())
    if record.get("schema") != FREEZE_SCHEMA:
        raise Refused("manifest_drift", "not a freeze record")
    if record["corpus_manifest_sha256"] != file_sha256(corpus_root / "manifest.json"):
        raise Refused("manifest_drift", "the corpus is not the one frozen")
    for name, digest in record["configs_sha256"].items():
        if file_sha256(HERE / name) != digest:
            raise Refused("manifest_drift", f"{name} changed after the freeze")
    for name, digest in record["harness_sha256"].items():
        if file_sha256(HERE / name) != digest:
            raise Refused("manifest_drift", f"{name} changed after the freeze")
    return record


@dataclass(frozen=True)
class Record:
    choice: str
    probabilities: dict[str, float]
    confidence: float


def load_answers(
    path: Path, corpus: Corpus, *, expected_digest: str
) -> tuple[dict[str, Record], dict[str, Any]]:
    """Exactly one valid answer per frozen test prompt, from the expected checkpoint."""
    payload: dict[str, Any] = json.loads(path.read_text())
    if payload.get("schema") != ANSWERS_SCHEMA:
        raise Refused("incomplete", "not an answers file")
    if payload["checkpoint"]["digest"] != expected_digest:
        raise Refused("checkpoint_identity", "answers come from another checkpoint")
    if payload["requests_sha256"] != corpus.manifest["files"]["test"]["requests"]:
        raise Refused("prompt_version", "answers were made for other requests")
    if payload["model_revision"] != corpus.manifest["config"]["model_revision"]:
        raise Refused("prompt_version", "answers carry another model revision")
    if payload.get("complete") is not True:
        raise Refused("incomplete", "the answers file is not complete")
    truth = {t["prompt_sha256"]: t for t in corpus.truth["test"]}
    records: dict[str, Record] = {}
    for item in payload["records"]:
        key = item["prompt_sha256"]
        if key in records:
            raise Refused("incomplete", "duplicate answer")
        if key not in truth:
            raise Refused(
                "prompt_version", "an answer for a prompt outside the frozen set"
            )
        if item.get("status") != "ok":
            raise Refused("incomplete", "an errored answer")
        options = [o[0] for o in truth[key]["options"]]
        probabilities = {str(k): float(v) for k, v in item["probabilities"].items()}
        values = list(probabilities.values())
        if (
            item["choice"] not in options
            or set(probabilities) != set(options)
            or not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in values)
            or abs(sum(values) - 1.0) > SUM_TOLERANCE
            or not 0.0 <= float(item["confidence"]) <= 1.0
        ):
            raise Refused("incomplete", "a malformed answer")
        records[key] = Record(item["choice"], probabilities, float(item["confidence"]))
    if set(records) != set(truth):
        raise Refused(
            "incomplete", f"{len(set(truth) - set(records))} prompts unanswered"
        )
    return records, payload


def derive_confidence(probabilities: Sequence[float]) -> float:
    """The library's normalized max probability, so ECE measures what is served."""
    n = len(probabilities)
    if n <= 1:
        return 1.0
    return max(0.0, min(1.0, (n * max(probabilities) - 1.0) / (n - 1.0)))


def calibration(items: Sequence[tuple[list[float], int]]) -> dict[str, float]:
    """ECE over 10 confidence bins and NLL, exactly as `training/evaluate.py` bins."""
    if not items:
        return {"n": 0}
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    nll = 0.0
    for probabilities, label in items:
        confidence = derive_confidence(probabilities)
        argmax = max(range(len(probabilities)), key=lambda i: probabilities[i])
        bins[min(ECE_BINS - 1, int(confidence * ECE_BINS))].append(
            (confidence, argmax == label)
        )
        nll -= math.log(max(1e-12, probabilities[label]))
    ece = sum(
        len(group)
        / len(items)
        * abs(
            sum(c for _, c in group) / len(group)
            - sum(k for k, _ in group) / len(group)
        )
        for group in bins.values()
    )
    return {"n": len(items), "ece": ece, "nll": nll / len(items)}


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {"count": numerator, "of": denominator}


def score_rows(corpus: Corpus, answers: Mapping[str, Record]) -> dict[str, Any]:
    """Row-level scores over every frozen test request."""
    rows = []
    for truth in corpus.truth["test"]:
        record = answers[truth["prompt_sha256"]]
        options = [o[0] for o in truth["options"]]
        outcome = next(
            (o for o in truth["outcomes"] if o["public_id"] == record.choice), None
        )
        rows.append(
            {
                "prompt_sha256": truth["prompt_sha256"],
                "family": truth["family"],
                "kind": truth["kind"],
                "catalog": truth["catalog"],
                "choice": record.choice,
                "correct": record.choice == truth["label_id"],
                "read": "none"
                if outcome is None
                else "useful"
                if outcome["productive"]
                else "wasted",
                "false_repair": bool(outcome and outcome["false_repair"]),
                "probabilities": [record.probabilities[o] for o in options],
                "label": options.index(truth["label_id"]),
            }
        )

    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "rows": len(items),
            "first_choice_correct": _rate(sum(r["correct"] for r in items), len(items)),
            "wasted_first_reads": _rate(
                sum(r["read"] == "wasted" for r in items), len(items)
            ),
            "no_read_choices": _rate(
                sum(r["read"] == "none" for r in items), len(items)
            ),
            "false_repairs": sum(r["false_repair"] for r in items),
            "calibration": calibration(
                [(r["probabilities"], r["label"]) for r in items]
            ),
        }

    def by(key: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        return {name: block(items) for name, items in sorted(groups.items())}

    return {
        "overall": block(rows),
        "by_family": by("family"),
        "by_kind": by("kind"),
        "by_catalog": by("catalog"),
        "per_row": [
            {k: r[k] for k in ("prompt_sha256", "family", "choice", "correct", "read")}
            for r in rows
        ],
    }


def score_integrated(
    corpus: Corpus, answers: Mapping[str, Record], provider: Provider
) -> dict[str, Any]:
    """Run 1 then Run 2 per test case through the real controller."""
    config = Config(
        model_revision=corpus.manifest["config"]["model_revision"],
        acceptance_tier=corpus.manifest["config"]["acceptance_tier"],
    )
    truth = {t["prompt_sha256"]: t for t in corpus.truth["test"]}
    fresh = {
        (t["family"], t["variant"], t["catalog"]): t
        for t in corpus.truth["test"]
        if t["kind"] == "fresh_d1" and t["budget"] == 1
    }
    missing: list[str] = []

    def answerer(request: SelectorInput, prompt: Prompt) -> Answer:
        record = answers.get(prompt.digest)
        if record is None or prompt.digest not in truth:
            missing.append(prompt.digest)
            return Answer(REVIEW)
        return Answer(
            record.choice, tuple(record.probabilities.items()), record.confidence
        )

    calls: list[dict[str, Any]] = []
    for scenario in corpus.integrated():
        productive_capability = fresh[
            (scenario.family, scenario.variant, scenario.catalog)
        ]["label_capability"]
        store = LocalReceiptRepository()
        for call_index in (1, 2):
            trace = run_call(
                scenario,
                call_index=call_index,
                budget=corpus.manifest["config"]["receipt_budget"],
                store=store,
                answerer=answerer,
                config=config,
                provider=provider,
            )
            if missing:
                raise Refused(
                    "incomplete",
                    "the replay reached a request outside the frozen answers",
                )
            first = trace.captured[0] if trace.captured else None
            row = truth[first.prompt.digest] if first else None
            choice = answers[first.prompt.digest].choice if first else None
            catalog = CATALOGS[scenario.catalog]
            calls.append(
                {
                    "family": scenario.family,
                    "variant": scenario.variant,
                    "catalog": scenario.catalog,
                    "call": call_index,
                    "choice": choice,
                    "first_choice_correct": bool(row and choice == row["label_id"]),
                    "first_read_outcome": trace.first_read_outcome,
                    "wasted_first_read": trace.first_read_outcome
                    not in (None, "useful_evidence"),
                    "verified_completion": verified(trace, scenario),
                    "false_repair": false_repair(trace, scenario.fixture()),
                    "false_preclusion": any(
                        catalog.capability(p) == productive_capability
                        for p in trace.precluded
                    ),
                    "precluded": list(trace.precluded),
                    "reason_code": trace.reason_code,
                }
            )

    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "calls": len(items),
            "first_choice_correct": _rate(
                sum(c["first_choice_correct"] for c in items), len(items)
            ),
            "wasted_first_reads": _rate(
                sum(c["wasted_first_read"] for c in items), len(items)
            ),
            "verified_completions": _rate(
                sum(c["verified_completion"] for c in items), len(items)
            ),
            "false_repairs": sum(c["false_repair"] for c in items),
            "false_preclusions": sum(c["false_preclusion"] for c in items),
        }

    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in calls:
        families[c["family"]].append(c)
    return {
        "overall": block(calls),
        "run_1": block([c for c in calls if c["call"] == 1]),
        "run_2": block([c for c in calls if c["call"] == 2]),
        "by_family": {name: block(items) for name, items in sorted(families.items())},
        "per_call": calls,
    }


def report(
    corpus_root: Path,
    freeze_path: Path,
    answers_path: Path,
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    corpus = load_corpus(corpus_root)
    frozen = load_freeze(freeze_path, corpus_root)
    digest = expected_digest or frozen["pinned_checkpoint"]["digest"]
    answers, payload = load_answers(answers_path, corpus, expected_digest=digest)
    return {
        "checkpoint_digest": digest,
        "pinned": digest == frozen["pinned_checkpoint"]["digest"],
        "corpus_manifest_sha256": frozen["corpus_manifest_sha256"],
        "answers_sha256": file_sha256(answers_path),
        "device": payload.get("device"),
        "rows": score_rows(corpus, answers),
        "integrated": score_integrated(corpus, answers, "decider"),
        "claims": {"latency": "none", "cost": "none", "mode": "offline"},
    }


def _calibration_record(
    path: Path, frozen: Mapping[str, Any], adapted: str
) -> list[str]:
    record = json.loads(path.read_text())
    failures = []
    if record.get("schema") != CALIBRATION_SCHEMA:
        failures.append("not a calibration record")
    if record.get("data_sha256") != frozen["calibration_examples_sha256"]:
        failures.append("calibrated on data other than the frozen calibration file")
    if record.get("data_sha256") == frozen["test_examples_sha256"]:
        failures.append("calibrated on the test partition")
    if record.get("split") != "all":
        failures.append("calibration did not use --split all")
    if record.get("checkpoint_digest_after") != adapted:
        failures.append("the calibrated checkpoint is not the evaluated one")
    return failures


def compare(
    corpus_root: Path,
    freeze_path: Path,
    baseline_path: Path,
    adapted_path: Path,
    adapted_digest: str,
    calibration_path: Path,
) -> dict[str, Any]:
    frozen = load_freeze(freeze_path, corpus_root)
    if adapted_digest == frozen["pinned_checkpoint"]["digest"]:
        raise Refused("checkpoint_identity", "the adapted checkpoint is the pinned one")
    calibration_failures = _calibration_record(calibration_path, frozen, adapted_digest)
    if calibration_failures:
        raise Refused("calibration", "; ".join(calibration_failures))
    base = report(corpus_root, freeze_path, baseline_path)
    tuned = report(
        corpus_root, freeze_path, adapted_path, expected_digest=adapted_digest
    )
    rows_b = {r["prompt_sha256"]: r for r in base["rows"]["per_row"]}
    rows_a = {r["prompt_sha256"]: r for r in tuned["rows"]["per_row"]}
    corrections = sum(rows_a[k]["correct"] and not rows_b[k]["correct"] for k in rows_b)
    regressions = sum(rows_b[k]["correct"] and not rows_a[k]["correct"] for k in rows_b)
    calls_b = base["integrated"]["per_call"]
    calls_a = tuned["integrated"]["per_call"]
    pairs = list(zip(calls_b, calls_a, strict=True))
    if any(
        (b["family"], b["variant"], b["catalog"], b["call"])
        != (a["family"], a["variant"], a["catalog"], a["call"])
        for b, a in pairs
    ):
        raise Refused("prompt_version", "integrated cases are not paired")
    wasted_b = sum(b["wasted_first_read"] for b, _ in pairs)
    wasted_a = sum(a["wasted_first_read"] for _, a in pairs)
    done_b = sum(b["verified_completion"] for b, _ in pairs)
    done_a = sum(a["verified_completion"] for _, a in pairs)
    completion_regressions = sum(
        b["verified_completion"] and not a["verified_completion"] for b, a in pairs
    )
    criteria = {
        "G13_wasted_first_reads_reduced_20pct": wasted_b > 0
        and wasted_a <= WASTED_RATIO_MAX * wasted_b,
        "G14_no_completion_regression": completion_regressions == 0
        and done_a >= done_b,
        "G15_zero_false_repairs": tuned["integrated"]["overall"]["false_repairs"] == 0
        and tuned["rows"]["overall"]["false_repairs"] == 0,
        "G15_zero_false_preclusions": tuned["integrated"]["overall"][
            "false_preclusions"
        ]
        == 0,
    }
    unmet = [name for name, held in criteria.items() if not held]
    return {
        "baseline": {k: base[k] for k in ("checkpoint_digest", "answers_sha256")},
        "adapted": {k: tuned[k] for k in ("checkpoint_digest", "answers_sha256")},
        "paired_rows": {
            "rows": len(rows_b),
            "corrections": corrections,
            "regressions": regressions,
            "first_choice_correct": [
                base["rows"]["overall"]["first_choice_correct"],
                tuned["rows"]["overall"]["first_choice_correct"],
            ],
            "ece_nll": [
                base["rows"]["overall"]["calibration"],
                tuned["rows"]["overall"]["calibration"],
            ],
        },
        "paired_integrated": {
            "calls": len(pairs),
            "wasted_first_reads": [wasted_b, wasted_a],
            "verified_completions": [done_b, done_a],
            "completion_regressions": completion_regressions,
        },
        "per_family": {
            name: [
                base["integrated"]["by_family"][name],
                tuned["integrated"]["by_family"][name],
            ]
            for name in base["integrated"]["by_family"]
        },
        "criteria": criteria,
        "verdict": "PROMOTION_ELIGIBLE" if not unmet else "PROMOTION_REFUSED",
        "unmet": unmet,
        "claims": {"latency": "none", "cost": "none", "mode": "offline"},
    }


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise Refused("frozen", f"{path} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    _readonly(path)


def _headline(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rows": {k: v for k, v in result["rows"]["overall"].items()},
        "run_1": result["integrated"]["run_1"],
        "run_2": result["integrated"]["run_2"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluate")
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--corpus", type=Path, required=True)
    f.add_argument("--checkpoint", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("report")
    r.add_argument("--corpus", type=Path, required=True)
    r.add_argument("--freeze", type=Path, required=True)
    r.add_argument("--answers", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare")
    c.add_argument("--corpus", type=Path, required=True)
    c.add_argument("--freeze", type=Path, required=True)
    c.add_argument("--baseline", type=Path, required=True)
    c.add_argument("--adapted", type=Path, required=True)
    c.add_argument("--adapted-digest", required=True)
    c.add_argument("--calibration-record", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            record = freeze(args.corpus, args.checkpoint, args.out)
            print(json.dumps(record["pinned_checkpoint"]))
            print("FROZEN")
            return 0
        if args.command == "report":
            result = report(args.corpus, args.freeze, args.answers)
            _write(args.out, result)
            print(json.dumps(_headline(result), indent=1, sort_keys=True))
            print("BASELINE_COMPLETE" if result["pinned"] else "REPORT_COMPLETE")
            return 0
        result = compare(
            args.corpus,
            args.freeze,
            args.baseline,
            args.adapted,
            args.adapted_digest,
            args.calibration_record,
        )
        _write(args.out, result)
        print(json.dumps({k: result[k] for k in ("criteria", "unmet")}, indent=1))
        print(result["verdict"])
        return 0 if result["verdict"] == "PROMOTION_ELIGIBLE" else 2
    except Refused as refusal:
        print(f"REFUSED {refusal}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
