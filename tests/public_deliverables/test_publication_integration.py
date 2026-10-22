"""Integration over the real repository artifacts. Offline.

Three things are checked end to end: the recipe example runs under both
catalogs from a copied directory, the audit and the checklist report the real
state of this repository rather than a rehearsed one, and the recipe package
leaks nothing.

The module basename is unique across `tests/`: these packages carry no
`__init__.py`, so two files named `test_integration.py` would collide at
collection.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from recipe_paths import RECIPE, RECIPE_FILES, SENTINEL_MARKERS

from publishing import PublicationFault
from publishing.checklist import SUBMISSION_CHECKLIST
from publishing.claims import Claim, audit
from publishing.outlines import ARTICLE_OUTLINE, VIDEO_OUTLINE

ROOT = Path(__file__).resolve().parents[2]


def test_the_example_runs_from_a_copied_directory_offline(recipe_copy: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(recipe_copy / "example.py")],
        capture_output=True,
        text=True,
        cwd=recipe_copy,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    output = completed.stdout
    # The example runs twice, once per catalog, and labels its own output.
    assert output.count("catalog") >= 2
    assert "synthetic" in output.lower()
    for marker in ("exact_repeat", "no_progress", "rechosen"):
        assert marker in output, marker


def test_the_example_output_is_structured_offline(recipe_copy: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(recipe_copy / "example.py")],
        capture_output=True,
        text=True,
        cwd=recipe_copy,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["classification"] == "synthetic"
    assert "none" in document["measurements"], "the example reports no measurement"
    runs = (document["development_catalog"], document["heldout_catalog"])
    for steps in runs:
        assert steps, "each catalog produces a structured run"
        for step in steps:
            assert step["classification"] == "synthetic"
            assert step.get("decisions", 0) <= 2, "no third decision"
            assert step.get("tool_runs", 0) <= 2, "no third read"
    # The same bounded behaviour under different identifiers.
    paths = [[step["path"] for step in steps if "path" in step] for steps in runs]
    assert paths[0] == paths[1]
    identifiers = [
        {step.get("chose") for step in steps if step.get("chose") is not None}
        for steps in runs
    ]
    assert identifiers[0].isdisjoint(identifiers[1]), "the catalogs differ by name"
    # Both runs record a stalled read and reach exactly one re-decision, and the
    # second call refuses the identical repetition before it runs.
    for steps in runs:
        assert any("no_progress" in step.get("outcomes", ()) for step in steps)
        kinds = [
            refusal["kind"]
            for step in steps
            for refusal in step.get("refused_repetitions", ())
        ]
        assert "exact_repeat" in kinds
        assert "failed_execution" in kinds
        decisions = [step["decisions"] for step in steps if "decisions" in step]
        assert max(decisions) == 2
        reads = [step["tool_runs"] for step in steps if "tool_runs" in step]
        assert min(reads) == 1, "a refused repetition costs no read"
    # The private capability never travels in the example's own output either.
    for capability in ("pipeline", "schema_keys", "scope"):
        assert f'"{capability}"' not in completed.stdout, capability


def test_the_audit_reports_this_repositorys_real_state_offline() -> None:
    claims = [
        Claim(
            claim_id="PUB-recipe-standalone",
            text="The recipe imports the standard library only.",
            category="capability",
            evidence_paths=("tests/public_deliverables/test_independence.py",),
        ),
        Claim(
            claim_id="PUB-licence",
            text="Every dependency licence is verified.",
            category="compliance",
            evidence_paths=("docs/evidence/licence-verification.md",),
        ),
        Claim(
            claim_id="PUB-benchmark",
            text="The selected arm reaches a verified success rate.",
            category="measurement",
            evidence_paths=("docs/evidence/benchmark-results.json",),
        ),
        Claim(
            claim_id="PUB-latency",
            text="The pattern lowers latency.",
            category="latency_benefit",
            evidence_paths=("docs/evidence/latency.json",),
        ),
        Claim(
            claim_id="PUB-novelty",
            text="This is a new idea.",
            category="novelty",
            evidence_paths=("docs/evidence/prior-art.md",),
        ),
    ]
    report = audit(claims, ROOT)
    supported = {item.claim_id for item in report.verdicts if item.supported}
    unsupported = {item.claim_id for item in report.unsupported}
    assert supported == {"PUB-recipe-standalone"}, (
        "only the claim whose evidence exists in this repository is supported"
    )
    assert unsupported == {
        "PUB-licence",
        "PUB-benchmark",
        "PUB-latency",
        "PUB-novelty",
    }
    assert report.publishable is False
    with pytest.raises(PublicationFault):
        report.require_publishable()


def test_the_checklist_over_this_repository_is_entirely_pending_offline() -> None:
    report = SUBMISSION_CHECKLIST.resolve(ROOT, frozenset())
    assert report.ready is False
    assert len(report.pending) == len(SUBMISSION_CHECKLIST.items)
    with pytest.raises(PublicationFault):
        report.require_ready()


def test_the_rendered_outlines_match_the_committed_documents_offline() -> None:
    pairs = (
        (ARTICLE_OUTLINE, ROOT / "docs" / "article-outline.md"),
        (VIDEO_OUTLINE, ROOT / "docs" / "video-outline.md"),
    )
    for outline, document in pairs:
        assert document.exists(), document
        expected = outline.render().rstrip("\n")
        actual = document.read_text(encoding="utf-8").rstrip("\n")
        assert actual == expected, f"{document.name} has drifted from the outline"


def test_the_recipe_package_leaks_nothing_offline() -> None:
    #: Paths and tokens that must never appear inside the recipe.
    forbidden = (
        "evaluation/",
        "truth",
        "holdout",
        "held_out",
        "answer_key",
        "expected_answer",
        "root_cause",
        "aws_access_key",
        "secret",
        "password",
        "bedrock",
        "boto3",
        "dynamodb",
        "fastapi",
        "duckdb",
        "pydantic",
    )
    for name in RECIPE_FILES:
        text = (RECIPE / name).read_text(encoding="utf-8")
        lowered = text.lower()
        for token in forbidden:
            assert token not in lowered, f"{name} carries {token}"
        for marker in SENTINEL_MARKERS:
            assert marker.lower() not in lowered, f"{name} carries {marker}"


@pytest.mark.parametrize(
    "artifact",
    [
        ROOT / "NOTICE",
        ROOT / "docs" / "article-outline.md",
        ROOT / "docs" / "video-outline.md",
        ROOT / "publishing" / "__init__.py",
        ROOT / "publishing" / "claims.py",
        ROOT / "publishing" / "evidence.py",
        ROOT / "publishing" / "checklist.py",
        ROOT / "publishing" / "outlines.py",
    ],
    ids=lambda artifact: artifact.relative_to(ROOT).as_posix(),
)
def test_no_publication_artifact_carries_a_personal_identity_offline(
    artifact: Path,
) -> None:
    lowered = artifact.read_text(encoding="utf-8").lower()
    for marker in SENTINEL_MARKERS:
        assert marker.lower() not in lowered, f"{artifact.name} carries {marker}"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", lowered), artifact.name
