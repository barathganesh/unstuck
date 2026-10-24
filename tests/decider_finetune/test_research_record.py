"""The v1 research record says what the evidence supports, and nothing more.

Every corrected statement about the Decider fine-tune is pinned here: the test
partition is controlled variation of the training distribution, ECE and NLL
are descriptive, the wasted-read change is five calls on one seed, zero false
repairs and preclusions are not safety evidence, and the later hosted canary
is named without any hosted metric or identifier. Each prohibition is checked
against every site that once carried the claim.
"""

import json
import re
from pathlib import Path

import pytest

from publishing.public_scan import scan_file

ROOT = Path(__file__).resolve().parents[2]
RECORDS = {
    stage: f"dist/public-demo/decider-finetune-{stage}-v1.json"
    for stage in ("baseline", "head-only", "lora-head")
}
RESEARCH = "docs/research/decider-adaptation-findings.md"
EXPERIMENT_README = "experiments/decider_finetune/README.md"
CONFIG = "experiments/decider_finetune/configs/head-only.yaml"

CANARY = (
    "A private hosted canary was run later and missed its pre-registered "
    "exact-choice parity gate, so no hosted result is claimed."
)
IN_DISTRIBUTION = "all 20 model-visible test situations occur in training"
#: Figures that exist only in the private canary record. None may appear.
HOSTED_FIGURES = ("132/160", "14/64", "50/64", "157/160", "0.196", "0.0316", "0.0301")
#: Wording that presented the test partition as a test on new situations.
NOVELTY = re.compile(r"\bunseen\b|\bhold-?out\b|\bheld[- ]out\b|generali[sz]", re.I)
#: The one place each document may quote the frozen files' own wording.
FROZEN_WORDING = '"held-out"'
#: The calibration gate that was documented and never existed.
ECE_GATE = re.compile(
    r"within \+0\.05|maximum_allowed_delta|CALIBRATION_ECE_MAX_DELTA|ece_delta"
    r"|calibration gate output|calibration_ece_within",
    re.I,
)
#: Seven-decimal ECE and NLL values overstated their precision.
FALSE_PRECISION = re.compile(r"\b0\.\d{5,}\b")
CONTENT_OUTSIDE_SCOPE = re.compile(
    r"\b(nba|basketball|sports?|customers?|submission|hackathon|judges?|industry)\b",
    re.I,
)


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def flat(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("**", "")).strip()


def section(relative: str, heading: str) -> str:
    body = text(relative)
    assert heading in body, f"{relative} lost {heading!r}"
    after = body.split(heading, 1)[1]
    return heading + re.split(r"\n## ", after, maxsplit=1)[0]


def sites() -> dict[str, str]:
    """Every text that states the v1 fine-tune result."""
    return {
        "README.md": section("README.md", "### The offline fine-tune evidence"),
        "docs/claim-register.md": section(
            "docs/claim-register.md", "## Decider fine-tune evidence"
        ),
        "docs/hackathon-validation.md": section(
            "docs/hackathon-validation.md", "## The Decider fine-tune experiment"
        ),
        EXPERIMENT_README: text(EXPERIMENT_README),
        RESEARCH: text(RESEARCH),
        **{path: text(path) for path in RECORDS.values()},
    }


def record(stage: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(text(RECORDS[stage]))
    return loaded


PROSE = ("README.md", "docs/claim-register.md", "docs/hackathon-validation.md")


# ---------------------------------------------------------------------------
# Controlled variation, not new situations


@pytest.mark.parametrize("name", [*PROSE, EXPERIMENT_README])
def test_the_test_partition_is_called_controlled_variation(name: str) -> None:
    body = flat(sites()[name])
    assert "controlled variation" in body
    assert "in-distribution" in body
    assert IN_DISTRIBUTION in body.replace("All 20", "all 20")


@pytest.mark.parametrize("name", [*PROSE, EXPERIMENT_README, RESEARCH])
def test_the_renamed_catalog_slice_is_distinguished(name: str) -> None:
    body = flat(sites()[name])
    assert "64/80 to 70/80" in body
    assert "never appear" in body


@pytest.mark.parametrize("name", sorted(sites()))
def test_no_site_presents_the_test_partition_as_unseen(name: str) -> None:
    lines = [
        line
        for line in sites()[name].splitlines()
        if NOVELTY.search(line) and FROZEN_WORDING not in line
    ]
    assert lines == []


def test_the_novelty_detector_catches_the_old_wording() -> None:
    for old in ("four held-out scenario families", "an offline synthetic holdout"):
        assert NOVELTY.search(old)
    assert not NOVELTY.search("STOP_INSUFFICIENT_HOLDOUT")


@pytest.mark.parametrize("stage", sorted(RECORDS))
def test_every_record_states_the_in_distribution_scope(stage: str) -> None:
    data = record(stage)
    assert data["classification"] == (
        "offline synthetic, in-distribution controlled variation"
    )
    scope = data["sample_sizes"]
    assert isinstance(scope, dict)
    assert scope["model_visible_situations"] == 20
    assert scope["model_visible_situations_also_in_training"] == 20
    assert scope["renamed_catalog_rows"] == 80
    assert scope["integrated_calls_replay_choice_row_answers"] is True


# ---------------------------------------------------------------------------
# ECE and NLL are descriptive


@pytest.mark.parametrize("name", sorted(sites()))
def test_no_site_claims_the_calibration_gate_that_never_existed(name: str) -> None:
    assert ECE_GATE.findall(sites()[name]) == []


@pytest.mark.parametrize("name", sorted(sites()))
def test_no_site_overstates_precision(name: str) -> None:
    assert FALSE_PRECISION.findall(sites()[name]) == []


@pytest.mark.parametrize("name", PROSE)
def test_prose_says_no_calibration_gate_was_applied(name: str) -> None:
    body = flat(sites()[name]).lower()
    assert "no calibration gate was applied" in body
    assert "normalized confidence" in body


def test_the_documented_confidence_formula_is_the_harness_formula() -> None:
    from experiments.decider_finetune.evaluate import derive_confidence

    assert "(n * pmax - 1) / (n - 1)" in text(EXPERIMENT_README)
    assert derive_confidence([0.5, 0.25, 0.25]) == pytest.approx((3 * 0.5 - 1) / 2)


def test_the_compare_source_has_no_calibration_constant() -> None:
    source = text("experiments/decider_finetune/evaluate.py")
    assert "CALIBRATION_ECE_MAX_DELTA" not in source and "ece_delta" not in source


@pytest.mark.parametrize("stage", sorted(RECORDS))
def test_record_metrics_are_rounded_and_described(stage: str) -> None:
    data = record(stage)
    block = data.get("metrics") or data["comparison"]
    assert isinstance(block, dict)
    definition = block["ece_definition"]
    assert "normalized confidence" in definition["confidence"]
    assert definition["use"].startswith("descriptive only")
    values: list[float] = []
    for key in ("ece", "nll"):
        item = block[key]
        values.extend(item.values() if isinstance(item, dict) else [item])
    assert values and all(round(v, 3) == v for v in values)


def test_the_eligible_verdict_is_scoped_away_from_calibration() -> None:
    comparison = record("lora-head")["comparison"]
    assert isinstance(comparison, dict)
    assert comparison["verdict"] == "promotion_eligible"
    assert "does not check calibration" in comparison["verdict_scope"]
    head = record("head-only")["comparison"]
    assert isinstance(head, dict)
    assert head["refused_on"].startswith("the wasted-first-read criterion")


# ---------------------------------------------------------------------------
# 20 to 15 is five calls, and 0/0 is not safety evidence


@pytest.mark.parametrize("name", [*PROSE, EXPERIMENT_README, RESEARCH])
def test_the_wasted_read_change_is_qualified(name: str) -> None:
    body = flat(sites()[name])
    assert "20/64 to 15/64" in body
    assert "five improved calls" in body
    assert "one seed" in body
    assert "in one family" in body
    assert "p = 0.0625" in body


@pytest.mark.parametrize("name", sorted(sites()))
def test_no_site_states_the_unqualified_percentage(name: str) -> None:
    assert "25%" not in sites()[name]


def test_the_wasted_read_record_carries_its_qualifiers() -> None:
    comparison = record("lora-head")["comparison"]
    assert isinstance(comparison, dict)
    wasted = comparison["integrated_wasted_first_reads"]
    assert (wasted["baseline"], wasted["adapted"]) == ("20/64", "15/64")
    assert (wasted["improved_calls"], wasted["worsened_calls"]) == (5, 0)
    assert wasted["improved_calls_in_one_family"] == 4
    assert wasted["exact_paired_p_two_sided"] == 0.0625
    assert wasted["seed_count"] == 1
    assert "reduction" not in wasted


@pytest.mark.parametrize("name", [*PROSE, EXPERIMENT_README, RESEARCH])
def test_zero_false_repairs_are_called_unreachable(name: str) -> None:
    body = flat(sites()[name])
    assert "structurally unreachable" in body
    assert "not safety evidence" in body
    assert "kept false repairs" not in body


def test_the_records_mark_zero_errors_as_unreachable() -> None:
    expected = {
        "observed": "0/0",
        "structurally_reachable_in_these_families": False,
        "safety_evidence": False,
    }
    baseline = record("baseline")["metrics"]
    assert isinstance(baseline, dict)
    assert baseline["false_repairs_and_false_preclusions"] == expected
    comparison = record("lora-head")["comparison"]
    assert isinstance(comparison, dict)
    zeros = comparison["false_repairs_and_false_preclusions"]
    assert zeros == {"baseline": expected, "adapted": expected}
    for stage in RECORDS:
        body = text(RECORDS[stage])
        assert '"false_repairs":' not in body and '"false_preclusions":' not in body


# ---------------------------------------------------------------------------
# Deployment status: the hosted canary, with no hosted figure


@pytest.mark.parametrize(
    "name",
    [*PROSE, EXPERIMENT_README, RESEARCH, RECORDS["baseline"], RECORDS["lora-head"]],
)
def test_every_site_states_the_canary_outcome(name: str) -> None:
    assert CANARY in flat(sites()[name])


@pytest.mark.parametrize("name", sorted(sites()))
def test_no_site_says_the_adapted_or_baseline_checkpoint_was_not_deployed(
    name: str,
) -> None:
    body = flat(sites()[name])
    allowed = "The head-only checkpoint was not deployed."
    assert "not deployed" not in body.replace(allowed, "")


@pytest.mark.parametrize("name", sorted(sites()))
def test_no_site_carries_a_hosted_figure(name: str) -> None:
    body = sites()[name]
    assert [figure for figure in HOSTED_FIGURES if figure in body] == []


# ---------------------------------------------------------------------------
# Identity: public open-model provenance, never a deployment identity


def _public_revision() -> str:
    found = re.search(r"--revision ([0-9a-f]{40})\b", text(CONFIG))
    assert found, "the config template lost its public open-model provenance"
    return found.group(1)


def test_the_config_keeps_the_public_open_model_provenance() -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", _public_revision())


@pytest.mark.parametrize(
    "name", [*PROSE, RESEARCH, *RECORDS.values(), "docs/public-scrub-policy.md"]
)
def test_no_public_file_ties_the_revision_to_a_deployment(name: str) -> None:
    body = text(name)
    revision = _public_revision()
    assert revision not in body and revision[:8] not in body


@pytest.mark.parametrize("stage", sorted(RECORDS))
def test_records_carry_no_checkpoint_digest_or_unbacked_figure(stage: str) -> None:
    data = record(stage)
    assert not [key for key in data if "checkpoint_hash" in key]
    assert "public open-model provenance" in str(data["baseline_checkpoint"])
    if "adapted_checkpoint" in data:
        assert "not published" in str(data["adapted_checkpoint"])
    training = data.get("training", {})
    assert isinstance(training, dict)
    for unbacked in (
        "total_parameters",
        "peak_memory_gib",
        "trainable_parameter_fraction",
    ):
        assert unbacked not in training


def test_the_register_separates_provenance_from_deployment_identity() -> None:
    register = flat(text("docs/claim-register.md"))
    assert "The revision value is not published" not in register
    assert "for both the local and the cloud work, throughout" not in register
    assert "public open-model provenance" in register
    assert "private deployment identity" in register
    validation = flat(text("docs/hackathon-validation.md"))
    assert "The revision value is private and is not published" not in validation
    assert "which is private and is not published, raw or digested" not in validation


def test_claim_identifiers_are_unique() -> None:
    ids = re.findall(
        r"^\| ([A-Z]{1,2}\d+[a-z]?) \|", text("docs/claim-register.md"), re.M
    )
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert ids and duplicates == []
    assert {f"FT{n}" for n in range(1, 10)} <= set(ids)


# ---------------------------------------------------------------------------
# Hygiene: paths, wording, scope, ignore rules


def _tree() -> list[Path]:
    roots = ("experiments", "tests/decider_finetune", "docs/research")
    suffixes = {".py", ".md", ".yaml", ".json"}
    return [
        path
        for root in roots
        for path in (ROOT / root).rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "__pycache__" not in path.parts
    ]


def test_no_machine_local_or_virtual_environment_path() -> None:
    local = re.compile(r"(?<![\w.])/(Users|home|private|tmp|var/folders)/|\.venv[/]")
    hits = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _tree()
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if local.search(line)
    ]
    assert hits == []


def test_no_em_dash_in_the_research_tree_or_records() -> None:
    dash = chr(0x2014)
    files = [*_tree(), *(ROOT / path for path in RECORDS.values())]
    assert [str(p.relative_to(ROOT)) for p in files if dash in p.read_text()] == []


@pytest.mark.parametrize("name", [RESEARCH, *RECORDS.values()])
def test_research_files_pass_the_public_scan(name: str) -> None:
    assert scan_file(name) == []


def test_the_research_record_stays_generic() -> None:
    from experiments.decider_finetune.scenarios import FAMILIES

    body = text(RESEARCH)
    assert CONTENT_OUTSIDE_SCOPE.findall(body) == []
    assert [family for family in FAMILIES if family in body] == []


def test_the_research_record_captures_the_v2_stop() -> None:
    body = flat(text(RESEARCH))
    assert "STOP_INSUFFICIENT_HOLDOUT" in body
    for row in (
        "| Verifier-labelled decisions | 390 |",
        "| Distinct canonical situations | 20 |",
        "| v1 train and test intersection | 20 |",
        "| v1 test situations absent from training | 0 |",
    ):
        assert row in body
    assert "no model was trained or evaluated" in body


def test_the_research_record_lists_the_audit_limitations() -> None:
    body = flat(text(RESEARCH))
    for limitation in (
        "One seed.",
        "Device confound.",
        "Test reuse.",
        "Simple comparators.",
        "Self-attested provenance.",
        "Coverage gap.",
        "Public truth.",
        "Unbacked figures removed.",
        "Frozen wording.",
    ):
        assert limitation in body


def test_local_working_records_are_ignored() -> None:
    assert ".unlazy/" in text(".gitignore").splitlines()
