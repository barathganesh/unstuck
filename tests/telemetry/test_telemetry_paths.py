"""Telemetry across the three real composed paths.

The direct path, the same-call second decision, and the cross-call refused
repetition are driven through the real controller, the real orchestrator, the
real receipt store, and the real DuckDB evidence engine. What is asserted is
that the telemetry timeline reconstructs the causal story of each one, in the
durable order the controller wrote it, with no decision or terminal behaviour
changed by telemetry being attached.

The Decider and the order of its answers are scripted offline, so no latency,
cost, or token figure in these tests is a measurement.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_paths.py -q
"""

from unstuck.cascade.offline import ScriptedChoice
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.telemetry import (
    CorrelationContext,
    InfrastructureDimensions,
    JsonlSink,
    MemorySink,
    TelemetryRecorder,
    public_findings,
    sanitize_timeline,
    span_for,
)

from .conftest import PIPELINE, SCHEMA, SCOPE, compose, pending_fixture


def ordered(kinds: list[str], expected: list[str]) -> None:
    positions = [kinds.index(name) for name in expected]
    assert positions == sorted(positions), list(zip(expected, positions, strict=True))


def test_contract_the_direct_path_timeline_reconstructs_one_verified_read(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        correlation=CorrelationContext(case_id="case-01", arm="decider_memory"),
    ) as composed:
        result = composed.execute()

    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    timeline = composed.timeline()
    # Every durable event is observed exactly once, in the durable order.
    assert list(timeline.kinds) == composed.types()
    assert [item.correlation.sequence for item in timeline.records] == [
        event.sequence for event in composed.events()
    ]
    assert composed.controller.telemetry.health.complete
    assert composed.controller.telemetry.health.emitted == len(composed.types())

    kinds = composed.kinds()
    ordered(
        kinds,
        [
            "run_started",
            "decision_history_requested",
            "decision_history_returned",
            "decider_first_requested",
            "decider_first_returned",
            "direct_path_selected",
            "probe_started",
            "probe_returned",
            "decision_receipt_written",
            "repair_applied",
            "run_finished",
        ],
    )
    assert "decider_final_requested" not in kinds

    opened = composed.record("run_started")
    assert opened.outcome is not None and opened.outcome.retry_state == "new"
    assert opened.correlation.case_id == "case-01"
    assert opened.correlation.arm == "decider_memory"

    history = composed.record("decision_history_returned")
    assert history.decision is not None
    assert history.decision.history_status == "empty"
    assert history.decision.history_receipt_ids == ()

    requested = composed.record("decider_first_requested")
    assert requested.decision is not None
    assert requested.decision.pass_index == 1
    assert SCHEMA in requested.decision.candidate_public_ids
    assert requested.decision.decision_context_hash is not None
    assert requested.decision.problem_fingerprint is not None
    assert requested.decision.remaining_selector_calls == 2
    assert requested.outcome is not None and requested.outcome.d2_triggered is False

    answered = composed.record("decider_first_returned")
    assert answered.decision is not None
    assert answered.decision.selected_public_tool_id == SCHEMA
    assert answered.decision.accepted is True
    assert answered.decision.confidence == 0.95
    assert answered.decision.model_revision == "scripted-v1"
    assert answered.decision.probabilities is not None
    assert answered.measurements.selector_latency_ms.availability == "measured"

    read = composed.record("probe_started")
    assert read.tool is not None
    assert read.tool.public_tool_id == SCHEMA
    assert read.tool.diagnostic_call_id is not None
    assert read.tool.execution_key is not None
    assert read.tool.read_index == 1

    written = composed.record("decision_receipt_written")
    assert written.receipt is not None and written.tool is not None
    assert written.receipt.outcome_class == "useful_evidence"
    assert written.receipt.blocking_eligible is False
    assert written.receipt.stored is True
    assert written.receipt.namespace_hash is not None
    assert written.receipt.versions_hash is not None
    assert written.tool.admitted_evidence is True
    assert written.tool.disposition_moved is True
    assert written.tool.execution_key == read.tool.execution_key

    finished = composed.record("run_finished")
    assert finished.outcome is not None
    assert finished.outcome.terminal_status == "completed"
    assert finished.outcome.terminal_reason == "populated_after_repair"
    assert finished.outcome.verification_status == "passed"
    assert finished.outcome.row_count == len(result["rows"])
    assert finished.outcome.row_digest is not None
    assert finished.outcome.counters is not None
    assert finished.outcome.counters.diagnostic_reads == 1
    assert finished.measurements.end_to_end_ms.availability == "measured"


def test_contract_the_same_call_second_decision_is_fully_reconstructable(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCOPE), ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        seed=(SCOPE,),
    ) as composed:
        result = composed.execute()

    assert result["status"] == "completed"
    assert result["pass_count"] == 2 and result["selection_pass"] == 2
    kinds = composed.kinds()
    assert list(composed.timeline().kinds) == composed.types()
    ordered(
        kinds,
        [
            "decider_first_requested",
            "decider_first_returned",
            "probe_started",
            "probe_returned",
            "decision_receipt_written",
            "decider_final_requested",
            "decider_final_returned",
        ],
    )
    assert kinds.count("probe_started") == 2
    assert kinds.count("decision_receipt_written") == 2
    # The second receipt is written after the second decision answered, so the
    # timeline really does carry two complete decide-then-read cycles.
    assert kinds.index("decider_final_returned") < len(kinds) - 1 - list(
        reversed(kinds)
    ).index("decision_receipt_written")

    first, final = (
        composed.records("decider_first_returned"),
        composed.records("decider_final_returned"),
    )
    assert len(first) == 1 and len(final) == 1
    assert first[0].decision is not None and final[0].decision is not None
    assert first[0].decision.selected_public_tool_id == SCOPE
    assert final[0].decision.selected_public_tool_id == SCHEMA
    assert first[0].decision.pass_index == 1 and final[0].decision.pass_index == 2
    # Both decisions are distinct model calls, which is what keeps the second
    # one a fresh choice in the record rather than a replay of the first.
    assert first[0].decision.decision_id != final[0].decision.decision_id

    opened = composed.record("decider_final_requested")
    assert opened.outcome is not None and opened.outcome.d2_triggered is True
    assert opened.decision is not None
    assert SCOPE not in opened.decision.candidate_public_ids

    receipts = composed.records("decision_receipt_written")
    assert len(receipts) == 2
    stalled, useful = receipts
    assert stalled.receipt is not None and stalled.tool is not None
    assert stalled.receipt.outcome_class == "no_progress"
    # Why the second decision opened: nothing was admitted and no gate moved.
    assert stalled.receipt.blocking_eligible is True
    assert stalled.tool.admitted_evidence is False
    assert stalled.tool.disposition_moved is False
    assert stalled.tool.evidence_before_hash == stalled.tool.evidence_after_hash
    assert useful.receipt is not None and useful.receipt.outcome_class == (
        "useful_evidence"
    )
    assert stalled.tool.execution_key != useful.tool.execution_key

    finished = composed.record("run_finished")
    assert finished.outcome is not None
    assert finished.outcome.d2_triggered is True
    assert finished.outcome.blocked_public_tool_id == SCOPE
    assert finished.outcome.blocked_reason_code == "insufficient_evidence"


def test_contract_a_cross_call_refused_repetition_is_recorded_with_its_receipt(
    tmp_path,
):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        receipts=receipts,
        seed=(SCOPE,),
    ) as first:
        cold = first.execute()
    assert cold["status"] == "unresolved"
    assert first.record("decision_history_returned").decision.history_status == "empty"

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
        seed=(SCOPE,),
    ) as second:
        result = second.execute()

    assert result["status"] == "completed"
    assert result["history_status"] == "matched"
    kinds = second.kinds()
    assert "repeat_blocked" in kinds
    assert kinds.count("probe_started") == 1
    # The repetition is precluded before the only selector request: one
    # decision, one read, no second decision.
    assert kinds.index("repeat_blocked") < kinds.index("decider_first_requested")
    assert kinds.count("decider_first_requested") == 1
    assert "decider_final_requested" not in kinds
    (blocked,) = [e for e in second.events() if e.type == "repeat_blocked"]
    assert blocked.payload["stage"] == "pre_selection"

    history = second.record("decision_history_returned")
    assert history.decision is not None
    assert history.decision.history_status == "matched"
    # The exact match is ranked first, which is the one that can refuse a
    # repetition; a compatible match behind it is labeled evidence only.
    assert history.decision.history_match_classes[0] == "exact"
    assert "exact" in history.decision.history_match_classes
    assert history.decision.history_receipt_ids
    assert history.receipt is not None
    assert history.receipt.outcome_class == "no_progress"
    assert history.receipt.match_class == "exact"

    refusal = second.record("repeat_blocked")
    assert refusal.outcome is not None and refusal.receipt is not None
    assert refusal.outcome.blocked_public_tool_id == SCOPE
    assert refusal.outcome.blocked_class == "exact_repeat"
    assert refusal.receipt.match_class == "exact"
    assert refusal.receipt.blocking_eligible is True
    assert refusal.receipt.receipt_id in history.decision.history_receipt_ids
    assert refusal.tool is not None and refusal.tool.execution_key is not None

    # The refused execution and the one that actually ran are distinguishable
    # by their opaque keys alone.
    executed = second.record("probe_started")
    assert executed.tool is not None
    assert executed.tool.public_tool_id == SCHEMA
    assert executed.tool.execution_key != refusal.tool.execution_key


def test_the_wait_path_records_its_wait_and_its_verification(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=PIPELINE)],
        key="call-1",
        fixture=pending_fixture(),
    ) as composed:
        result = composed.execute()
    assert result["reason_code"] == "populated_after_wait"
    kinds = composed.kinds()
    ordered(kinds, ["probe_returned", "wait_started", "wait_finished"])
    assert composed.record("wait_started").outcome.wait_started is True
    assert composed.record("wait_finished").outcome.wait_finished is True
    # The report is verified once before the incident opens and once after the
    # partition arrives, so the wait path's own verification is the last one.
    assert len(composed.records("verification_finished")) == 2
    assert kinds.index("wait_finished") < max(
        index for index, kind in enumerate(kinds) if kind == "verification_finished"
    )
    verified = composed.records("verification_finished")[-1]
    assert verified.outcome is not None
    assert verified.outcome.verification_status == "passed"
    assert verified.outcome.verification_id is not None


def test_an_unresolved_run_records_why_no_read_resolved_it(tmp_path):
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        seed=(SCOPE, PIPELINE),
    ) as composed:
        result = composed.execute()
    assert result["status"] == "unresolved"
    kinds = composed.kinds()
    assert kinds.count("decision_receipt_written") == 2
    assert all(
        record.receipt is not None and record.receipt.outcome_class == "no_progress"
        for record in composed.records("decision_receipt_written")
    )
    # Two reads, two decisions, and no third selection anywhere in the record.
    assert kinds.count("decider_first_requested") == 1
    assert kinds.count("decider_final_requested") == 1
    finished = composed.record("run_finished")
    assert finished.outcome is not None
    assert finished.outcome.terminal_status == "unresolved"
    assert finished.outcome.terminal_reason == "insufficient_evidence"
    assert finished.outcome.row_count == 0
    assert finished.outcome.counters is not None
    assert finished.outcome.counters.decider_calls == 2
    assert finished.outcome.counters.diagnostic_reads == 2


def test_security_no_private_capability_or_truth_marker_reaches_telemetry(tmp_path):
    receipts = LocalReceiptRepository()
    with compose(
        tmp_path,
        choices=[
            ScriptedChoice(selected_id=SCOPE),
            ScriptedChoice(selected_id=PIPELINE),
        ],
        key="call-1",
        receipts=receipts,
        seed=(SCOPE,),
    ) as first:
        first.execute()
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-2",
        receipts=receipts,
        seed=(SCOPE,),
        infrastructure=InfrastructureDimensions(
            source="runtime_configuration",
            account_id="123456789012",
            region="us-east-1",
            principal_arn="arn:aws:iam::123456789012:role/unstuck",
        ),
    ) as second:
        second.execute()

    private = second.timeline()
    rendered = private.model_dump_json().lower()
    assert "selected_capability" not in rendered
    for term in (
        "jev",
        "bma",
        "jarvis",
        "hobson",
        "claude",
        "assistant",
        "expected_rows",
        "expected_action",
        "evaluator",
        "authorization",
        "country_code",
    ):
        assert term not in rendered

    # The private timeline retains the hosting identity it was configured with.
    assert "123456789012" in private.model_dump_json()

    public = sanitize_timeline(private)
    exported = public.model_dump_json()
    assert public_findings(public) == ()
    for forbidden in ("123456789012", "arn:aws:iam", "us-east-1"):
        assert forbidden not in exported
    # The public export still names the public tools and the receipt outcomes.
    assert SCHEMA in exported and SCOPE in exported
    assert "no_progress" in exported and "useful_evidence" in exported


def test_a_private_file_sink_captures_the_whole_run_for_later_reuse(tmp_path):
    path = tmp_path / "evidence" / "telemetry.jsonl"
    path.parent.mkdir()
    sink = JsonlSink(path)
    with compose(
        tmp_path / "runs",
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        sink=sink,  # type: ignore[arg-type]
    ) as composed:
        result = composed.execute()
    assert result["status"] == "completed"
    stored = sink.read()
    assert list(stored.kinds) == composed.types()
    assert stored.run_id == composed.run_id
    assert sink.written == len(composed.types())
    # The same file is enough to describe every record as an exportable span.
    spans = [span_for(record) for record in stored.records]
    assert [span.name for span in spans] == list(stored.kinds)
    assert all(span.scope == "unstuck.telemetry" for span in spans)


def test_telemetry_never_changes_the_run_it_observes(tmp_path):
    """The one property the whole leaf rests on: observation changes nothing."""

    def run(root, attach: bool) -> tuple[dict[str, object], list[str]]:
        with compose(
            root,
            choices=[
                ScriptedChoice(selected_id=SCOPE),
                ScriptedChoice(selected_id=SCHEMA),
            ],
            key="call-1",
            seed=(SCOPE,),
            sink=MemorySink(),
            attach=attach,
        ) as composed:
            return composed.execute(), composed.types()

    observed, observed_types = run(tmp_path / "observed", True)
    unobserved, unobserved_types = run(tmp_path / "unobserved", False)
    assert observed_types == unobserved_types
    # Minted identities differ between any two runs, so what is compared is
    # every decided and verified value: the outcome, the choices, the budgets,
    # the receipts, and the verification.
    minted = {
        "run_id",
        "evidence_ids",
        "report_evidence_id",
        "verification_id",
        "duration_ms",
    }
    assert {key: observed[key] for key in observed if key not in minted} == {
        key: unobserved[key] for key in unobserved if key not in minted
    }
    assert observed["status"] == unobserved["status"] == "completed"
    assert observed["first_choice"] == unobserved["first_choice"] == SCOPE
    assert observed["final_choice"] == unobserved["final_choice"] == SCHEMA


def test_a_failing_sink_leaves_the_verified_result_untouched(tmp_path):
    class Unwritable:
        def emit(self, record) -> None:
            raise OSError("scripted sink failure")

    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        sink=Unwritable(),  # type: ignore[arg-type]
    ) as composed:
        result = composed.execute()
    assert result["status"] == "completed"
    assert result["reason_code"] == "populated_after_repair"
    health = composed.controller.telemetry.health
    assert health.emitted == 0
    assert health.dropped == len(composed.types())
    assert not health.complete


def test_the_recorder_on_the_controller_is_inactive_without_a_sink(tmp_path):
    with compose(
        tmp_path,
        choices=[ScriptedChoice(selected_id=SCHEMA)],
        key="call-1",
        sink=None,
    ) as composed:
        composed.controller.telemetry = TelemetryRecorder()
        result = composed.execute()
    assert result["status"] == "completed"
    assert not composed.controller.telemetry.active
    assert composed.controller.telemetry.health.emitted == 0
