"""Render the measured submission brief and evidence-gated outline sections."""

import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from publishing import PublicationFault, PublicationReason
from unstuck.domain import Contract, Identifier

#: REQ-PUB-04: the hard bound and the target, in seconds.
VIDEO_SECOND_BOUND = 180
VIDEO_SECOND_TARGET = 175
MEASURED_STORY = "Run 1 happens once: one inference, 21.6 s client time, unresolved, with the\nreceipt stored. Run 1 is not saved. The superseded post-D1 Run 2 made two\ninferences and took 54.8 s. Optimized Run 2 exact-matches the receipt before\nselection, precludes only the proven dead end, and lets Decider make one fresh\nchoice from the remaining tools. It completes with one inference in 26.6 s.\nThe observed Run 2 delta is 28.2 s, or 51.4% lower client latency: n = 1, the\nsame synthetic case, each proof once. The always-on SageMaker endpoint is\nbilled by uptime, not requests. One fewer inference is not a dollar saving."

#: REQ-PUB-05: the submission tags, verbatim.
SUBMISSION_TAGS: tuple[str, ...] = ("#strands-decider-2b", "#tool-selection")

#: Identifier tokens that legitimately carry a digit inside prose.
_IDENTIFIER_TOKEN = re.compile(
    r"\b(?:REQ-[A-Z]+-\d+|NFR\d+(?:\.\d+)*|FR\d+(?:\.\d+)*|US\d+(?:\.\d+)*"
    r"|AC\d+(?:\.\d+)*|PF\d+|OD-\d+|SC-\d+|D[12]|2B)\b"
)
#: Words that assert a ranking or a magnitude this project cannot support.
FORBIDDEN_SUPERLATIVES: tuple[str, ...] = (
    "best",
    "fastest",
    "cheapest",
    "most accurate",
    "state of the art",
    "unprecedented",
    "novel",
    "breakthrough",
    "dramatically",
    "significantly",
    "outperforms",
    "beats",
    "world class",
    "industry leading",
)
#: Phrasings that assert a finding rather than naming the slot for one.
FORBIDDEN_FINDING_PHRASES: tuple[str, ...] = (
    "we found",
    "results show",
    "demonstrates that",
    "proves",
    "achieves",
    "improved",
    "reduced",
    "increased",
    "measured at",
    "we measured",
    "turns out",
    "on average",
)
#: Magnitudes written as words, which a digit scan alone would not catch.
FORBIDDEN_MAGNITUDES: tuple[str, ...] = (
    "percent",
    "per cent",
    "half",
    "double",
    "doubled",
    "twice",
    "fold",
    "faster",
    "slower",
    "cheaper",
    "more accurate",
    "less accurate",
    "fell by",
    "fell",
    "rose by",
    "rose",
    "cut by",
    "orders of magnitude",
)


def scan_prose(text: str) -> tuple[str, ...]:
    """Return every claim marker in one prose string, most literal first."""
    found: list[str] = []
    lowered = text.lower()
    for phrase in FORBIDDEN_FINDING_PHRASES:
        # Bounded on both sides, so "improves" is not a hit for "proves".
        if re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", lowered):
            found.append(phrase)
    for word in FORBIDDEN_SUPERLATIVES + FORBIDDEN_MAGNITUDES:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            found.append(word)
    stripped = _IDENTIFIER_TOKEN.sub("", text)
    digits = sorted(set(re.findall(r"\d", stripped)))
    found.extend(f"digit {digit}" for digit in digits)
    return tuple(found)


def require_claim_free(text: str, where: str) -> None:
    """Refuse prose that carries a claim marker."""
    markers = scan_prose(text)
    if markers:
        raise PublicationFault(
            PublicationReason.OUTLINE_CLAIM_PRESENT,
            f"{where}: {', '.join(markers)}",
        )


class EvidenceSlot(Contract):
    """One thing a section needs before it can be written, and its gate."""

    slot_id: Identifier
    needs: Annotated[str, Field(min_length=1, max_length=512)]
    gate: Annotated[str, Field(min_length=1, max_length=256)]
    status: Literal["awaiting_evidence"] = "awaiting_evidence"

    @model_validator(mode="after")
    def claim_free(self) -> Self:
        require_claim_free(self.needs, f"slot {self.slot_id} needs")
        return self


class Section(Contract):
    """One outline section: what it is for, and what it waits for."""

    section_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=256)]
    purpose: Annotated[str, Field(min_length=1, max_length=1024)]
    slots: Annotated[tuple[EvidenceSlot, ...], Field(max_length=8)] = ()
    seconds: Annotated[int, Field(ge=0, le=VIDEO_SECOND_BOUND)] | None = None

    @model_validator(mode="after")
    def claim_free(self) -> Self:
        require_claim_free(self.title, f"section {self.section_id} title")
        require_claim_free(self.purpose, f"section {self.section_id} purpose")
        return self


class Outline(Contract):
    """An article or video outline. Never content, and never a finding."""

    outline_id: Identifier
    medium: Literal["article", "video"]
    working_title: Annotated[str, Field(min_length=1, max_length=256)]
    requirements: Annotated[tuple[str, ...], Field(min_length=1, max_length=8)]
    sections: Annotated[tuple[Section, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        require_claim_free(self.working_title, f"outline {self.outline_id} title")
        ids = [section.section_id for section in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("a section id appears twice")
        timed = [section for section in self.sections if section.seconds is not None]
        if self.medium == "video":
            if len(timed) != len(self.sections):
                raise ValueError("every video section carries its duration")
            if self.total_seconds > VIDEO_SECOND_BOUND:
                raise ValueError("the video outline exceeds its bound")
        elif timed:
            raise ValueError("an article section carries no duration")
        return self

    @property
    def total_seconds(self) -> int:
        return sum(
            section.seconds for section in self.sections if section.seconds is not None
        )

    @property
    def open_slots(self) -> tuple[EvidenceSlot, ...]:
        return tuple(slot for section in self.sections for slot in section.slots)

    def render(self) -> str:
        """Render the outline as Markdown. Still an outline, never content."""
        lines = [
            f"# {self.working_title}",
            "",
            f"Medium: {self.medium}.",
            "",
            "Status: outline only. The measured brief is recorded evidence.",
            "Each remaining slot names its evidence or pending publication step.",
            "",
            MEASURED_STORY,
            "",
        ]
        if self.medium == "video":
            lines.extend(
                [
                    f"Target: {VIDEO_SECOND_TARGET} seconds. "
                    f"Hard bound: {VIDEO_SECOND_BOUND} seconds. "
                    f"Outlined: {self.total_seconds} seconds.",
                    "",
                ]
            )
        for section in self.sections:
            heading = f"## {section.title}"
            if section.seconds is not None:
                heading = f"{heading} ({section.seconds} s)"
            lines.extend([heading, "", section.purpose, ""])
            if not section.slots:
                lines.extend(["Needs no evidence: it asserts nothing.", ""])
                continue
            for slot in section.slots:
                lines.append(f"- Evidence slot: {slot.needs}")
                lines.append(f"  Gate: {slot.gate}")
            lines.append("")
        return "\n".join(lines)


_BENCHMARK_GATE = (
    "Held-out benchmark findings, Pending measurement and deployment evidence."
)

#: The loop-escape slots are filled by the curated public record.
_LOOP_ESCAPE_GATE = (
    "The curated loop-escape record in dist/public-demo, generated from the two "
    "passing private proofs. Recorded and drawn on by docs/article.md; "
    "publication Pending the owner."
)

ARTICLE_OUTLINE = Outline(
    outline_id="article-decision-receipts",
    medium="article",
    working_title=(
        "The model kept picking the same useless tool, so I let it fail once and "
        "then refused the repeat"
    ),
    requirements=("REQ-PUB-03", "REQ-PUB-07"),
    sections=(
        Section(
            section_id="problem",
            title="The dead end a new call repeats",
            purpose=(
                "State the situation: a synthetic report returns zero rows, Decider "
                "2B picks a read that returns real data and settles nothing, and a "
                "new call about the same incident picks it again. Name the choice "
                "the project faced and why the same model selector was kept rather than "
                "replaced by fixed routing or a larger model, without comparing "
                "their outcomes."
            ),
        ),
        Section(
            section_id="causality",
            title="What the Decider is responsible for, and what it is not",
            purpose=(
                "Separate the two roles. Decider 2B chooses every diagnostic, "
                "including the one that escapes. Deterministic code decides what "
                "the diagnostic found, whether the evidence is admissible, and what "
                "outcome follows. Explain why that boundary is the whole design."
            ),
        ),
        Section(
            section_id="control",
            title="The memoryless control stays stuck",
            purpose=(
                "Show the calls without receipts. Each makes the same first choice, "
                "spends its only diagnostic read on the dead end, has no read left "
                "for a second choice, and ends unresolved."
            ),
            slots=(
                EvidenceSlot(
                    slot_id="control-calls",
                    needs=(
                        "The control calls from the curated record, each count "
                        "beside its denominator."
                    ),
                    gate=_LOOP_ESCAPE_GATE,
                ),
            ),
        ),
        Section(
            section_id="receipts",
            title="Run one pays the learning cost: the Decision Receipt, and why it is not a cache",
            purpose=(
                "Describe the receipt: identity from content rather than time, no "
                "expiry, no window, and the verifier as its only author. The model "
                "is unchanged; the system records a verified dead end. Explain "
                "that a cache answers for the model while a receipt tells it what "
                "already happened."
            ),
            slots=(
                EvidenceSlot(
                    slot_id="receipt-example",
                    needs=(
                        "The receipt run one stored, shown by tool and outcome "
                        "class from the curated record, with no identifier, digest "
                        "or version."
                    ),
                    gate=_LOOP_ESCAPE_GATE,
                ),
            ),
        ),
        Section(
            section_id="fresh-choice",
            title="Current state plus history, and a fresh choice every time",
            purpose=(
                "Walk the construction: build the authoritative current state, "
                "look up matching receipts, preclude only proven exact dead ends before "
                "selection, and require a fresh choice from remaining candidates. "
                "State plainly that a "
                "historical choice never replays and never scores a candidate."
            ),
        ),
        Section(
            section_id="blocking",
            title="Run two escapes: the one authority history carries is negative",
            purpose=(
                "Explain deterministic no-progress blocking: an exact-context "
                "receipt classed disposition_unchanged refuses the identical "
                "repetition before selection or any read. The same Decider makes one "
                "fresh choice from the remaining tools and spends the call's only "
                "read on that diagnostic. No D2 occurs in this proof. An optional "
                "D2 requires a remaining read. Note that a provider error never "
                "blocks."
            ),
            slots=(
                EvidenceSlot(
                    slot_id="escape-call",
                    needs=(
                        "Run two from the curated record: the matched receipt, the "
                        "preclusion before selection, the fresh choice, the repair, and "
                        "the verified outcome."
                    ),
                    gate=_LOOP_ESCAPE_GATE,
                ),
            ),
        ),
        Section(
            section_id="arms",
            title="The four arms the benchmark compares",
            purpose=(
                "Introduce the arms by construction only: fixed routing, a plain "
                "model selector, one-pass Decider, and Decider with receipt "
                "memory. Describe what each does and what question pairing them "
                "is meant to answer. Assert no comparison."
            ),
            slots=(
                EvidenceSlot(
                    slot_id="arm-comparison",
                    needs=(
                        "Held-out results for every arm, with the pairing design "
                        "and uncertainty the benchmark records."
                    ),
                    gate=_BENCHMARK_GATE,
                ),
            ),
        ),
        Section(
            section_id="findings",
            title="What the held-out benchmark must still establish",
            purpose=(
                "This held-out section cannot be written yet. The measured brief "
                "is separate single-case evidence, not a benchmark. This section "
                "stays empty until held-out evidence exists. The article states which arm was "
                "selected and distinguishes it from experimental evidence."
            ),
            slots=(
                EvidenceSlot(
                    slot_id="verified-success",
                    needs=(
                        "Verified success by arm on the held-out split, with the "
                        "interval the benchmark reports."
                    ),
                    gate=_BENCHMARK_GATE,
                ),
                EvidenceSlot(
                    slot_id="repeat-prevention",
                    needs=(
                        "On the frozen receipt-match subset: repeated mistakes "
                        "prevented, fresh-choice changes, re-decision corrections "
                        "and regressions, and false matches or blocks."
                    ),
                    gate=_BENCHMARK_GATE,
                ),
                EvidenceSlot(
                    slot_id="latency-cost",
                    needs=(
                        "Recorded latency and cost per arm. Any benefit is a "
                        "bonus claim and is made only when the evidence supports "
                        "it."
                    ),
                    gate=_BENCHMARK_GATE,
                ),
            ),
        ),
        Section(
            section_id="limitations",
            title="Limitations, and what this does not show",
            purpose=(
                "State the bounds honestly: one synthetic case, each proof run "
                "once, a controlled incident track, one task family, no model "
                "learning, no literal infinite loop, no automatic gain from "
                "memory, and no claim of a benefit that receipt history produces "
                "by itself. Name what would falsify the design."
            ),
        ),
        Section(
            section_id="prior-art",
            title="Prior art, and where this sits in it",
            purpose=(
                "Place the pattern against existing tool-selection, reflection "
                "and caching work, citing each source. Claim no novelty: the "
                "contribution described is the engineering boundary, not a new "
                "idea."
            ),
            slots=(
                EvidenceSlot(
                    slot_id="citations",
                    needs=(
                        "The cited prior-art sources, each read and recorded, so "
                        "the placement can be checked."
                    ),
                    gate=(
                        "A recorded literature review. Pending; no novelty claim "
                        "is made in the meantime."
                    ),
                ),
            ),
        ),
    ),
)

#: The video's slots are answered by the rendered video, which records the
#: loop-escape pair from the curated public record over the pitch frames.
_VIDEO_GATE = (
    "The rendered video recording the loop-escape pair from the curated public "
    "evidence record. Pending the owner's review and publication."
)

VIDEO_OUTLINE = Outline(
    outline_id="video-decision-receipts",
    medium="video",
    working_title="Run one pays once, run two uses the receipt before selection",
    requirements=("REQ-PUB-04", "REQ-PUB-07"),
    sections=(
        Section(
            section_id="frame",
            title="The dead end, in one breath",
            purpose=(
                "Open on the empty report and the elevator pitch. Decider 2B can "
                "repeat a decision already proven useless on a new call, and "
                "Unstuck turns that verified dead end into a Decision Receipt so "
                "the same Decider chooses a new path, with no larger model taking "
                "over."
            ),
            seconds=20,
        ),
        Section(
            section_id="control",
            title="The memoryless control stays stuck",
            purpose=(
                "Show the control calls without receipts. Each makes the same "
                "first choice, spends its only diagnostic read on the dead end, "
                "and ends unresolved."
            ),
            seconds=30,
            slots=(
                EvidenceSlot(
                    slot_id="control-capture",
                    needs=(
                        "A screen of the control calls: the first choice each "
                        "made, the read each spent, and the unresolved terminal "
                        "result each recorded."
                    ),
                    gate=_VIDEO_GATE,
                ),
            ),
        ),
        Section(
            section_id="scene-one",
            title="Scene one: run one pays the learning cost",
            purpose=(
                "Show the first call with "
                "receipts: Decider D1 making a fresh selection from the current "
                "candidates, the controller running the chosen diagnostic, the "
                "call ending unresolved, and the verifier writing a Decision "
                "Receipt for the dead end. The model is unchanged; the system "
                "records a verified fact."
            ),
            seconds=40,
            slots=(
                EvidenceSlot(
                    slot_id="scene-one-capture",
                    needs=(
                        "A screen of the first call with receipts: the empty "
                        "history, the selection it returned, the tool that ran, "
                        "the unresolved result, and the receipt the verifier "
                        "wrote."
                    ),
                    gate=_VIDEO_GATE,
                ),
            ),
        ),
        Section(
            section_id="scene-two",
            title="Scene two: run two escapes",
            purpose=(
                "Show a new call whose exact-context no-progress receipt, "
                "classed disposition_unchanged, precludes only the proven dead "
                "end before selection. Decider D1 makes a fresh choice from "
                "the remaining tools and its one read leads to the repair. "
                "No D2 occurs; post-D1 recovery is superseded history."
            ),
            seconds=50,
            slots=(
                EvidenceSlot(
                    slot_id="scene-two-capture",
                    needs=(
                        "A screen of the escape: the matched receipt, the "
                        "preclusion before selection, the fresh choice, the repair, "
                        "and the outcome the verifier recorded."
                    ),
                    gate=_VIDEO_GATE,
                ),
            ),
        ),
        Section(
            section_id="numbers",
            title="What was recorded",
            purpose=(
                "Show the curated evidence record with each count beside its "
                "denominator, and name the earlier two-read pair once as "
                "superseded history. Nothing is narrated that the record does "
                "not show. Keep the client-time comparison scoped to this case."
            ),
            seconds=20,
            slots=(
                EvidenceSlot(
                    slot_id="recorded-figures",
                    needs=(
                        "The curated record for the control and both runs, taken "
                        "from the record rather than restated."
                    ),
                    gate=_VIDEO_GATE,
                ),
            ),
        ),
        Section(
            section_id="close",
            title="The boundary, restated",
            purpose=(
                "Close on the one line that matters: Decider 2B makes every "
                "choice, the receipt only refuses an identical repeat, and code "
                "decides what a read found. Point at the repository and the "
                "recipe."
            ),
            seconds=15,
        ),
    ),
)

#: Both outlines, which is everything this unit produces for the two pieces.
OUTLINES: tuple[Outline, ...] = (ARTICLE_OUTLINE, VIDEO_OUTLINE)


__all__ = [
    "ARTICLE_OUTLINE",
    "FORBIDDEN_FINDING_PHRASES",
    "FORBIDDEN_SUPERLATIVES",
    "OUTLINES",
    "MEASURED_STORY",
    "SUBMISSION_TAGS",
    "VIDEO_OUTLINE",
    "VIDEO_SECOND_BOUND",
    "VIDEO_SECOND_TARGET",
    "EvidenceSlot",
    "Outline",
    "Section",
    "require_claim_free",
    "scan_prose",
]
