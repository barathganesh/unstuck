"""The same-device CPU parity record: scrubbed, self-consistent, frozen criteria.

The record is the sanitized summary of one private replay of the frozen
held-out requests against the baseline and the adapted checkpoint, both served
on cpu. These tests read only the tracked record. They assert that it carries no
operational identifier, that every count it states agrees with every other
count it states, and that its verdict is what the frozen criteria give for its
own integrated counts. Offline; nothing is invoked.

Focused command:
.venv/bin/python -m pytest tests/decider_finetune/test_device_parity_evidence.py -q -p no:cacheprovider
"""

import base64
import json
import re
from pathlib import Path

import pytest

from experiments.decider_finetune import evaluate as ev
from publishing.public_scan import checks_for, scan_text

ROOT = Path(__file__).resolve().parents[2]
PARITY = ROOT / "experiments" / "decider_finetune" / "device_parity"
RECORD = PARITY / "device-parity-cpu-v1.json"
LORA = ROOT / "dist" / "public-demo" / "decider-finetune-lora-head-v1.json"

#: Stricter than the shared scan: anything that names where or when this ran.
FORBIDDEN = (
    r"\b\d{12}\b",
    r"arn:",
    r"s3://",
    r"\b[a-z]{2}-(?:north|south|east|west|central|northeast|southeast)+-\d\b",
    r"unstuck-decider|unstuck-artifacts|models/decider",
    r"canary-2026|parity-2026",
    r"Unstuck[A-Z]\w+Role",
    r"\b(?:sg|subnet|vpc|eni)-[0-9a-f]+",
    r"/Users/|/private/|/tmp/|\.venv|\.unlazy",
    r"\d{4}-\d{2}-\d{2}",
    r"\b\d{2}:\d{2}(?::\d{2})?Z?\b",
    r"\b[0-9a-f]{16,}\b",
    r"AKIA|ASIA|aws_secret|session_token|Bearer\s",
    r"scripted-v\d",
    r"sha256:",
    r"isengard|Admin",
    r"\bml\.[a-z0-9]+\.[a-z0-9]+\b",
    r"VersionId|ETag",
    "—",
)


def _record() -> dict:
    return json.loads(RECORD.read_text())


def _fraction(text: str) -> tuple[int, int]:
    numerator, denominator = text.split("/")
    return int(numerator), int(denominator)


def _tracked_files() -> list[Path]:
    return sorted(p for p in PARITY.rglob("*") if p.is_file())


def test_the_tracked_evidence_directory_holds_only_the_record_and_its_note() -> None:
    names = sorted(p.name for p in _tracked_files())
    assert names == ["FINDINGS.md", "device-parity-cpu-v1.json"]


@pytest.mark.parametrize("path", _tracked_files(), ids=lambda p: p.name)
def test_the_shared_leakage_scan_finds_nothing(path: Path) -> None:
    findings = scan_text(path.name, path.read_text(), checks_for("prose"))
    assert findings == []


@pytest.mark.parametrize("path", _tracked_files(), ids=lambda p: p.name)
def test_no_operational_identifier_survives(path: Path) -> None:
    text = path.read_text()
    hits = [pattern for pattern in FORBIDDEN if re.search(pattern, text)]
    assert hits == []


def test_checkpoint_identities_match_the_published_lora_record() -> None:
    record, published = _record(), json.loads(LORA.read_text())
    assert (
        record["baseline_checkpoint_hash_base64"]
        == published["baseline_checkpoint_hash_base64"]
    )
    assert (
        record["adapted_checkpoint_hash_base64"]
        == published["adapted_checkpoint_hash_base64"]
    )
    for key in ("baseline_checkpoint_hash_base64", "adapted_checkpoint_hash_base64"):
        assert len(base64.b64decode(record[key])) == 32


def test_both_checkpoints_were_compared_on_cpu() -> None:
    same = _record()["same_device_comparison"]
    assert same["devices"] == ["cpu", "cpu"]
    assert same["baseline"]["device"] == same["adapted"]["device"] == "cpu"
    assert _record()["replay"]["requests_answered_per_checkpoint"] == "160/160"


@pytest.mark.parametrize("side", ["baseline", "adapted"])
@pytest.mark.parametrize("block", ["same_device_comparison", "offline_reference"])
def test_every_metric_is_well_formed(block: str, side: str) -> None:
    m = _record()[block][side]
    for key, size in (
        ("first_choice_correct", 160),
        ("row_wasted_first_reads", 160),
        ("row_no_read_choices", 160),
        ("integrated_wasted_first_reads", 64),
        ("integrated_verified_completions", 64),
        ("integrated_first_choice_correct", 64),
    ):
        count, of = _fraction(m[key])
        assert of == size and 0 <= count <= of, key
    for key in ("ece_normalized_confidence", "ece_top_label"):
        assert 0.0 <= m[key] <= 1.0, key
    assert m["nll"] > 0.0
    assert m["row_false_repairs"] == m["integrated_false_repairs"] == 0
    assert m["integrated_false_preclusions"] == 0


def test_paired_counts_agree_with_the_per_side_counts() -> None:
    same = _record()["same_device_comparison"]
    base, tuned = same["baseline"], same["adapted"]
    pi = same["paired_integrated"]
    assert pi["wasted_first_reads"] == (
        f"{base['integrated_wasted_first_reads']} to {tuned['integrated_wasted_first_reads']}"
    )
    assert pi["verified_completions"] == (
        f"{base['integrated_verified_completions']} to {tuned['integrated_verified_completions']}"
    )
    b_correct, _ = _fraction(base["first_choice_correct"])
    a_correct, _ = _fraction(tuned["first_choice_correct"])
    rows = same["paired_rows"]
    assert a_correct - b_correct == rows["corrections"] - rows["regressions"]


def test_the_verdict_is_what_the_frozen_criteria_give() -> None:
    same = _record()["same_device_comparison"]
    base, tuned = same["baseline"], same["adapted"]
    wasted_b, _ = _fraction(base["integrated_wasted_first_reads"])
    wasted_a, _ = _fraction(tuned["integrated_wasted_first_reads"])
    done_b, _ = _fraction(base["integrated_verified_completions"])
    done_a, _ = _fraction(tuned["integrated_verified_completions"])
    regressions = same["paired_integrated"]["completion_regressions"]
    criteria = {
        "G13_wasted_first_reads_reduced_20pct": wasted_b > 0
        and wasted_a <= ev.WASTED_RATIO_MAX * wasted_b,
        "G14_no_completion_regression": regressions == 0 and done_a >= done_b,
        "G15_zero_false_repairs": tuned["integrated_false_repairs"] == 0
        and tuned["row_false_repairs"] == 0,
        "G15_zero_false_preclusions": tuned["integrated_false_preclusions"] == 0,
    }
    assert same["frozen_criteria"] == {
        k: "met" if v else "unmet" for k, v in criteria.items()
    }
    expected = "promotion_eligible" if all(criteria.values()) else "promotion_refused"
    assert same["frozen_harness_verdict"] == expected


@pytest.mark.parametrize(
    "pair",
    [
        "adapted_cpu_versus_adapted_cuda",
        "adapted_cpu_versus_earlier_adapted_cpu_replay",
        "baseline_cpu_versus_baseline_offline_accelerator",
    ],
)
def test_device_variance_counts_agree_with_the_rows_listed(pair: str) -> None:
    p = _record()["device_variance"][pair]
    agree, of = _fraction(p["choice_agreement"])
    assert of == 160 and agree == 160 - len(p["disagreeing_rows"])
    identical, _ = _fraction(p["rows_with_identical_probabilities"])
    assert identical <= agree
    assert (p["max_absolute_probability_difference"] == 0.0) == (identical == 160)
    assert (
        p["mean_absolute_probability_difference"]
        <= p["max_absolute_probability_difference"]
    )
    for row in p["disagreeing_rows"]:
        assert 0 <= row["row"] < 160
        assert row["first"]["choice"] != row["second"]["choice"]
        for side in ("first", "second"):
            (top, p_top), (_, p_second) = row[side]["top_two"]
            assert row[side]["choice"] == top and p_top >= p_second


def test_the_two_ece_definitions_differ_as_documented() -> None:
    """Normalized confidence is (n * max - 1) / (n - 1); top-label is max itself."""
    assert ev.derive_confidence([0.7, 0.1, 0.1, 0.1]) == pytest.approx(
        (4 * 0.7 - 1) / 3
    )
    definitions = _record()["metric_definitions"]
    assert "(n * max - 1) / (n - 1)" in definitions["ece_normalized_confidence"]
    assert "max probability" in definitions["ece_top_label"]


def test_no_threshold_latency_or_cost_claim_is_made() -> None:
    record = _record()
    text = json.dumps(record).lower()
    assert (
        "latency"
        not in json.dumps(
            {k: v for k, v in record.items() if k != "limitations"}
        ).lower()
    )
    assert "no latency benefit, cost benefit or cost saving is claimed." in text
    assert "none was changed for this study" in text
