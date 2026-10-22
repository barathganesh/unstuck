"""Step 4: receipt admission, repository, and isolation tests.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/confidence_cascade/test_receipts.py -q
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from unstuck.cascade.receipts import (
    OUTCOME_REASONS,
    DecisionReceipt,
    EvidenceDelta,
    ReceiptNamespace,
    receipt_identity,
)
from unstuck.cascade.repository import (
    LocalReceiptRepository,
    ReceiptWriteToken,
    mint_write_token,
)
from unstuck.cascade.verifier import DiagnosticReport
from unstuck.domain import Reason, RuntimeFault

DIGEST = "a" * 64


def test_admission_only_the_verifier_may_write_a_receipt(
    repository, verifier, make_receipt, useful_report, namespace
):
    receipt = make_receipt(useful_report)
    for forged in (
        ReceiptWriteToken(authority=object()),
        ReceiptWriteToken(authority=None),
        ReceiptWriteToken(authority="verifier"),
    ):
        with pytest.raises(RuntimeFault) as error:
            repository.put(receipt, forged)
        assert error.value.reason is Reason.RECEIPT_INVALID
    assert repository.count(namespace) == 0
    assert repository.put(receipt, verifier.token) is True
    assert repository.count(namespace) == 1
    # The write capability is not reachable from the receipt contract itself.
    assert mint_write_token().valid()
    assert not ReceiptWriteToken(authority=object()).valid()


def test_admission_writes_are_conditional_and_idempotent(
    repository, verifier, make_receipt, useful_report, namespace
):
    receipt = make_receipt(useful_report)
    assert repository.put(receipt, verifier.token) is True
    assert repository.put(receipt, verifier.token) is False
    assert repository.count(namespace) == 1
    stored = repository.find(namespace, receipt.problem_fingerprint)
    assert stored == (receipt,)
    # A different verified fact is a different row, never an overwrite.
    other = make_receipt(useful_report)
    assert other.receipt_id != receipt.receipt_id
    assert repository.put(other, verifier.token) is True
    assert repository.count(namespace) == 2
    # Retrieval order is receipt identity, not insertion order, so the first
    # write is still stored and is still retrievable beside the second.
    found = repository.find(namespace, receipt.problem_fingerprint)
    assert found == tuple(sorted((receipt, other), key=lambda row: row.receipt_id))


def test_admission_outcome_class_is_derived_and_never_supplied(
    verifier, make_report, make_receipt
):
    cases = {
        ("evidence", True): "useful_evidence",
        ("evidence", False): "no_progress",
        ("provider_error", False): "provider_error",
        ("invalid_evidence", False): "invalid_evidence",
    }
    for (status, changed), expected in cases.items():
        report = make_report(status=status, changed=changed)
        assert verifier.classify(report) == expected
        assert make_receipt(report).outcome_class == expected
    # No field of the controller-owned report can state an outcome class.
    assert "outcome_class" not in DiagnosticReport.model_fields
    assert "confidence" not in DiagnosticReport.model_fields
    with pytest.raises(RuntimeFault):
        verifier.classify({"status": "evidence"})


#: FR5.4 as a table. The key is (status, new canonical fact, moved gate); the
#: value is the derived outcome class and whether the receipt may block.
FR54_TABLE: dict[tuple[str, bool, bool], tuple[str, bool]] = {
    ("evidence", True, True): ("useful_evidence", False),
    ("evidence", True, False): ("disposition_unchanged", True),
    ("evidence", False, False): ("no_progress", True),
    ("provider_error", False, False): ("provider_error", False),
    ("invalid_evidence", False, False): ("invalid_evidence", False),
}

FR54_REASONS: dict[tuple[str, bool, bool], Reason] = {
    ("evidence", True, True): Reason.POPULATED_INITIAL,
    ("evidence", True, False): Reason.INSUFFICIENT,
    ("evidence", False, False): Reason.INSUFFICIENT,
    ("provider_error", False, False): Reason.TOOL_ERROR,
    ("invalid_evidence", False, False): Reason.CONFLICT,
}


def split_delta(changed: bool, progressed: bool) -> EvidenceDelta:
    """A delta that states the evidence and disposition claims separately."""
    return EvidenceDelta(
        evidence_before_hash=DIGEST,
        evidence_after_hash=("b" * 64) if changed else DIGEST,
        evidence_ids=(uuid4(),) if changed else (),
        disposition_before_hash=DIGEST,
        disposition_after_hash=("c" * 64) if progressed else DIGEST,
    )


@pytest.mark.parametrize("key", list(FR54_TABLE))
def test_admission_fr54_no_progress_is_the_conjunction_of_both_negatives(
    verifier, make_report, make_receipt, key
):
    """No progress requires zero new admissible facts AND zero moved gates."""
    status, changed, progressed = key
    expected, blocking = FR54_TABLE[key]
    report = make_report(
        status=status,
        changed=changed,
        admissible=status == "evidence",
        reason_code=FR54_REASONS[key],
        delta=split_delta(changed, progressed),
    )
    assert report.delta.changed is changed
    assert report.delta.progressed is progressed
    assert report.delta.stalled is (not changed and not progressed)
    assert verifier.classify(report) == expected
    receipt = make_receipt(report)
    assert receipt.outcome_class == expected
    assert receipt.blocking is blocking


def test_admission_fr54_an_admitted_fact_that_opens_no_gate_is_its_own_class(
    verifier, make_report, make_receipt
):
    """The two dimensions are independent, so this combination has a name.

    The read admitted a real fact, so it is not the strict no-progress class.
    It moved no supported disposition, so it is not useful either. It is the
    disposition-neutral class, and that is what makes the repeated blind-spot
    choice refusable at all.
    """
    report = make_report(
        changed=True,
        reason_code=Reason.INSUFFICIENT,
        delta=split_delta(changed=True, progressed=False),
    )
    assert verifier.classify(report) == "disposition_unchanged"
    receipt = make_receipt(report)
    assert receipt.outcome_class == "disposition_unchanged"
    assert receipt.delta.changed and not receipt.delta.progressed
    assert receipt.delta.neutral is True
    assert receipt.delta.stalled is False
    assert receipt.delta.evidence_ids
    assert receipt.blocking is True
    # `insufficient_evidence` is what the fact opened, not what it admitted.
    assert Reason.INSUFFICIENT in OUTCOME_REASONS["disposition_unchanged"]
    assert Reason.INSUFFICIENT in OUTCOME_REASONS["no_progress"]


def test_admission_fr54_a_no_progress_receipt_may_never_name_admitted_evidence(
    make_receipt, make_report
):
    """The receipt contract refuses the deviation even if a caller forges it."""
    stalled = make_receipt(
        make_report(
            changed=False,
            reason_code=Reason.INSUFFICIENT,
            delta=split_delta(changed=False, progressed=False),
        )
    )
    assert stalled.outcome_class == "no_progress" and stalled.blocking
    payload = stalled.model_dump() | {
        "delta": split_delta(changed=True, progressed=False).model_dump(mode="json")
    }
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(
            payload | {"receipt_id": receipt_identity(payload)}
        )
    # And the controller-owned report refuses it one layer earlier.
    with pytest.raises(ValidationError):
        DiagnosticReport.model_validate(
            make_report(changed=True, reason_code=Reason.INSUFFICIENT).model_dump()
            | {
                "delta": split_delta(changed=True, progressed=False).model_dump(
                    mode="json"
                ),
                "status": "invalid_evidence",
                "admissible": False,
            }
        )


def test_admission_rejects_a_delta_that_does_not_match_the_status(make_report):
    for status in ("provider_error", "invalid_evidence"):
        with pytest.raises(ValidationError):
            make_report(status=status, changed=True)
    with pytest.raises(ValidationError):
        make_report(status="evidence", admissible=False)
    with pytest.raises(ValidationError):
        make_report(status="provider_error", admissible=True, changed=False)
    completed = make_report(status="evidence", changed=False)
    with pytest.raises(ValidationError):
        DiagnosticReport.model_validate(
            completed.model_dump() | {"diagnostic_call_id": None}
        )
    # A read that reports evidence without admitting any is explicit no progress.
    assert make_report(status="evidence", changed=False).outcome_class == "no_progress"


def test_admission_enforces_the_closed_reason_taxonomy(make_report):
    assert OUTCOME_REASONS["no_progress"] == frozenset(
        {
            Reason.INSUFFICIENT,
            Reason.NO_READ,
            Reason.UNSUPPORTED_REPAIR,
            Reason.REPAIR_FAILED,
            Reason.DIAGNOSTICS_EXHAUSTED,
        }
    )
    for reason in (Reason.LEASE_EXPIRED, Reason.STORAGE_ERROR, Reason.DEADLINE):
        with pytest.raises(ValidationError):
            make_report(status="evidence", changed=False, reason_code=reason)
    for reason in OUTCOME_REASONS["no_progress"]:
        assert (
            make_report(
                status="evidence", changed=False, reason_code=reason
            ).reason_code
            is reason
        )
    # A progress reason cannot describe a read that admitted nothing.
    with pytest.raises(ValidationError):
        make_report(
            status="evidence", changed=False, reason_code=Reason.POPULATED_INITIAL
        )


def test_admission_binds_the_receipt_to_the_current_context(
    make_receipt, useful_report, context, namespace
):
    receipt = make_receipt(useful_report)
    assert receipt.problem_fingerprint == context.problem_fingerprint
    assert receipt.decision_context_hash == context.decision_context_hash
    assert receipt.versions == context.versions
    assert receipt.namespace == namespace
    assert receipt.selected_capability == "inspect_schema_keys"
    assert receipt.arguments_hash == useful_report.arguments_hash
    assert receipt.delta == useful_report.delta
    assert receipt.diagnostic_call_id == useful_report.diagnostic_call_id


def test_admission_rejects_unknown_or_mismatched_capability_identity(
    verifier, namespace, context, make_report
):
    arguments = {
        "namespace": namespace,
        "decision_id": uuid4(),
        "model_call_id": uuid4(),
        "context": context,
    }
    mismatched = make_report(
        capability="inspect_schema_keys", public_tool_id="inspect_scope"
    )
    with pytest.raises(RuntimeFault) as error:
        verifier.create(report=mismatched, **arguments)
    assert error.value.reason is Reason.RECEIPT_INVALID
    with pytest.raises(ValidationError):
        make_report(capability="not a capability")
    unknown = make_report(capability="inspect_future", public_tool_id="inspect_future")
    with pytest.raises(RuntimeFault):
        verifier.create(report=unknown, **arguments)
    # A published alias resolves to the same canonical capability.
    aliased = make_report(capability="probe_scope", public_tool_id="inspect-scope")
    assert verifier.create(report=aliased, **arguments).selected_capability == (
        "inspect_scope"
    )


@pytest.mark.parametrize(
    "case_id",
    ["evaluator_only", "hidden_cause_a", "expected_rows_12", "authorization_token"],
)
def test_security_admission_rejects_evaluator_and_secret_markers(
    verifier, context, make_report, repository, case_id
):
    report = make_report()
    with pytest.raises(RuntimeFault) as error:
        verifier.create(
            namespace=ReceiptNamespace(arm="decider_memory", case_id=case_id),
            decision_id=uuid4(),
            model_call_id=uuid4(),
            context=context,
            report=report,
        )
    assert error.value.reason is Reason.RECEIPT_INVALID
    assert not list(repository.namespaces())


def test_security_repository_isolates_every_arm_and_case(
    repository, verifier, make_report, namespace, context
):
    report = make_report()
    written = []
    for arm in ("decider", "decider_memory"):
        for case in ("case-01", "case-02"):
            receipt = verifier.create(
                namespace=ReceiptNamespace(arm=arm, case_id=case),
                decision_id=uuid4(),
                model_call_id=uuid4(),
                context=context,
                report=report,
            )
            assert repository.put(receipt, verifier.token) is True
            written.append(receipt)
    assert len(set(receipt.receipt_id for receipt in written)) == 4
    for receipt in written:
        found = repository.find(receipt.namespace, receipt.problem_fingerprint)
        assert found == (receipt,)
    assert repository.count(namespace) == 1
    # A fresh namespace starts empty, which is the held-out starting condition.
    assert (
        repository.find(
            ReceiptNamespace(arm="decider_memory", case_id="case-99"),
            context.problem_fingerprint,
        )
        == ()
    )


def test_security_frozen_corpus_refuses_further_admission(
    repository, verifier, make_receipt, useful_report, namespace
):
    first = make_receipt(useful_report)
    assert repository.put(first, verifier.token) is True
    assert not repository.frozen
    repository.freeze()
    assert repository.frozen
    with pytest.raises(RuntimeFault) as error:
        repository.put(make_receipt(useful_report), verifier.token)
    assert error.value.reason is Reason.RECEIPT_INVALID
    # Frozen reads still work; only admission stops.
    assert repository.find(namespace, first.problem_fingerprint) == (first,)


def test_admission_invalidation_removes_a_receipt_from_retrieval(
    repository, verifier, make_receipt, stalled_report, namespace
):
    receipt = make_receipt(stalled_report)
    repository.put(receipt, verifier.token)
    assert repository.count(namespace) == 1
    with pytest.raises(RuntimeFault):
        repository.supersede(
            namespace, receipt.receipt_id, ReceiptWriteToken(authority=object())
        )
    assert repository.supersede(namespace, receipt.receipt_id, verifier.token) is True
    assert repository.find(namespace, receipt.problem_fingerprint) == ()
    assert repository.count(namespace) == 0
    assert repository.supersede(namespace, DIGEST, verifier.token) is False


def test_admission_refuses_a_malformed_or_foreign_row(repository, verifier, context):
    with pytest.raises(RuntimeFault):
        repository.put({"receipt_id": DIGEST}, verifier.token)
    payload = {
        "namespace": ReceiptNamespace(arm="decider_memory", case_id="case-01"),
        "decision_id": uuid4(),
        "model_call_id": uuid4(),
        "diagnostic_call_id": uuid4(),
        "problem_fingerprint": context.problem_fingerprint,
        "decision_context_hash": context.decision_context_hash,
        "selected_capability": "inspect_scope",
        "public_tool_id": "inspect_scope",
        "arguments_hash": DIGEST,
        "delta": EvidenceDelta(evidence_before_hash=DIGEST, evidence_after_hash=DIGEST),
        "outcome_class": "no_progress",
        "reason_code": Reason.INSUFFICIENT,
        "versions": context.versions,
    }
    coherent = DecisionReceipt.model_validate(
        payload
        | {
            "receipt_id": receipt_identity(payload),
            "observed_at": verifier.clock.utcnow(),
        }
    )
    assert repository.put(coherent, verifier.token) is True
    with pytest.raises(ValidationError):
        DecisionReceipt.model_validate(
            payload | {"receipt_id": DIGEST, "observed_at": verifier.clock.utcnow()}
        )
    assert isinstance(LocalReceiptRepository(), LocalReceiptRepository)
