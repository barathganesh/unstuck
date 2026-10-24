"""The public story and its curated evidence hold their shape. Offline.

The approved story is a causal sequence: Decider 2B repeats a verified dead
end, the verifier records it as a Decision Receipt, and on a later call the
controller refuses that one repeat so the same Decider chooses a new path. Run
1 stores a receipt, Run 2 uses it before selection, and the control stays stuck.

These tests fail when the opening of a public file drifts from that story, when
the earlier two-read story creeps back into an opening, when the curated record
stops showing the causal facts, when an identifier or hash reaches it, when a
number in it loses its denominator, or when any story file makes a claim the
evidence does not support. Each detector carries its own positive control, so a
detector that stops detecting fails rather than passing vacuously.
"""

import json
import re
from pathlib import Path

import pytest

from publishing.loop_escape_evidence import (
    AgentCoreProof,
    EvidenceRefused,
    Measurements,
    RealProof,
    build,
    load,
    render,
)
from publishing.public_scan import PUBLIC_FILES, scan_file, scan_text

ROOT = Path(__file__).resolve().parents[2]
CURATED = "dist/public-demo/loop-escape-proof.json"

PITCH = (
    "Decider 2B makes tool decisions, but a new call can repeat a decision "
    "already proven useless. Unstuck turns that verified dead end into a Decision "
    "Receipt, forcing the same Decider to choose a new path and escape the "
    "decision loop, without a larger model taking over."
)
SEQUENCE = (
    "Run 1 pays the learning cost once. Run 2 uses the receipt before selection. "
    "The memoryless control stays stuck."
)

#: Each opening ends at the first heading that leaves the lead story.
OPENINGS: dict[str, str] = {
    "README.md": "## How it works",
    "PRODUCT.md": "## Platform",
    "docs/article.md": "## How the story got here",
    "docs/hackathon-validation.md": "## The experiments before it",
}

#: Every file that tells the public story.
STORY_FILES: tuple[str, ...] = (
    "README.md",
    "PRODUCT.md",
    "docs/article.md",
    "docs/hackathon-validation.md",
    "docs/decision-receipt.md",
    "docs/agentcore-deployment.md",
    "docs/claim-register.md",
    "docs/submission-checklist.md",
    "docs/public-deliverables.md",
    "docs/article-outline.md",
    "docs/video-outline.md",
    "docs/video-script.md",
    "recipes/diagnostic-tool-selection/README.md",
    "AGENT.md",
    CURATED,
)

#: The earlier lead: the first call repairing on two reads, one fewer read later.
OLD_STORY = re.compile(
    r"one fewer (?:diagnostic )?read|fewer diagnostic read|\b2 reads\b"
    r"|two diagnostic reads|spent two|0\.3067|0\.4746|0\.2158|0\.6498"
    r"|(?:first call|call one|call 1|run 1)\b[^.|]{0,80}"
    r"(?:repaired|populated_after_repair|repair applie)",
    re.IGNORECASE,
)

#: A claim the evidence cannot carry. A sentence naming one must negate it.
FORBIDDEN_CLAIMS: dict[str, re.Pattern[str]] = {
    "biggest problem": re.compile(r"biggest problem", re.IGNORECASE),
    "infinite loop": re.compile(r"infinite loop", re.IGNORECASE),
    "model learning": re.compile(
        r"\b(?:the model|decider(?: 2b)?|the checkpoint)\s+(?:learn|learns|learned"
        r"|improves|improved)\b(?!\s+nothing)",
        re.IGNORECASE,
    ),
    "generic learning": re.compile(
        r"\b(?:learns|learning) (?:in general|over time"
        r"|from (?:its|every) mistake)",
        re.IGNORECASE,
    ),
    "accuracy": re.compile(r"\b(?:general |generic )?accuracy\b", re.IGNORECASE),
    "general latency benefit": re.compile(
        r"(?:general|guaranteed|workload-wide) latency (?:benefit|improvement|gain)",
        re.I,
    ),
    "cost benefit": re.compile(r"cost (?:benefit|saving|improvement|reduction)", re.I),
    "generalization": re.compile(r"\bgenerali[sz]", re.IGNORECASE),
    "significance": re.compile(r"statistical(?:ly)? significan", re.IGNORECASE),
    "novelty": re.compile(r"\bnovel(?:ty)?\b|\bfirst ever\b", re.IGNORECASE),
}
NEGATION = re.compile(
    r"\b(?:not|no|never|nor|nothing|none|without|cannot|forbidden|refused"
    r"|unclaimed|pending)\b|n't\b",
    re.IGNORECASE,
)


def text_of(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


#: A section whose job is to list the forbidden claims, in order to forbid them.
FORBIDDEN_LISTS: dict[str, str] = {
    "docs/claim-register.md": "## Forbidden, and asserted nowhere",
}


def claimable_text(relative_path: str) -> str:
    """The file's text without the one section that names forbidden claims."""
    text = text_of(relative_path)
    heading = FORBIDDEN_LISTS.get(relative_path)
    if heading is None:
        return text
    assert heading in text, f"{relative_path} lost its section {heading!r}"
    before, after = text.split(heading, 1)
    rest = after.split("\n## ", 1)
    return before + ("\n## " + rest[1] if len(rest) == 2 else "")


def flat(text: str) -> str:
    """Prose with emphasis and line breaks removed, for exact phrase checks."""
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def opening_of(relative_path: str) -> str:
    text = text_of(relative_path)
    marker = OPENINGS[relative_path]
    assert marker in text, f"{relative_path} lost its section {marker!r}"
    return text.split(marker, 1)[0]


def sentences(text: str) -> list[str]:
    """Sentences, table cells and list items, each judged on its own."""
    pieces = re.split(r"(?<=[.!?])\s+|\n\s*\n|\|", text)
    return [flat(piece) for piece in pieces if piece.strip()]


def unsupported_claims(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for sentence in sentences(text):
        for name, pattern in FORBIDDEN_CLAIMS.items():
            if pattern.search(sentence) and not NEGATION.search(sentence):
                found.append((name, sentence[:160]))
    return found


def _filled(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def bare_numbers(value: object, where: str = "$") -> list[str]:
    """Every number not held as a metric object beside what it was read against.

    A whole number may sit in ``{"value": n, "denominator": "..."}``. A measured
    quantity is a real number, so it may only sit in
    ``{"value": n, "unit": "...", "denominator": "..."}``: without a unit a
    fractional value says nothing about what was measured.
    """
    found: list[str] = []
    if isinstance(value, dict):
        has_denominator = _filled(value.get("denominator"))
        has_unit = _filled(value.get("unit"))
        for key, item in value.items():
            if key == "value" and has_denominator:
                if isinstance(item, bool) or not isinstance(item, int | float):
                    found.append(f"{where}.value is not a number")
                elif isinstance(item, float) and not has_unit:
                    found.append(f"{where}.value is a float without a unit")
                continue
            found.extend(bare_numbers(item, f"{where}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(bare_numbers(item, f"{where}[{index}]"))
    elif isinstance(value, int | float) and not isinstance(value, bool):
        found.append(where)
    return found


def metric_objects(value: object) -> list[dict[str, object]]:
    """Every object that states a measured quantity, by carrying a unit."""
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if "unit" in value:
            found.append(value)
        for item in value.values():
            found.extend(metric_objects(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(metric_objects(item))
    return found


@pytest.fixture(scope="module")
def curated() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(text_of(CURATED))
    return loaded


# ---------------------------------------------------------------------------
# The opening story


@pytest.mark.parametrize(
    "relative_path", ["README.md", "PRODUCT.md", "docs/article.md"]
)
def test_the_opening_carries_the_approved_pitch_verbatim(relative_path: str) -> None:
    if relative_path == "README.md":
        opening = flat(opening_of(relative_path))
        assert "Strands Decider 2B chooses from the remaining options" in opening
        assert "deterministic verifier stores" in opening
    else:
        assert PITCH in flat(opening_of(relative_path))


def test_the_readme_leads_with_the_problem_then_explains_the_sequence() -> None:
    head = flat(opening_of("README.md"))
    assert "synthetic order reports that return zero rows" in head
    assert head.index("synthetic order reports") < head.index("21.6")
    assert SEQUENCE in head


def test_product_states_the_causal_sequence() -> None:
    assert SEQUENCE in flat(opening_of("PRODUCT.md"))


@pytest.mark.parametrize("relative_path", sorted(OPENINGS))
def test_no_opening_carries_the_earlier_two_read_story(relative_path: str) -> None:
    opening = re.sub(
        r"at most two diagnostic reads per call",
        "the design read bound",
        opening_of(relative_path),
        flags=re.I,
    )
    hits = [match.group(0) for match in OLD_STORY.finditer(opening)]
    assert hits == [], f"{relative_path} opening: {hits}"


@pytest.mark.parametrize(
    "stale",
    [
        "Call 1 repaired on two reads.",
        "The same verified rows come back on one fewer diagnostic read.",
        "First reported confidence 0.3067.",
        "Run 1 spent two reads before it repaired.",
    ],
)
def test_the_old_story_detector_catches_its_own_example(stale: str) -> None:
    assert OLD_STORY.search(stale)


@pytest.mark.parametrize("relative_path", ["README.md", "docs/article.md"])
def test_the_opening_names_the_control_run_1_and_run_2(relative_path: str) -> None:
    opening = flat(opening_of(relative_path))
    for phrase in (
        "Run 1 pays the learning cost once",
        "Run 2 uses the receipt before selection",
        "control stays stuck",
        "3 of 3",
    ):
        assert phrase in opening, f"{relative_path}: {phrase}"


@pytest.mark.parametrize("relative_path", ["README.md", "docs/article.md"])
def test_the_earlier_pair_is_marked_superseded_not_erased(relative_path: str) -> None:
    text = flat(text_of(relative_path))
    assert "Superseded as the lead claim" in text
    assert "efficiency only" in text
    for verdict in ("UNVALIDATED", "FAIL"):
        assert verdict in text.upper()


# ---------------------------------------------------------------------------
# Claims the evidence cannot carry


@pytest.mark.parametrize("relative_path", STORY_FILES)
def test_no_story_file_makes_an_unsupported_claim(relative_path: str) -> None:
    assert unsupported_claims(claimable_text(relative_path)) == []


@pytest.mark.parametrize(
    "claim",
    [
        "Repeating a dead end is Decider 2B's biggest problem.",
        "Unstuck proves the agent escapes an infinite loop.",
        "Decider 2B learns from the receipt.",
        "The agent keeps learning over time.",
        "This raises the model's accuracy.",
        "The receipt gives a guaranteed latency benefit.",
        "It delivers a cost saving.",
        "The result generalizes to every workload.",
        "The improvement is statistically significant.",
        "This is a novel mechanism.",
    ],
)
def test_the_claim_detector_catches_its_own_example(claim: str) -> None:
    assert unsupported_claims(claim) != []


@pytest.mark.parametrize(
    "honest",
    [
        "No latency benefit is claimed.",
        "It is not an infinite loop, and no infinite loop was observed.",
        "The model learned nothing; the system recorded a verified fact.",
        "Nothing here generalizes to other cases.",
    ],
)
def test_the_claim_detector_passes_an_honest_negative(honest: str) -> None:
    assert unsupported_claims(honest) == []


# ---------------------------------------------------------------------------
# The curated evidence


def test_the_curated_record_is_in_the_public_scan_and_scans_clean() -> None:
    assert CURATED in PUBLIC_FILES
    assert scan_file(CURATED) == []


def test_the_curated_record_is_labelled_synthetic_and_n_of_one(
    curated: dict[str, object],
) -> None:
    assert curated["classification"] == "synthetic"
    scope = curated["scope"]
    assert isinstance(scope, dict)
    assert scope["cases"]["value"] == 1
    assert scope["runs_of_each_proof"]["value"] == 1
    assert scope["diagnostic_read_bound"]["value"] == 1
    assert "n = 1" in str(curated["what_this_is_not"])


def test_the_curated_record_is_the_second_schema(curated: dict[str, object]) -> None:
    assert curated["schema_version"] == "2"


def test_both_proofs_passed_every_check(curated: dict[str, object]) -> None:
    direct = curated["hosted_decider_proof"]
    assert isinstance(direct, dict)
    assert direct["verdict"] == "PASS"
    assert direct["checks_passed"] == {"value": 13, "denominator": "of 13 checks"}
    cloud = curated["agentcore_proof"]
    assert isinstance(cloud, dict)
    assert cloud["verdict"] == "PASS"
    assert cloud["checks_passed"] == {"value": 25, "denominator": "of 25 checks"}


def test_the_control_repeats_one_dead_end_three_times_and_stays_unresolved(
    curated: dict[str, object],
) -> None:
    proof = curated["hosted_decider_proof"]
    assert isinstance(proof, dict)
    assert proof["hosted_decider_choices"]["value"] == 5
    control = proof["without_receipts_control"]
    assert control["independent_calls"]["value"] == 3
    assert control["same_first_choice"]["value"] == 3
    assert "inspect_pipeline" in control["same_first_choice"]["denominator"]
    assert control["unresolved"]["value"] == 3
    calls = control["calls"]
    assert len(calls) == 3
    for call in calls:
        assert call["decider_choice"] == "inspect_pipeline"
        assert call["re_decision_d2"] is None
        assert call["dead_end_precluded"] is False
        assert call["options_offered"]["value"] == 4
        assert call["decider_choices"]["value"] == 1
        assert call["diagnostic_reads"]["value"] == 1
        assert call["terminal_status"] == "unresolved"
        assert call["terminal_reason"] == "diagnostic_reads_exhausted"


def test_run_1_pays_the_learning_cost_once(curated: dict[str, object]) -> None:
    run_1 = curated["hosted_decider_proof"]["with_receipts"][0]  # type: ignore[index]
    assert run_1["call"] == "Run 1"
    assert run_1["decision_history"] == "empty"
    assert run_1["dead_end_precluded"] is False
    assert run_1["options_offered"]["value"] == 4
    assert run_1["decider_choice"] == "inspect_pipeline"
    assert run_1["re_decision_d2"] is None
    assert run_1["decider_choices"]["value"] == 1
    assert run_1["diagnostic_reads"]["value"] == 1
    assert run_1["terminal_status"] == "unresolved"
    assert run_1["terminal_reason"] == "diagnostic_reads_exhausted"
    assert run_1["receipts_stored"] == [
        {"tool": "inspect_pipeline", "outcome_class": "disposition_unchanged"}
    ]
    cloud = curated["agentcore_proof"]
    cloud_1 = cloud["calls"][0]  # type: ignore[index]
    assert cloud_1["terminal_status"] == "unresolved"
    assert cloud_1["second_decider_call"] is False
    assert cloud["narration_rule"]["run_1_narration_not_permitted"] is True  # type: ignore[index]


def test_run_2_precludes_the_dead_end_before_selection_then_repairs(
    curated: dict[str, object],
) -> None:
    proof = curated["hosted_decider_proof"]
    run_2 = proof["with_receipts"][1]  # type: ignore[index]
    assert run_2["call"] == "Run 2"
    assert run_2["decision_history"] == "matched"
    assert run_2["receipt_match"] == "exact"
    assert run_2["dead_end_precluded"] is True
    assert run_2["precluded_before_selection"] is True
    assert run_2["precluded_before_any_read"] is True
    assert run_2["options_offered"]["value"] == 3
    assert run_2["decider_choice"] == "inspect_schema_keys"
    assert run_2["re_decision_d2"] is None
    assert run_2["decider_choices"]["value"] == 1
    assert run_2["executed"] == ["inspect_schema_keys"]
    assert run_2["diagnostic_reads"]["value"] == 1
    assert run_2["repair_applied"] is True
    assert run_2["verified"] is True
    assert (run_2["terminal_status"], run_2["terminal_reason"]) == (
        "completed",
        "populated_after_repair",
    )
    linkage = proof["linkage"]  # type: ignore[index]
    assert all(linkage.values())
    assert linkage["run_2_preclusion_precedes_first_decider_call"] is True
    assert linkage["run_2_decider_choice_differs_from_precluded_dead_end"] is True
    assert proof["false_preclusions"]["value"] == 0  # type: ignore[index]

    cloud = curated["agentcore_proof"]
    cloud_2 = cloud["calls"][1]  # type: ignore[index]
    assert cloud_2["dead_end_precluded"] is True
    assert cloud_2["preclusion_precedes_first_decider_call"] is True
    assert cloud_2["preclusion_precedes_first_read"] is True
    assert cloud_2["decider_choice"] == "inspect_schema_keys"
    assert cloud_2["final_choice"] == "inspect_schema_keys"
    assert cloud_2["second_decider_call"] is False
    assert cloud_2["terminal_status"] == "completed"
    assert cloud_2["terminal_reason"] == "populated_after_repair"
    assert cloud_2["narration"] == "narrated"
    assert cloud["false_preclusions"]["value"] == 0  # type: ignore[index]


def test_no_call_in_the_record_spends_a_second_decider_inference(
    curated: dict[str, object],
) -> None:
    proof = curated["hosted_decider_proof"]
    direct = [
        *proof["without_receipts_control"]["calls"],  # type: ignore[index]
        *proof["with_receipts"],  # type: ignore[index]
    ]
    for call in direct:
        assert call["re_decision_d2"] is None, call["call"]
        assert call["decider_choices"]["value"] == 1, call["call"]
    for call in curated["agentcore_proof"]["calls"]:  # type: ignore[index]
        assert call["second_decider_call"] is False, call["call"]


def test_every_timeline_finalized_with_nothing_dropped_or_refused(
    curated: dict[str, object],
) -> None:
    timelines = curated["agentcore_proof"]["telemetry_timelines"]  # type: ignore[index]
    assert [timeline["events"]["value"] for timeline in timelines] == [15, 21]
    for timeline in timelines:
        assert timeline["finalized"] is True
        assert timeline["expected"]["value"] == timeline["events"]["value"]
        assert timeline["dropped"]["value"] == 0
        assert timeline["refused"]["value"] == 0


# ---------------------------------------------------------------------------
# The one latency comparison, and the limits on it


def test_the_split_latency_section_carries_the_measured_values(
    curated: dict[str, object],
) -> None:
    section = curated["measured_latency"]
    assert isinstance(section, dict)
    client = section["agentcore_client_time"]
    assert client["run_1_learning"]["elapsed"]["value"] == 21635.7
    assert client["run_1_learning"]["decider_inferences"]["value"] == 1
    assert client["superseded_run_2_post_d1"]["elapsed"]["value"] == 54795.9
    assert client["superseded_run_2_post_d1"]["decider_inferences"]["value"] == 2
    assert client["optimized_run_2_preselection"]["elapsed"]["value"] == 26622.5
    assert client["optimized_run_2_preselection"]["decider_inferences"]["value"] == 1
    assert client["difference"]["absolute"]["value"] == 28173.4
    assert client["difference"]["relative"]["value"] == 51.4
    assert client["difference"]["decider_inferences"]["value"] == 1

    selector = section["agentcore_selector_time"]
    assert selector["superseded_run_2_post_d1"]["elapsed"]["value"] == 52263.79
    assert selector["optimized_run_2_preselection"]["elapsed"]["value"] == 24781.65
    assert selector["difference"]["absolute"]["value"] == 27482.14
    assert selector["difference"]["relative"]["value"] == 52.6

    direct = section["hosted_selector_time"]
    assert direct["run_1_learning"]["elapsed"]["value"] == 20651.01
    assert direct["superseded_run_2_post_d1"]["elapsed"]["value"] == 53957.49
    assert direct["superseded_run_2_post_d1"]["decider_inferences"]["value"] == 2
    assert direct["optimized_run_2_preselection"]["elapsed"]["value"] == 25229.29
    assert direct["optimized_run_2_preselection"]["decider_inferences"]["value"] == 1
    assert direct["difference"]["absolute"]["value"] == 28728.2
    assert direct["difference"]["relative"]["value"] == 53.2


def test_every_metric_states_its_unit_and_its_basis(
    curated: dict[str, object],
) -> None:
    metrics = metric_objects(curated)
    assert len(metrics) >= 20
    for metric in metrics:
        assert _filled(metric.get("unit")), metric
        denominator = metric.get("denominator")
        assert _filled(denominator), metric
        assert isinstance(denominator, str)
        assert "n = 1" in denominator, metric
        assert "each proof run once" in denominator, metric
        assert "separate earlier run" in denominator, metric


def test_run_1_is_never_counted_as_a_saving(curated: dict[str, object]) -> None:
    section = curated["measured_latency"]
    assert isinstance(section, dict)
    statement = flat(str(section["run_1_is_not_saved"]))
    assert "pays the learning cost once" in statement
    assert "never presented as a saving" in statement
    assert "unchanged" in statement


def test_no_dollar_saving_is_claimed(curated: dict[str, object]) -> None:
    section = curated["measured_latency"]
    assert isinstance(section, dict)
    statement = flat(str(section["no_dollar_saving"]))
    assert "No dollar or cost saving is measured" in statement
    assert "billed by instance uptime" in statement
    assert "one fewer inference" in statement
    assert "n = 1" in statement
    assert "dollar" in str(curated["not_claimed"])


def test_the_earlier_evidence_keeps_the_superseded_post_d1_proof(
    curated: dict[str, object],
) -> None:
    earlier = flat(str(curated["earlier_evidence"]))
    assert "superseded as the lead claim" in earlier
    assert "repeated the proven dead end" in earlier
    assert "before any read" in earlier
    assert "second Decider call chose inspect_schema_keys" in earlier
    assert "two inferences" in earlier
    assert "efficiency only" in earlier


def test_every_number_in_the_curated_record_has_its_denominator(
    curated: dict[str, object],
) -> None:
    assert bare_numbers(curated) == []


def test_the_denominator_check_catches_a_bare_number() -> None:
    assert bare_numbers({"reads": 1}) == ["$.reads"]
    assert bare_numbers({"ms": 1.5}) == ["$.ms"]
    assert bare_numbers({"reads": {"value": 1, "denominator": ""}}) != []
    assert bare_numbers({"reads": {"value": 1, "denominator": "of 1"}}) == []
    assert bare_numbers({"ms": {"value": 1.5, "denominator": "of 1"}}) != []
    assert bare_numbers({"ms": {"value": 1.5, "unit": " ", "denominator": "n"}}) != []
    assert bare_numbers({"ms": {"value": 1.5, "unit": "ms", "denominator": "n"}}) == []


def keys_of(value: object) -> list[str]:
    if isinstance(value, dict):
        return [*value, *(key for item in value.values() for key in keys_of(item))]
    if isinstance(value, list):
        return [key for item in value for key in keys_of(item)]
    return []


#: A key name that would carry private identity. A latency-named key is allowed
#: now that the record states one measured comparison; these never are.
IDENTITY_KEY_PARTS: tuple[str, ...] = (
    "_id",
    "hash",
    "digest",
    "revision",
    "version",
    "session",
    "request",
    "timestamp",
    "confidence",
    "raw",
)


def identity_keys(value: object) -> list[str]:
    return [
        key
        for key in keys_of(value)
        if key != "schema_version" and any(part in key for part in IDENTITY_KEY_PARTS)
    ]


def test_the_curated_record_carries_no_identifier_or_hash(
    curated: dict[str, object],
) -> None:
    text = text_of(CURATED)
    assert scan_text(CURATED, text) == []
    assert not re.search(r"[0-9a-f]{16,}", text)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
    for token in ("arn:", "case-", "/Users/", ".venv", "sha256"):
        assert token not in text, token
    assert identity_keys(curated) == []


@pytest.mark.parametrize(
    "key",
    [
        "decision_id",
        "model_revision",
        "budgets_hash",
        "row_digest",
        "runtime_version",
        "session_key",
        "request_id",
        "started_timestamp",
        "confidence",
        "raw_output",
    ],
)
def test_the_identity_key_check_catches_its_own_example(key: str) -> None:
    assert identity_keys({"calls": [{key: "anything"}]}) == [key]


def test_a_latency_named_key_is_allowed(curated: dict[str, object]) -> None:
    assert "measured_latency" in keys_of(curated)
    assert identity_keys({"measured_latency": {"elapsed_latency_ms": 1}}) == []


# ---------------------------------------------------------------------------
# The generator


def _measured_run(
    *,
    choices: list[str],
    inferences: int,
    selector_sum: float,
    client: float | None = None,
    offers: list[int] | None = None,
    done: bool,
) -> dict[str, object]:
    record: dict[str, object] = {
        "choices": choices,
        "inferences": inferences,
        "reads": 1,
        "reason": "populated_after_repair" if done else "diagnostic_reads_exhausted",
        "selector_ms": [selector_sum],
        "selector_ms_sum": selector_sum,
        "terminal": "completed" if done else "unresolved",
        "note": "placeholder-never-published",
    }
    if client is not None:
        record["client_ms"] = client
    if offers is not None:
        record["offers"] = offers
    return record


def _metric_run(
    *, client: float, selector_sum: float, inferences: int, done: bool
) -> dict[str, object]:
    return {
        "client_latency_ms": client,
        "diagnostic_reads": 1,
        "reason": "populated_after_repair" if done else "diagnostic_reads_exhausted",
        "selector_inferences": inferences,
        "selector_latency_ms_sum": selector_sum,
        "terminal": "completed" if done else "unresolved",
        "note": "placeholder-never-published",
    }


#: Synthetic stand-ins for the three private records, in the pre-selection
#: shape. Every value is a placeholder; none is a measured value from any run.
RUN_1_SELECTOR = 900.0
RUN_2_SELECTOR = 1100.0
OLD_RUN_2_SELECTOR = 2200.0
RUN_1_CLIENT = 1000.0
RUN_2_CLIENT = 1200.0
OLD_RUN_2_CLIENT = 2400.0


def _real_record(terminal_reason: str = "populated_after_repair") -> dict[str, object]:
    def call(
        *,
        history: str,
        match: str,
        choice: str,
        d2: str | None,
        precluded: bool,
        done: bool,
    ) -> dict[str, object]:
        return {
            "history_status": history,
            "receipt_match": match,
            "d1_choice": choice,
            "d1_decision_id": "placeholder-never-published",
            "d2_choice": d2,
            "blocked_tool_id": "inspect_pipeline" if precluded else None,
            "blocked_receipt_id": "placeholder-never-published",
            "blocked_before_first_read": True if precluded else None,
            "blocked_before_selection": True if precluded else None,
            "executed_tools": ["inspect_schema_keys" if done else "inspect_pipeline"],
            "diagnostic_reads": 1,
            "selector_calls": 2 if d2 else 1,
            "repair_applied": done,
            "verified_success": done,
            "terminal_status": "completed" if done else "unresolved",
            "terminal_reason": terminal_reason
            if done
            else "diagnostic_reads_exhausted",
            "receipts": [
                {
                    "public_tool_id": "inspect_schema_keys"
                    if done
                    else "inspect_pipeline",
                    "outcome_class": "useful_evidence"
                    if done
                    else "disposition_unchanged",
                    "stored": True,
                    "receipt_id": "placeholder-never-published",
                }
            ],
        }

    def observation(history: str, options: int, latency: float) -> dict[str, object]:
        return {
            "history_status": history,
            "option_ids": ["a", "b", "c", "d"][:options],
            "validation_status": "valid",
            "measured_latency_ms": latency,
            "decision_id": "placeholder-never-published",
            "confidence": 0.5,
        }

    control = call(
        history="not_requested",
        match="not_requested",
        choice="inspect_pipeline",
        d2=None,
        precluded=False,
        done=False,
    )
    return {
        "fixture": "synthetic",
        "status": "complete",
        "verdict": "PASS",
        "max_diagnostic_reads": 1,
        "model_revision": "placeholder-never-published",
        "control_calls": [control, control, control],
        "memory_calls": [
            call(
                history="empty",
                match="no_match",
                choice="inspect_pipeline",
                d2=None,
                precluded=False,
                done=False,
            ),
            call(
                history="matched",
                match="exact",
                choice="inspect_schema_keys",
                d2=None,
                precluded=True,
                done=True,
            ),
        ],
        "observations": [
            observation("not_requested", 4, RUN_1_SELECTOR),
            observation("not_requested", 4, RUN_1_SELECTOR),
            observation("not_requested", 4, RUN_1_SELECTOR),
            observation("empty", 4, RUN_1_SELECTOR),
            observation("matched", 3, RUN_2_SELECTOR),
        ],
        "linkage": {
            "call_1_receipt_cites_call_1_d1_decision": True,
            "call_2_block_cites_call_1_receipt": True,
            "call_2_block_precedes_any_read": True,
            "call_2_block_precedes_selector_request": True,
            "call_2_d1_differs_from_blocked_choice": True,
        },
        "checks": [{"name": "synthetic", "passed": True}],
    }


def _agentcore_record() -> dict[str, object]:
    def cloud_call(*, precluded: bool) -> dict[str, object]:
        kinds = ["run_started", "decider_first_requested", "probe_started"]
        if precluded:
            kinds.insert(1, "repeat_blocked")
            kinds.append("repair_applied")
        kinds.append("run_finished")
        return {
            "blocked": "inspect_pipeline" if precluded else None,
            "client_latency_ms": RUN_2_CLIENT if precluded else RUN_1_CLIENT,
            "diagnostic_reads": 1,
            "event_kinds": kinds,
            "first_choice": "inspect_schema_keys" if precluded else "inspect_pipeline",
            "final_choice": "inspect_schema_keys" if precluded else "inspect_pipeline",
            "history": "matched" if precluded else "empty",
            "narration_status": "narrated" if precluded else "unavailable",
            "reason": "populated_after_repair"
            if precluded
            else "diagnostic_reads_exhausted",
            "repair_attempts": 1 if precluded else 0,
            "terminal": "completed" if precluded else "unresolved",
            "session_key": "placeholder-never-published",
        }

    def timeline(events: int) -> dict[str, object]:
        return {
            "emitted": events,
            "expected_event_count": events,
            "finalized": True,
            "dropped": 0,
            "refused": 0,
        }

    return {
        "classification": "synthetic",
        "visibility": "private",
        "verdict": "PASS",
        "model_revision_hash": ["placeholder-never-published"],
        "calls": {
            "call-1": cloud_call(precluded=False),
            "call-2": cloud_call(precluded=True),
        },
        "checks": {
            "call1_narration_not_permitted": True,
            "call2_history_matched_exact_receipt": True,
            "call2_narration_after_completion_only": True,
            "call2_no_d2": True,
            "call2_preclusion_before_decider_first_requested": True,
        },
        "metrics": {
            "basis": "placeholder-never-published",
            "run_1": _metric_run(
                client=RUN_1_CLIENT,
                selector_sum=RUN_1_SELECTOR,
                inferences=1,
                done=False,
            ),
            "run_2_optimized": _metric_run(
                client=RUN_2_CLIENT,
                selector_sum=RUN_2_SELECTOR,
                inferences=1,
                done=True,
            ),
            "old_v10_run_2_post_d1": _metric_run(
                client=OLD_RUN_2_CLIENT,
                selector_sum=OLD_RUN_2_SELECTOR,
                inferences=2,
                done=True,
            ),
            "run_2_delta_vs_old": {
                "client_latency": {"absolute_ms": 1200.0, "percent": 50.0},
                "inferences": 1,
                "selector_latency_sum": {"absolute_ms": 1100.0, "percent": 50.0},
            },
        },
        "telemetry_summaries": {"first": timeline(5), "second": timeline(7)},
        "leaks": [],
    }


def _measurements_record() -> dict[str, object]:
    return {
        "agentcore": {
            "run_1": _measured_run(
                choices=["inspect_pipeline"],
                inferences=1,
                selector_sum=RUN_1_SELECTOR,
                client=RUN_1_CLIENT,
                done=False,
            ),
            "old_run_2": _measured_run(
                choices=["inspect_pipeline", "inspect_schema_keys"],
                inferences=2,
                selector_sum=OLD_RUN_2_SELECTOR,
                client=OLD_RUN_2_CLIENT,
                done=True,
            ),
            "optimized_run_2": _measured_run(
                choices=["inspect_schema_keys"],
                inferences=1,
                selector_sum=RUN_2_SELECTOR,
                client=RUN_2_CLIENT,
                done=True,
            ),
            "run_2_delta": {
                "client": {"absolute_ms": 1200.0, "percent": 50.0},
                "inferences": 1,
                "selector": {"absolute_ms": 1100.0, "percent": 50.0},
            },
        },
        "hosted": {
            "run_1": _measured_run(
                choices=["inspect_pipeline"],
                inferences=1,
                selector_sum=RUN_1_SELECTOR,
                offers=[4],
                done=False,
            ),
            "old_run_2": _measured_run(
                choices=["inspect_pipeline", "inspect_schema_keys"],
                inferences=2,
                selector_sum=OLD_RUN_2_SELECTOR,
                offers=[4, 3],
                done=True,
            ),
            "optimized_run_2": _measured_run(
                choices=["inspect_schema_keys"],
                inferences=1,
                selector_sum=RUN_2_SELECTOR,
                offers=[3],
                done=True,
            ),
            "run_2_delta": {
                "inferences": 1,
                "selector": {"absolute_ms": 1100.0, "percent": 50.0},
            },
        },
        "verdicts": {"agentcore_pair": "PASS", "hosted_proof": "PASS"},
    }


def _synthetic_inputs(
    terminal_reason: str = "populated_after_repair",
) -> tuple[RealProof, AgentCoreProof, Measurements]:
    """The three private records as this generator reads them, in miniature."""
    return (
        RealProof.model_validate(_real_record(terminal_reason)),
        AgentCoreProof.model_validate(_agentcore_record()),
        Measurements.model_validate(_measurements_record()),
    )


def _old_shape_real() -> RealProof:
    """The superseded post-D1 hosted shape: D1 repeats, then a second call."""
    record = _real_record()
    memory = record["memory_calls"]
    assert isinstance(memory, list)
    memory[1] = {
        **memory[1],
        "d1_choice": "inspect_pipeline",
        "d2_choice": "inspect_schema_keys",
        "selector_calls": 2,
        "blocked_before_selection": None,
    }
    record["linkage"] = {
        "call_1_receipt_cites_call_1_d1_decision": True,
        "call_2_block_cites_call_1_receipt": True,
        "call_2_block_precedes_any_read": True,
        "call_2_d2_differs_from_blocked_choice": True,
    }
    return RealProof.model_validate(record)


def _old_shape_agentcore() -> AgentCoreProof:
    """The superseded post-D1 cloud shape: the block lands after selection."""
    record = _agentcore_record()
    calls = record["calls"]
    assert isinstance(calls, dict)
    calls["call-2"] = {
        **calls["call-2"],
        "first_choice": "inspect_pipeline",
        "event_kinds": [
            "run_started",
            "decider_first_requested",
            "decider_first_returned",
            "repeat_blocked",
            "decider_final_requested",
            "decider_final_returned",
            "probe_started",
            "repair_applied",
            "run_finished",
        ],
    }
    record["checks"] = {
        "call1_narration_not_permitted": True,
        "call2_history_matched_exact_receipt": True,
        "call2_narration_after_completion_only": True,
        "call2_d2_inspect_schema_keys_excludes_blocked": True,
    }
    return AgentCoreProof.model_validate(record)


def test_the_generator_drops_every_field_it_does_not_name() -> None:
    real, agentcore, measured = _synthetic_inputs()
    text = render(build(real, agentcore, measured))
    assert "placeholder-never-published" not in text
    assert bare_numbers(json.loads(text)) == []
    assert identity_keys(json.loads(text)) == []


def test_the_generator_refuses_a_proof_that_did_not_pass() -> None:
    real, agentcore, measured = _synthetic_inputs()
    with pytest.raises(EvidenceRefused):
        build(real.model_copy(update={"verdict": "FAIL"}), agentcore, measured)
    with pytest.raises(EvidenceRefused):
        build(real, agentcore.model_copy(update={"leaks": ("anything",)}), measured)
    with pytest.raises(EvidenceRefused):
        build(real, agentcore.model_copy(update={"verdict": "FAIL"}), measured)


def test_the_generator_refuses_to_render_a_leak() -> None:
    """A digest smuggled into a field the allowlist does read is still caught."""
    real, agentcore, measured = _synthetic_inputs(
        terminal_reason="deadbeefdeadbeefdeadbeef"
    )
    with pytest.raises(EvidenceRefused):
        render(build(real, agentcore, measured))


def test_the_generator_refuses_the_superseded_post_d1_hosted_shape() -> None:
    _, agentcore, measured = _synthetic_inputs()
    with pytest.raises(EvidenceRefused, match="second Decider choice"):
        build(_old_shape_real(), agentcore, measured)


def test_the_generator_refuses_the_superseded_post_d1_cloud_shape() -> None:
    real, _, measured = _synthetic_inputs()
    with pytest.raises(EvidenceRefused, match="before the first Decider request"):
        build(real, _old_shape_agentcore(), measured)


@pytest.mark.parametrize(
    "update",
    [
        {"selector_calls": 2},
        {"blocked_before_selection": None},
        {"blocked_before_selection": False},
        {"d2_choice": "inspect_scope"},
    ],
)
def test_the_generator_refuses_a_run_2_that_spent_a_second_choice(
    update: dict[str, object],
) -> None:
    real, agentcore, measured = _synthetic_inputs()
    run_1, run_2 = real.memory_calls
    broken = real.model_copy(
        update={"memory_calls": (run_1, run_2.model_copy(update=update))}
    )
    with pytest.raises(EvidenceRefused):
        build(broken, agentcore, measured)


@pytest.mark.parametrize(
    "flag",
    ["call_2_block_precedes_selector_request", "call_2_d1_differs_from_blocked_choice"],
)
def test_the_generator_refuses_an_unlinked_preclusion(flag: str) -> None:
    real, agentcore, measured = _synthetic_inputs()
    for value in (None, False):
        broken = real.model_copy(
            update={"linkage": real.linkage.model_copy(update={flag: value})}
        )
        with pytest.raises(EvidenceRefused):
            build(broken, agentcore, measured)


@pytest.mark.parametrize(
    "name", ["call2_no_d2", "call2_preclusion_before_decider_first_requested"]
)
def test_the_generator_refuses_a_cloud_proof_missing_its_preselection_check(
    name: str,
) -> None:
    real, agentcore, measured = _synthetic_inputs()
    dropped = {key: value for key, value in agentcore.checks.items() if key != name}
    with pytest.raises(EvidenceRefused, match=name):
        build(real, agentcore.model_copy(update={"checks": dropped}), measured)


def test_the_generator_refuses_a_measurement_that_disagrees() -> None:
    real, agentcore, _ = _synthetic_inputs()
    for path, value in (
        ("client_ms", RUN_1_CLIENT + 1.0),
        ("inferences", 2),
        ("selector_ms_sum", RUN_1_SELECTOR + 1.0),
    ):
        record = _measurements_record()
        record["agentcore"]["run_1"][path] = value  # type: ignore[index]
        with pytest.raises(EvidenceRefused):
            build(real, agentcore, Measurements.model_validate(record))

    record = _measurements_record()
    record["hosted"]["optimized_run_2"]["selector_ms_sum"] = 99.0  # type: ignore[index]
    with pytest.raises(EvidenceRefused):
        build(real, agentcore, Measurements.model_validate(record))

    record = _measurements_record()
    record["verdicts"]["hosted_proof"] = "FAIL"  # type: ignore[index]
    with pytest.raises(EvidenceRefused):
        build(real, agentcore, Measurements.model_validate(record))


def test_the_generator_refuses_a_delta_that_does_not_recompute() -> None:
    real, agentcore, _ = _synthetic_inputs()
    record = _measurements_record()
    record["agentcore"]["run_2_delta"]["client"]["percent"] = 90.0  # type: ignore[index]
    with pytest.raises(EvidenceRefused):
        build(real, agentcore, Measurements.model_validate(record))


def test_the_committed_record_matches_the_private_proofs_when_present() -> None:
    real_path = (
        ROOT / ".venv" / "loop-escape-real" / "preselection-20261022" / "proof.json"
    )
    cloud_path = ROOT / ".venv" / "preselection-real-cloud" / "proof.json"
    measured_path = ROOT / ".venv" / "preselection-real-cloud" / "measurements.json"
    if not all(path.is_file() for path in (real_path, cloud_path, measured_path)):
        pytest.skip("the private proof records are not on this machine")
    real, agentcore, measured = load(real_path, cloud_path, measured_path)
    assert render(build(real, agentcore, measured)) == text_of(CURATED)


# ---------------------------------------------------------------------------
# The video narration and the links


#: The speech rate measured on the last rendered narration, words per minute.
MEASURED_WORDS_PER_MINUTE = 148.7
VIDEO_SECOND_LIMIT = 180


def narration_by_scene() -> list[tuple[str, int, int]]:
    """``(scene, spoken words, stated hold seconds)`` for every scene."""
    script = text_of("docs/video-script.md")
    scenes: list[tuple[str, int, int]] = []
    for block in script.split("\n## Scene ")[1:]:
        body = block.split("\n## ", 1)[0]
        spoken = " ".join(
            line[1:].strip() for line in body.splitlines() if line.startswith(">")
        )
        words = len(re.sub(r"[`_/]", " ", spoken).split())
        held = re.search(r"(\d+) s held", body)
        assert held is not None, block.splitlines()[0]
        scenes.append((block.splitlines()[0], words, int(held.group(1))))
    return scenes


def test_the_narration_fits_the_limit_at_the_measured_rate() -> None:
    scenes = narration_by_scene()
    seconds = sum(
        -(-words * 60 // MEASURED_WORDS_PER_MINUTE) + held for _, words, held in scenes
    )
    assert seconds < VIDEO_SECOND_LIMIT
    plan = json.loads(text_of("submission/video/edit-plan.json"))
    assert plan["total_frames"] / plan["fps"] < VIDEO_SECOND_LIMIT


def test_the_first_scene_opens_with_the_incident():
    scene = (
        text_of("docs/video-script.md").split("## Scene 2.")[0].split("## Scene 1.")[1]
    )
    spoken = flat(
        " ".join(line[1:] for line in scene.splitlines() if line.startswith(">"))
    )
    for phrase in (
        "zero rows",
        "Without receipts",
        "three calls",
        "unresolved",
    ):
        assert phrase in spoken
    assert "recorded" in scene.lower()


def test_no_scene_of_the_video_script_tells_the_earlier_story() -> None:
    # The video is the eight-slide pitch spoken aloud; the superseded two-read
    # pair is history in the article and the README, and has no scene here.
    script = text_of("docs/video-script.md")
    assert len(narration_by_scene()) == 9
    assert OLD_STORY.findall(script) == []


@pytest.mark.parametrize("relative_path", [p for p in STORY_FILES if p.endswith(".md")])
def test_every_relative_link_resolves(relative_path: str) -> None:
    base = (ROOT / relative_path).parent
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text_of(relative_path)):
        if re.match(r"[a-z]+://", target):
            continue
        assert (base / target).exists(), f"{relative_path} -> {target}"


@pytest.mark.parametrize(
    "relative_path", [name for name in STORY_FILES if name.endswith(".md")]
)
def test_written_artifacts_carry_the_measured_scope(relative_path: str) -> None:
    text = flat(text_of(relative_path))
    for value in ("21.6", "54.8", "26.6", "28.2", "51.4%", "n = 1"):
        assert value in text, f"{relative_path}: missing {value}"
    assert "Run 1 is not saved" in text or "Run 1 pays the learning cost once" in text, relative_path
    assert "uptime" in text and "SageMaker" in text, relative_path
    assert scan_file(relative_path) == [], relative_path


def test_run_2_client_delta_recomputes_from_curated_times(
    curated: dict[str, object],
) -> None:
    section = curated["measured_latency"]
    assert isinstance(section, dict)
    client = section["agentcore_client_time"]
    old = client["superseded_run_2_post_d1"]["elapsed"]["value"]
    optimized = client["optimized_run_2_preselection"]["elapsed"]["value"]
    assert round((old - optimized) / 1000, 1) == 28.2
    assert round(100 * (old - optimized) / old, 1) == 51.4


@pytest.mark.parametrize(
    "relative_path", ["README.md", "PRODUCT.md", "docs/article.md"]
)
def test_judge_opening_explains_the_measured_comparison(relative_path: str) -> None:
    opening = (
        flat(opening_of(relative_path))
        if relative_path == "README.md"
        else " ".join(flat(text_of(relative_path)).split()[:115])
    )
    for phrase in ("before selection", "unresolved", "26.6", "54.8", "51.4%", "n = 1"):
        assert phrase in opening, f"{relative_path}: {phrase}"


def stale_current_sequence(text: str) -> bool:
    for paragraph in re.split(r"\n\s*\n", text):
        if re.search(r"superseded|historical", paragraph, re.I):
            continue
        if re.search(
            r"Run 2.{0,100}(?:first chose|first choice repeats|D1 repeats|chose.*again)"
            r".{0,100}(?:dead end|inspect_pipeline)",
            flat(paragraph),
            re.I,
        ):
            return True
    return False


@pytest.mark.parametrize("relative_path", sorted(OPENINGS))
def test_current_opening_never_describes_post_d1_recovery(relative_path: str) -> None:
    assert not stale_current_sequence(opening_of(relative_path))


def test_stale_sequence_detector_distinguishes_history() -> None:
    stale = "Run 2 first chose inspect_pipeline, the dead end, then used D2."
    assert stale_current_sequence(stale)
    assert not stale_current_sequence("Historical, superseded: " + stale)
    assert not stale_current_sequence(
        "Run 2 precludes the dead end before selection and makes one fresh choice."
    )


def editorial_flags(text: str) -> list[str]:
    patterns = {
        "em dash": r"\u2014",
        "self-scoring": r"\b(?:achieved|predicted|estimated)\s+(?:rubric\s+)?score\b",
        "promotion": r"\b(?:groundbreaking|seamless|unprecedented|fastest|best-in-class)\b",
        "dollar figure": r"(?:\$\s*\d|\b\d+(?:\.\d+)?\s*USD\b)",
    }
    return [
        name for name, pattern in patterns.items() if re.search(pattern, text, re.I)
    ]


@pytest.mark.parametrize(
    "relative_path", [name for name in STORY_FILES if name.endswith(".md")]
)
def test_written_editorial_audit(relative_path: str) -> None:
    assert editorial_flags(text_of(relative_path)) == []


@pytest.mark.parametrize(
    "text", ["A\u2014B", "Predicted score: 90", "A groundbreaking result", "$4 saved"]
)
def test_editorial_audit_positive_controls(text: str) -> None:
    assert editorial_flags(text)


def test_editorial_audit_accepts_scoped_latency_and_external_criteria() -> None:
    assert (
        editorial_flags(
            "Observed Run 2 client latency is 51.4% lower, n = 1, one synthetic case. "
            "Official judging weight: 30%. No self-scoring or dollar saving."
        )
        == []
    )
