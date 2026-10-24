"""The built corpus and the evaluation harness, on one tiny frozen corpus.

The corpus is built once per session through the real controller and verifier
(`conftest.corpus_root`). Answers are synthetic stand-ins shaped exactly as
`infer.py run` writes them, so no figure here is a model measurement.
"""

import json
import stat
from pathlib import Path

import pytest

from experiments.decider_finetune.corpus import (
    Corpus,
    Plan,
    build,
    check_leakage,
    verify,
)
from experiments.decider_finetune.evaluate import (
    Refused,
    compare,
    freeze,
    load_corpus,
    report,
)
from experiments.decider_finetune.identity import file_sha256
from experiments.decider_finetune.oracle import Config
from experiments.decider_finetune.scenarios import FAMILIES
from tests.decider_finetune.helpers import (
    REVISION,
    answers,
    dead_end_policy,
    fake_checkpoint,
    label_policy,
    pipeline_policy,
    reseal,
    writable_copy,
)


def test_tiny_corpus_meets_every_corpus_gate(corpus_root):
    results = verify(corpus_root, replay=True, workers=8)
    assert {marker: failures for marker, failures in results.items() if failures} == {}
    assert set(results) == {
        "CORPUS_VERIFIED",
        "PROMPT_SHAPE_OK",
        "LEAKAGE_NONE",
        "ORDER_PERMUTED",
        "IDS_NOT_PREDICTIVE",
        "LABELS_REDERIVED",
        "CALIBRATION_ISOLATED",
    }


def test_corpus_rows_are_official_examples_with_receipt_aware_kinds(corpus_root):
    manifest = json.loads((corpus_root / "manifest.json").read_text())
    for partition in ("train", "calibration", "test"):
        kinds = manifest["partitions"][partition]["kinds"]
        assert kinds["fresh_d1"] and kinds["receipt_d1"]
        assert len(manifest["partitions"][partition]["label_capabilities"]) == 2
        line = (corpus_root / f"{partition}.jsonl").read_text().splitlines()[0]
        assert list(json.loads(line)) == [
            "kind",
            "state",
            "instructions",
            "options",
            "label",
            "task",
            "weight",
            "instruction_variants",
        ]
    excluded = {e["family"]: e["reason"] for e in manifest["excluded_families"]}
    assert excluded["pending_key_drift"].startswith("public twin")
    assert all(not p.stat().st_mode & stat.S_IWUSR for p in corpus_root.iterdir())


def test_requests_carry_no_truth(corpus_root):
    text = (corpus_root / "test.requests.jsonl").read_text()
    for token in (*FAMILIES, "fresh_d1", "receipt_d1", "productive", "label"):
        assert token not in text


def test_build_is_deterministic_and_refuses_an_existing_directory(tmp_path):
    plan = Plan(
        assignment={
            "train": ("arrival_pending_plain",),
            "calibration": ("empty_prefixed",),
            "test": ("key_drift_fact",),
        },
        variants={"train": 1, "calibration": 1, "test": 1},
        catalogs={"train": ("r1",), "calibration": ("r5",), "test": ("production",)},
    )
    build(tmp_path / "a", Config(REVISION), plan=plan, workers=3)
    build(tmp_path / "b", Config(REVISION), plan=plan, workers=1)
    for name in ("manifest.json", "test.requests.jsonl", "train.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()
    with pytest.raises(FileExistsError):
        build(tmp_path / "a", Config(REVISION), plan=plan, workers=1)


@pytest.fixture
def frozen(corpus_root, tmp_path):
    pinned = fake_checkpoint(tmp_path / "pinned", "pinned")
    record = freeze(corpus_root, tmp_path / "pinned", tmp_path / "freeze.json")
    assert record["pinned_checkpoint"]["digest"] == pinned
    return tmp_path / "freeze.json", pinned


def test_report_scores_rows_and_integrated_runs(corpus_root, frozen, tmp_path):
    freeze_path, pinned = frozen
    good = report(
        corpus_root,
        freeze_path,
        answers(corpus_root, tmp_path / "good.json", pinned, label_policy),
    )
    rows, calls = good["rows"]["overall"], good["integrated"]
    assert rows["first_choice_correct"]["count"] == rows["rows"]
    assert rows["wasted_first_reads"]["count"] == 0 and rows["false_repairs"] == 0
    assert (
        calls["overall"]["verified_completions"]["count"] == calls["overall"]["calls"]
    )
    assert calls["overall"]["false_preclusions"] == 0
    assert good["pinned"] and good["claims"]["latency"] == "none"
    assert set(rows["calibration"]) == {"n", "ece", "nll"}
    constant = report(
        corpus_root,
        freeze_path,
        answers(corpus_root, tmp_path / "pipe.json", pinned, pipeline_policy),
    )
    run_1, run_2 = constant["integrated"]["run_1"], constant["integrated"]["run_2"]
    # A constant arrival read wastes Run 1 on every non-arrival case; Run 2 escapes
    # because the exact receipt precludes the dead end before D1.
    assert run_1["wasted_first_reads"]["count"] == 3
    assert run_2["wasted_first_reads"]["count"] == 0
    assert run_2["verified_completions"]["count"] == run_2["calls"]
    assert set(constant["integrated"]["by_family"]) == {
        "arrival_pending_prefixed",
        "key_drift_fact",
        "empty_filtered_country",
        "empty_drift_fact",
    }


def test_compare_pairs_and_applies_the_frozen_criteria(corpus_root, frozen, tmp_path):
    freeze_path, pinned = frozen
    adapted = fake_checkpoint(tmp_path / "adapted", "adapted")
    base = answers(corpus_root, tmp_path / "base.json", pinned, pipeline_policy)
    tuned = answers(corpus_root, tmp_path / "tuned.json", adapted, label_policy)
    record = tmp_path / "cal.json"
    record.write_text(
        json.dumps(
            {
                "schema": "decider-calibration-record-v1",
                "data_sha256": file_sha256(corpus_root / "calibration.jsonl"),
                "split": "all",
                "checkpoint_digest_after": adapted,
            }
        )
    )
    result = compare(corpus_root, freeze_path, base, tuned, adapted, record)
    assert result["verdict"] == "PROMOTION_ELIGIBLE"
    assert result["paired_integrated"]["wasted_first_reads"] == [3, 0]
    assert result["paired_rows"]["regressions"] == 0
    flipped = tmp_path / "flipped.json"
    answers(corpus_root, flipped, adapted, pipeline_policy)
    worse = compare(corpus_root, freeze_path, base, flipped, adapted, record)
    assert worse["verdict"] == "PROMOTION_REFUSED"
    assert "G13_wasted_first_reads_reduced_20pct" in worse["unmet"]


def _calibration_record(corpus_root: Path, path: Path, digest: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "decider-calibration-record-v1",
                "data_sha256": file_sha256(corpus_root / "calibration.jsonl"),
                "split": "all",
                "checkpoint_digest_after": digest,
            }
        )
    )
    return path


def test_compare_applies_no_calibration_criterion(corpus_root, frozen, tmp_path):
    """ECE is descriptive: a far worse ECE still leaves the verdict eligible."""
    freeze_path, pinned = frozen
    adapted = fake_checkpoint(tmp_path / "adapted", "adapted")
    base = answers(corpus_root, tmp_path / "base.json", pinned, pipeline_policy)
    flat = answers(corpus_root, tmp_path / "flat.json", adapted, label_policy, top=0.5)
    record = _calibration_record(corpus_root, tmp_path / "cal.json", adapted)
    result = compare(corpus_root, freeze_path, base, flat, adapted, record)
    ece_base, ece_flat = (c["ece"] for c in result["paired_rows"]["ece_nll"])
    assert ece_flat > ece_base + 0.05
    assert result["verdict"] == "PROMOTION_ELIGIBLE" and result["unmet"] == []
    assert not [
        name for name in result["criteria"] if "ece" in name.lower() or "calib" in name
    ]


def test_false_repairs_and_preclusions_are_unreachable_in_these_families(
    corpus_root, frozen, tmp_path
):
    """Worst-case answers still score zero, so zero is not safety evidence."""
    freeze_path, pinned = frozen
    worst = report(
        corpus_root,
        freeze_path,
        answers(corpus_root, tmp_path / "worst.json", pinned, dead_end_policy),
    )
    rows, calls = worst["rows"]["overall"], worst["integrated"]["overall"]
    assert rows["first_choice_correct"]["count"] == 0
    assert calls["wasted_first_reads"]["count"] > 0
    assert rows["false_repairs"] == 0
    assert calls["false_repairs"] == 0 and calls["false_preclusions"] == 0


def test_harness_refuses_manifest_drift(corpus_root, tmp_path):
    root = writable_copy(corpus_root, tmp_path / "c")
    path = root / "test.truth.jsonl"
    path.write_text(path.read_text().replace("key_drift_fact", "key_drift_fakt", 1))
    with pytest.raises(Refused) as caught:
        load_corpus(root)
    assert caught.value.reason == "manifest_drift"


def test_harness_refuses_leakage_even_when_resealed(corpus_root, tmp_path):
    root = writable_copy(corpus_root, tmp_path / "c")
    path = root / "test.truth.jsonl"
    path.write_text(
        path.read_text().replace('"family":"key_drift_fact"', '"family":"empty_plain"')
    )
    reseal(root)
    with pytest.raises(Refused) as caught:
        load_corpus(root)
    assert caught.value.reason == "leakage"


def test_harness_refuses_a_truth_token_in_a_model_input(corpus_root):
    corpus = Corpus.load(corpus_root)
    assert check_leakage(corpus) == []
    corpus.examples["test"][0]["options"][0][1] += " empty_plain"
    assert any("truth token" in failure for failure in check_leakage(corpus))


def test_harness_refuses_wrong_checkpoint_identity(corpus_root, frozen, tmp_path):
    freeze_path, _ = frozen
    other = fake_checkpoint(tmp_path / "other", "other")
    with pytest.raises(Refused) as caught:
        report(
            corpus_root,
            freeze_path,
            answers(corpus_root, tmp_path / "a.json", other, label_policy),
        )
    assert caught.value.reason == "checkpoint_identity"


@pytest.mark.parametrize(
    "tamper",
    [
        {"complete": False},
        {"records": "drop"},
        {"records": "error"},
        {"records": "malformed"},
    ],
)
def test_harness_refuses_incomplete_results(corpus_root, frozen, tmp_path, tamper):
    freeze_path, pinned = frozen
    path = answers(corpus_root, tmp_path / "a.json", pinned, label_policy)
    payload = json.loads(path.read_text())
    if tamper.get("complete") is False:
        payload["complete"] = False
    elif tamper["records"] == "drop":
        payload["records"].pop()
    elif tamper["records"] == "error":
        payload["records"][0] = {
            "prompt_sha256": payload["records"][0]["prompt_sha256"],
            "status": "error",
        }
    else:
        first = payload["records"][0]
        first["probabilities"] = {k: 0.5 for k in first["probabilities"]}
    path.write_text(json.dumps(payload))
    with pytest.raises(Refused) as caught:
        report(corpus_root, freeze_path, path)
    assert caught.value.reason == "incomplete"


@pytest.mark.parametrize(
    "field", ["model_revision", "requests_sha256", "foreign_prompt"]
)
def test_harness_refuses_mixed_prompt_or_catalog_versions(
    corpus_root, frozen, tmp_path, field
):
    freeze_path, pinned = frozen
    path = answers(corpus_root, tmp_path / "a.json", pinned, label_policy)
    payload = json.loads(path.read_text())
    if field == "foreign_prompt":
        payload["records"][0]["prompt_sha256"] = "0" * 64
    else:
        payload[field] = "other"
    path.write_text(json.dumps(payload))
    with pytest.raises(Refused) as caught:
        report(corpus_root, freeze_path, path)
    assert caught.value.reason == "prompt_version"


def test_harness_refuses_a_changed_harness_or_config_after_freeze(
    corpus_root, frozen, tmp_path
):
    freeze_path, pinned = frozen
    record = json.loads(freeze_path.read_text())
    record["configs_sha256"]["configs/head-only.yaml"] = "0" * 64
    tampered = tmp_path / "freeze2.json"
    tampered.write_text(json.dumps(record))
    with pytest.raises(Refused) as caught:
        report(
            corpus_root,
            tampered,
            answers(corpus_root, tmp_path / "a.json", pinned, label_policy),
        )
    assert caught.value.reason == "manifest_drift"
    with pytest.raises(Refused):
        freeze(corpus_root, tmp_path / "pinned", freeze_path)


@pytest.mark.parametrize("problem", ["test_data", "split", "digest", "same_checkpoint"])
def test_compare_refuses_calibration_outside_the_calibration_partition(
    corpus_root, frozen, tmp_path, problem
):
    freeze_path, pinned = frozen
    adapted = fake_checkpoint(tmp_path / "adapted", "adapted")
    record = {
        "schema": "decider-calibration-record-v1",
        "data_sha256": file_sha256(corpus_root / "calibration.jsonl"),
        "split": "all",
        "checkpoint_digest_after": adapted,
    }
    if problem == "test_data":
        record["data_sha256"] = file_sha256(corpus_root / "test.jsonl")
    elif problem == "split":
        record["split"] = "calib"
    elif problem == "digest":
        record["checkpoint_digest_after"] = pinned
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(record))
    base = answers(corpus_root, tmp_path / "b.json", pinned, label_policy)
    tuned = answers(corpus_root, tmp_path / "t.json", adapted, label_policy)
    digest = pinned if problem == "same_checkpoint" else adapted
    with pytest.raises(Refused) as caught:
        compare(corpus_root, freeze_path, base, tuned, digest, path)
    assert caught.value.reason in {"calibration", "checkpoint_identity"}


def test_scope_command_allows_only_the_experiment(tmp_path):
    from experiments.decider_finetune.corpus import scope, tree

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    baseline = tmp_path / "start.json"
    baseline.write_text(json.dumps({"files": tree(tmp_path)}))
    (tmp_path / "experiments" / "decider_finetune").mkdir(parents=True)
    (tmp_path / "experiments" / "decider_finetune" / "ok.py").write_text("y = 2\n")
    # The baseline file itself is new relative to its own snapshot.
    assert scope(baseline, tmp_path) == ["added outside the experiment: start.json"]
    (tmp_path / "src" / "a.py").write_text("x = 2\n")
    (tmp_path / "experiments" / "decider_finetune" / "bad.py").write_text(
        "import boto3\n"
    )
    problems = scope(baseline, tmp_path)
    assert "modified: src/a.py" in problems
    assert "network or AWS import: experiments/decider_finetune/bad.py" in problems


def test_requests_file_is_the_inference_input_and_is_bound(corpus_root):
    manifest = json.loads((corpus_root / "manifest.json").read_text())
    assert manifest["files"]["test"]["requests"] == file_sha256(
        corpus_root / "test.requests.jsonl"
    )
    assert Path(corpus_root / "manifest.sha256").read_text().strip() == file_sha256(
        corpus_root / "manifest.json"
    )
