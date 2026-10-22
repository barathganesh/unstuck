"""The claims audit refuses a claim its evidence does not support. Offline.

Every assertion here is about provenance, never about whether a claim is true.
The audit's whole job is to stop an unsupported sentence from travelling.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from publishing import PublicationFault, PublicationReason
from publishing.claims import (
    GUARDED_CATEGORIES,
    Claim,
    audit,
    audit_claim,
)


def recorded(root: Path, relative: str) -> str:
    """Create a recorded evidence file and return its relative path."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("offline evidence record\n", encoding="utf-8")
    return relative


def test_a_claim_with_recorded_evidence_passes_offline(tmp_path: Path) -> None:
    path = recorded(tmp_path, "docs/evidence/run.md")
    verdict = audit_claim(
        Claim(
            claim_id="C-1",
            text="The selector runs under two catalogs.",
            category="capability",
            evidence_paths=(path,),
        ),
        tmp_path,
    )
    assert verdict.supported is True
    assert verdict.reason is None
    assert verdict.resolved == (path,)
    assert verdict.missing == ()


def test_a_claim_with_no_evidence_at_all_fails_offline(tmp_path: Path) -> None:
    verdict = audit_claim(
        Claim(
            claim_id="C-2",
            text="The pattern is implemented as described.",
            category="design",
        ),
        tmp_path,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.EVIDENCE_MISSING
    assert verdict.resolved == ()


def test_a_claim_whose_evidence_path_does_not_exist_fails_offline(
    tmp_path: Path,
) -> None:
    verdict = audit_claim(
        Claim(
            claim_id="C-3",
            text="The gate order ran clean.",
            category="capability",
            evidence_paths=("docs/evidence/absent.md",),
        ),
        tmp_path,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.EVIDENCE_UNRESOLVED
    assert verdict.missing == ("docs/evidence/absent.md",)
    assert "absent.md" in verdict.detail


@pytest.mark.parametrize("category", GUARDED_CATEGORIES)
def test_a_guarded_claim_cannot_be_constructed_without_evidence_offline(
    category: str,
) -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="C-4",
            text="A sentence that asserts something measurable.",
            category=category,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("category", GUARDED_CATEGORIES)
def test_a_guarded_claim_is_refused_by_category_when_evidence_is_absent_offline(
    tmp_path: Path, category: str
) -> None:
    verdict = audit_claim(
        Claim(
            claim_id="C-5",
            text="A sentence that asserts something measurable.",
            category=category,  # type: ignore[arg-type]
            evidence_paths=("docs/evidence/never-written.md",),
        ),
        tmp_path,
    )
    assert verdict.supported is False
    assert verdict.reason is PublicationReason.CLAIM_FORBIDDEN, category
    assert category in verdict.detail


def test_the_guarded_set_names_every_forbidden_category_offline() -> None:
    assert set(GUARDED_CATEGORIES) == {
        "novelty",
        "significance",
        "latency_benefit",
        "cost_benefit",
        "compliance",
        "rubric_score",
        "measurement",
    }


def test_a_claim_about_data_must_label_it_synthetic_offline(tmp_path: Path) -> None:
    path = recorded(tmp_path, "docs/evidence/corpus.md")
    unlabelled = audit_claim(
        Claim(
            claim_id="C-6",
            text="The corpus holds one hundred repairable cases.",
            category="design",
            evidence_paths=(path,),
            about_data=True,
        ),
        tmp_path,
    )
    assert unlabelled.supported is False
    assert unlabelled.reason is PublicationReason.LABEL_MISSING

    labelled = audit_claim(
        Claim(
            claim_id="C-7",
            text="The synthetic corpus holds one hundred repairable cases.",
            category="design",
            evidence_paths=(path,),
            about_data=True,
        ),
        tmp_path,
    )
    assert labelled.supported is True


def test_a_controlled_track_claim_must_disclose_the_prefix_offline(
    tmp_path: Path,
) -> None:
    path = recorded(tmp_path, "docs/evidence/track.md")
    undisclosed = audit_claim(
        Claim(
            claim_id="C-8",
            text="The second track exercises a wait outcome.",
            category="design",
            evidence_paths=(path,),
            about_controlled_track=True,
        ),
        tmp_path,
    )
    assert undisclosed.supported is False
    assert undisclosed.reason is PublicationReason.PREFIX_UNDISCLOSED

    disclosed = audit_claim(
        Claim(
            claim_id="C-9",
            text="The controlled incident track exercises a wait outcome.",
            category="design",
            evidence_paths=(path,),
            about_controlled_track=True,
        ),
        tmp_path,
    )
    assert disclosed.supported is True


def test_the_report_refuses_publication_while_one_claim_is_unsupported_offline(
    tmp_path: Path,
) -> None:
    path = recorded(tmp_path, "docs/evidence/one.md")
    report = audit(
        [
            Claim(
                claim_id="C-10",
                text="The recipe is standalone.",
                category="capability",
                evidence_paths=(path,),
            ),
            Claim(
                claim_id="C-11",
                text="A sentence that asserts something measurable.",
                category="measurement",
                evidence_paths=("docs/evidence/absent.md",),
            ),
        ],
        tmp_path,
    )
    assert report.publishable is False
    assert [item.claim_id for item in report.unsupported] == ["C-11"]
    with pytest.raises(PublicationFault) as raised:
        report.require_publishable()
    assert raised.value.reason is PublicationReason.CLAIM_UNSUPPORTED
    assert "C-11" in raised.value.detail


def test_an_empty_claim_set_is_not_publishable_offline(tmp_path: Path) -> None:
    report = audit([], tmp_path)
    assert report.publishable is False, "nothing audited is not the same as cleared"
    with pytest.raises(PublicationFault):
        report.require_publishable()


def test_a_verdict_cannot_record_a_pass_with_a_reason_offline() -> None:
    from publishing.claims import ClaimVerdict

    with pytest.raises(ValidationError):
        ClaimVerdict(
            claim_id="C-12",
            supported=True,
            reason=PublicationReason.EVIDENCE_MISSING,
        )
    with pytest.raises(ValidationError):
        ClaimVerdict(claim_id="C-13", supported=False)
    with pytest.raises(ValidationError):
        ClaimVerdict(
            claim_id="C-14", supported=True, missing=("docs/evidence/absent.md",)
        )
