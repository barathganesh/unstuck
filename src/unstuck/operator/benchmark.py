"""Read-only benchmark view. Computes no statistic of its own.

The four arms are `fixed`, `llm`, `decider`, and `decider_memory`
(`docs/spec/design.md` section 17). The retention decision this view presents
is about `decider_memory`, not about any earlier supplement design.

Every number here is read from a validated evaluation export produced by the
evaluation unit. This module sums nothing, averages nothing, and ranks nothing:
if a figure is absent it stays absent, a figure whose coverage is partial is
labelled non-quotable rather than shown as a value, and a chart is never shown
without the counts it was drawn from. With no export, the view reports an empty
corpus and disables the retention panel, which is the honest state.

Three separate properties are required before this view will call a corpus
complete, because the defect it answers reported `complete` over three arms:

* the export names exactly the four arms, once each;
* every arm records both ordered calls of every case pair, with its episode
  identities reconciled against the frozen expectation;
* every measurement class this view displays reports complete coverage.

None of those properties is read off a flag. The export's declared identity
hashes, identity counts, pair totals, per-arm call counts, rate denominators
and coverage counts are reconciled against each other here, and completeness,
retention and quotability are derived from that reconciliation. A declaration
that cannot describe any set of records at all is refused outright; a
declaration that is coherent but does not reconcile is reported as a named gap,
which blocks completeness and therefore blocks every figure and the retention
panel.

`experiment_complete` and `retention_decidable` are carried separately from
`corpus_status`, so "the experiment finished" and "the retention rule can be
evaluated" can never be read off one flag.

The product package never imports the evaluation package. The reader below is a
narrow consumer schema over the declared `results.json` export: it names the
fields this view displays, refuses anything missing or mis-shaped, and ignores
the rest.
"""

import json
import os
import stat
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from unstuck.domain import Contract, Digest, Identifier
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.contracts import DisplayValue

Arm = Literal["fixed", "llm", "decider", "decider_memory"]

#: The two ordered calls every case pair is recorded over. The pair arithmetic
#: this consumer reconciles is defined for exactly two, so an export declaring
#: another number is refused rather than reconciled against the wrong total.
ORDERED_CALLS_PER_CASE = 2

ARMS: tuple[Arm, ...] = ("fixed", "llm", "decider", "decider_memory")
#: Retention needs these arms measured before the rule can be evaluated at all.
#: It is a subset of the four, so it is never sufficient for completeness.
RETENTION_ARMS: tuple[str, ...] = ("llm", "decider", "decider_memory")
RETENTION_RULE = (
    "Keep decider_memory only if verified success or prevented repeated "
    "mistakes improve over decider with no net regression, and it stays faster "
    "or cheaper than llm at comparable verified success."
)
#: The one export file a chart may be drawn from.
EXPORT_FILENAME = "results.json"
#: The aggregate figure classes this view displays, by export field name.
DISPLAYED_AGGREGATES: tuple[str, ...] = (
    "reads_per_success",
    "selector_latency_p50",
    "selector_latency_p95",
    "selector_cost",
)


class RateView(Contract):
    """One rate as the evaluation unit reported it: a count over a total."""

    count: Annotated[int, Field(ge=0, le=10**6)]
    denominator: Annotated[int, Field(ge=0, le=10**6)]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.count > self.denominator:
            raise ValueError("a count cannot exceed its denominator")
        return self

    @property
    def display(self) -> DisplayValue:
        """A rate with no denominator carries no percentage at all."""
        if self.denominator == 0:
            return DisplayValue.for_availability("not_measured")
        return DisplayValue.measured(100.0 * self.count / self.denominator)


class CoverageView(Contract):
    """How much of an aggregate was measured, reconciled to its total.

    The counts are the authority and the labels are checked against them. An
    export whose `measured` and `unavailable` do not add up to its own total,
    or whose declared completeness or quotability disagrees with its own
    counts, describes no set of records and is refused rather than displayed.
    """

    total: Annotated[int, Field(ge=0, le=10**6)]
    measured: Annotated[int, Field(ge=0, le=10**6)]
    unavailable: Annotated[int, Field(ge=0, le=10**6)]
    completeness: Literal["complete", "partial", "none"]
    quotable: bool

    @model_validator(mode="after")
    def reconciled(self) -> Self:
        if self.measured + self.unavailable != self.total:
            raise ValueError("coverage does not reconcile with its own total")
        if self.completeness != self.derived_completeness:
            raise ValueError("declared completeness disagrees with the counts")
        if self.quotable != (self.derived_completeness == "complete"):
            raise ValueError("declared quotability disagrees with the counts")
        return self

    @property
    def derived_completeness(self) -> Literal["complete", "partial", "none"]:
        """Completeness computed from the counts, never read off the label."""
        if not self.measured:
            return "none"
        return "complete" if self.measured == self.total else "partial"

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.measured == self.total


class FigureView(Contract):
    """One aggregate with its coverage. Partial is labelled, never shown."""

    value: Annotated[float, Field(allow_inf_nan=False)] | None = None
    availability: Literal["measured", "unavailable"]
    coverage: CoverageView

    @model_validator(mode="after")
    def explicit(self) -> Self:
        if (self.availability == "measured") != (self.value is not None):
            raise ValueError("availability does not match the recorded value")
        return self

    @property
    def quotable(self) -> bool:
        """Derived: a measured value over complete coverage, and nothing less."""
        return self.availability == "measured" and self.coverage.complete

    @property
    def display(self) -> DisplayValue:
        """Only a complete, measured figure reaches the view as a number."""
        if self.value is None or not self.coverage.complete:
            return DisplayValue.for_availability("not_measured")
        return DisplayValue.measured(self.value)


class ArmFigures(Contract):
    """The figures this view displays for one arm, with their denominators."""

    arm: Arm
    episodes: Annotated[int, Field(ge=0, le=10**6)]
    call_1_episodes: Annotated[int, Field(ge=0, le=10**6)]
    call_2_episodes: Annotated[int, Field(ge=0, le=10**6)]
    verified_completion: RateView
    harmed: RateView
    aggregates: dict[str, FigureView]

    @model_validator(mode="after")
    def reconciled(self) -> Self:
        if self.call_1_episodes + self.call_2_episodes != self.episodes:
            raise ValueError("call counts do not reconcile with the episode total")
        missing = [name for name in DISPLAYED_AGGREGATES if name not in self.aggregates]
        if missing:
            raise ValueError(f"the arm reports no {', '.join(missing)}")
        for name in DISPLAYED_AGGREGATES:
            if self.aggregates[name].coverage.total != self.episodes:
                raise ValueError(
                    f"{name} coverage is counted over {self.aggregates[name].coverage.total} "
                    f"of this arm's {self.episodes} episodes"
                )
        return self

    @property
    def measured(self) -> bool:
        """Measured means every displayed rate ran over every episode.

        A rate counted over one episode of forty is not this arm's rate, so a
        denominator short of the arm's own episode total is not measurement.
        """
        return (
            self.episodes > 0
            and self.verified_completion.denominator == self.episodes
            and self.harmed.denominator == self.episodes
        )

    def ordered_pairs(self, expected_pairs: int) -> bool:
        """Both ordered calls of every expected case pair, and no other count.

        Equal call counts are not enough: they must be the pair total the
        export's own reconciled identities account for, so an arm cannot record
        one pair twice and read as complete.
        """
        return (
            expected_pairs > 0
            and self.call_1_episodes == expected_pairs
            and self.call_2_episodes == expected_pairs
        )

    @property
    def fully_covered(self) -> bool:
        return all(
            self.aggregates[name].coverage.complete for name in DISPLAYED_AGGREGATES
        )


class IdentityReconciliation(Contract):
    """The export's declared episode identities, as counts and hashes.

    Every field is a number or a hash this consumer can check against the
    others. A combination no set of records could have produced is refused in
    the validator; a combination that is possible but does not reconcile is
    named by `gaps`.
    """

    expected_hash: Digest
    observed_hash: Digest
    expected: Annotated[int, Field(ge=0, le=10**6)]
    observed: Annotated[int, Field(ge=0, le=10**6)]
    unique_observed: Annotated[int, Field(ge=0, le=10**6)]
    missing: Annotated[int, Field(ge=0, le=10**6)] = 0
    unexpected: Annotated[int, Field(ge=0, le=10**6)] = 0
    duplicates: Annotated[int, Field(ge=0, le=10**6)] = 0
    unordered_pairs: Annotated[int, Field(ge=0, le=10**6)] = 0
    ordered_pairs: Annotated[int, Field(ge=0, le=10**6)] = 0

    @model_validator(mode="after")
    def possible(self) -> Self:
        if self.unique_observed > self.observed:
            raise ValueError("more unique identities than records")
        if self.missing > self.expected:
            raise ValueError("more missing identities than expected ones")
        if self.duplicates > self.unique_observed:
            raise ValueError("more duplicated identities than identities")
        if self.unique_observed != self.expected - self.missing + self.unexpected:
            raise ValueError("the unique identities do not reconcile with the sets")
        if bool(self.duplicates) != (self.observed > self.unique_observed):
            raise ValueError("duplicate records do not reconcile with the identities")
        if (self.expected_hash == self.observed_hash) != (
            not self.missing and not self.unexpected
        ):
            raise ValueError("the identity hashes disagree with the discrepancies")
        if self.ordered_pairs * ORDERED_CALLS_PER_CASE > self.observed:
            raise ValueError("more ordered pairs than records to form them")
        return self

    @property
    def gaps(self) -> tuple[str, ...]:
        """Every identity field that does not reconcile with the others."""
        found: list[str] = []
        if not self.expected:
            found.append("the export expects no episode identity at all")
        if self.expected_hash != self.observed_hash:
            found.append("the expected and observed identity hashes disagree")
        if self.expected != self.unique_observed:
            found.append(
                f"{self.unique_observed} unique identities against "
                f"{self.expected} expected"
            )
        if self.observed != self.unique_observed:
            found.append(f"{self.observed - self.unique_observed} duplicate records")
        if self.missing or self.unexpected or self.duplicates or self.unordered_pairs:
            found.append("episode identities are not reconciled")
        return tuple(found)

    @property
    def reconciled(self) -> bool:
        return not self.gaps


class EvaluationExport(Contract):
    """The narrow consumer view of one evaluation export."""

    experiment_id: Identifier
    arms: Annotated[tuple[ArmFigures, ...], Field(min_length=1, max_length=4)]
    experiment_complete: bool
    retention_decidable: bool
    quotable: bool
    identities: IdentityReconciliation
    episodes_expected: Annotated[int, Field(ge=0, le=10**6)]
    episodes_recorded: Annotated[int, Field(ge=0, le=10**6)]
    complete_pairs: Annotated[int, Field(ge=0, le=10**6)]
    incomplete_pairs: Annotated[int, Field(ge=0, le=10**6)]
    gaps: Annotated[tuple[str, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def distinct(self) -> Self:
        names = [arm.arm for arm in self.arms]
        if len(set(names)) != len(names):
            raise ValueError("duplicate benchmark arm")
        if self.complete_pairs * ORDERED_CALLS_PER_CASE < self.episodes_recorded:
            raise ValueError("more recorded episodes than the pairs could produce")
        return self

    @property
    def missing_arms(self) -> tuple[str, ...]:
        present = {arm.arm for arm in self.arms}
        return tuple(name for name in ARMS if name not in present)

    @property
    def pairs_per_arm(self) -> int:
        """The case pairs each arm must have recorded both calls of.

        It is derived from the export's own declared pair total and the arms it
        carries, so a per-arm call count is reconciled against the corpus
        rather than against itself.
        """
        if not self.arms or self.complete_pairs % len(self.arms):
            return 0
        return self.complete_pairs // len(self.arms)

    def completeness_gaps(self) -> tuple[str, ...]:
        """Everything that stops this export from being a complete corpus."""
        found: list[str] = list(self.identities.gaps)
        if self.missing_arms:
            found.insert(0, f"missing arms: {', '.join(self.missing_arms)}")
        pairs = self.pairs_per_arm
        if not pairs:
            found.append(
                f"{self.complete_pairs} complete pairs do not divide across "
                f"{len(self.arms)} arm(s)"
            )
        for arm in self.arms:
            if not arm.measured:
                found.append(f"{arm.arm} reports no measured episodes")
            if not arm.ordered_pairs(pairs):
                found.append(f"{arm.arm} did not record both ordered calls")
            if not arm.fully_covered:
                found.append(f"{arm.arm} reports partial measurement coverage")
            if arm.verified_completion.denominator != arm.episodes:
                found.append(
                    f"{arm.arm} reports verified completion over "
                    f"{arm.verified_completion.denominator} of {arm.episodes} episodes"
                )
            if arm.harmed.denominator != arm.episodes:
                found.append(
                    f"{arm.arm} reports harm over {arm.harmed.denominator} of "
                    f"{arm.episodes} episodes"
                )
        if self.identities.expected != self.episodes_expected:
            found.append(
                f"{self.identities.expected} expected identities against "
                f"{self.episodes_expected} expected episodes"
            )
        if self.identities.unique_observed != self.episodes_recorded:
            found.append(
                f"{self.identities.unique_observed} unique identities against "
                f"{self.episodes_recorded} episodes recorded"
            )
        if self.identities.ordered_pairs != self.complete_pairs:
            found.append(
                f"{self.identities.ordered_pairs} ordered pairs against "
                f"{self.complete_pairs} complete pairs"
            )
        if self.complete_pairs * ORDERED_CALLS_PER_CASE != self.episodes_expected:
            found.append(
                f"{self.complete_pairs} complete pairs do not account for "
                f"{self.episodes_expected} expected episodes"
            )
        if self.episodes_recorded != self.episodes_expected:
            found.append(
                f"{self.episodes_recorded} episodes recorded against "
                f"{self.episodes_expected} expected"
            )
        if sum(arm.episodes for arm in self.arms) != self.episodes_recorded:
            found.append(
                f"the arms carry {sum(arm.episodes for arm in self.arms)} episodes "
                f"against {self.episodes_recorded} recorded"
            )
        if sum(arm.call_1_episodes for arm in self.arms) != self.complete_pairs:
            found.append(
                f"the arms record {sum(arm.call_1_episodes for arm in self.arms)} "
                f"first calls against {self.complete_pairs} complete pairs"
            )
        if self.incomplete_pairs:
            found.append(f"{self.incomplete_pairs} incomplete case pairs")
        if not self.experiment_complete:
            found.append("the export does not declare the experiment complete")
        return tuple(found) + self.gaps


class ArmRow(Contract):
    """One arm's figures, exactly as the evaluation unit reported them."""

    arm: Arm
    episodes: Annotated[int, Field(ge=0, le=100000)] = 0
    verified_success: DisplayValue
    harmful_choices: DisplayValue
    diagnostic_reads: DisplayValue
    selector_latency_p50_ms: DisplayValue
    selector_latency_p95_ms: DisplayValue
    cost_usd: DisplayValue
    call_1_episodes: Annotated[int, Field(ge=0, le=100000)] = 0
    call_2_episodes: Annotated[int, Field(ge=0, le=100000)] = 0
    quotable: bool = False

    @property
    def measured(self) -> bool:
        return self.episodes > 0 and self.verified_success.availability == "measured"


class BenchmarkView(Contract):
    """The benchmark tab. Counts always accompany any chart."""

    experiment_id: Identifier | None = None
    classification: Literal["synthetic"] = "synthetic"
    corpus_status: Literal["empty", "partial", "complete"]
    episodes_total: Annotated[int, Field(ge=0, le=1000000)] = 0
    arms: Annotated[tuple[ArmRow, ...], Field(max_length=4)] = ()
    experiment_complete: bool = False
    retention_decidable: bool = False
    quotable: bool = False
    retention_enabled: bool = False
    retention_decision: Literal["pending", "keep", "submit_one_pass"] = "pending"
    retention_rule: str = RETENTION_RULE
    gaps: Annotated[tuple[str, ...], Field(max_length=64)] = ()
    notice: Annotated[str, Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        names = [row.arm for row in self.arms]
        if len(set(names)) != len(names):
            raise ValueError("duplicate benchmark arm")
        if self.corpus_status == "complete" and (
            set(ARMS) != set(names) or self.gaps or not self.experiment_complete
        ):
            raise ValueError("a complete corpus covers four arms with no gap")
        if self.retention_enabled and self.corpus_status != "complete":
            raise ValueError("retention is enabled only on a complete corpus")
        if self.retention_enabled and not self.retention_decidable:
            raise ValueError("retention cannot be enabled while it is undecidable")
        if not self.retention_enabled and self.retention_decision != "pending":
            raise ValueError("an undecidable retention stays pending")
        if self.quotable and self.corpus_status != "complete":
            raise ValueError("an incomplete corpus is never quotable")
        if self.episodes_total != sum(row.episodes for row in self.arms):
            raise ValueError("the episode total must equal the arms it came from")
        return self

    def require_retention(self) -> None:
        """Refuse to present a retention decision the evidence cannot support."""
        if not self.retention_enabled:
            raise OperatorFault(
                OperatorReason.RETENTION_UNDECIDABLE,
                "; ".join(self.gaps) or "required arms or denominators are missing",
            )


def _absent() -> DisplayValue:
    return DisplayValue.for_availability("not_measured")


def empty_arm(arm: Arm) -> ArmRow:
    """An arm with no measurement. Nothing here reads as zero."""
    return ArmRow(
        arm=arm,
        episodes=0,
        verified_success=_absent(),
        harmful_choices=_absent(),
        diagnostic_reads=_absent(),
        selector_latency_p50_ms=_absent(),
        selector_latency_p95_ms=_absent(),
        cost_usd=_absent(),
    )


def empty_view() -> BenchmarkView:
    """The honest state before any evaluation export exists."""
    return BenchmarkView(
        corpus_status="empty",
        episodes_total=0,
        arms=tuple(empty_arm(arm) for arm in ARMS),
        retention_enabled=False,
        gaps=("no evaluation export has been produced",),
        notice=(
            "No evaluation corpus has been produced yet, so every figure is "
            "Not measured and the retention decision stays Pending. "
            "Synthetic, offline."
        ),
    )


def resolve_export_path(root: Path, relative: str) -> Path:
    """Resolve the export inside one canonical root, or refuse.

    An absolute path, a traversal, a link anywhere on the way, a directory, and
    anything whose canonical location escapes the root are refusals. The
    surface reads exactly one declared file from inside the repository.
    """
    if not relative or relative != relative.strip():
        raise OperatorFault(
            OperatorReason.EVALUATION_PATH_REJECTED, "empty or padded path"
        )
    declared = Path(relative)
    if declared.is_absolute() or declared.anchor:
        raise OperatorFault(
            OperatorReason.EVALUATION_PATH_REJECTED, f"{relative} is absolute"
        )
    if any(part in {"..", ""} for part in declared.parts):
        raise OperatorFault(
            OperatorReason.EVALUATION_PATH_REJECTED, f"{relative} traverses"
        )
    canonical_root = Path(os.path.realpath(root))
    candidate = canonical_root / declared
    canonical = Path(os.path.realpath(candidate))
    if canonical != candidate or not canonical.is_relative_to(canonical_root):
        raise OperatorFault(
            OperatorReason.EVALUATION_PATH_REJECTED,
            f"{relative} resolves outside the export root",
        )
    try:
        mode = os.lstat(canonical).st_mode
    except OSError as error:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"{relative} does not exist"
        ) from error
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise OperatorFault(
            OperatorReason.EVALUATION_PATH_REJECTED,
            f"{relative} is not a regular file",
        )
    return canonical


def _require(body: Any, key: str, kind: type) -> Any:
    if not isinstance(body, dict) or key not in body:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"the export has no {key}"
        )
    value = body[key]
    if kind is bool:
        shaped = isinstance(value, bool)
    elif kind is int:
        shaped = isinstance(value, int) and not isinstance(value, bool)
    else:
        shaped = isinstance(value, kind)
    if not shaped:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"{key} has the wrong shape"
        )
    return value


def _rate(body: Any, key: str) -> RateView:
    raw = _require(body, key, dict)
    try:
        return RateView(
            count=_require(raw, "count", int),
            denominator=_require(raw, "denominator", int),
        )
    except ValueError as error:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"{key}: {error}"
        ) from error


def _figure(body: Any, key: str) -> FigureView:
    raw = _require(body, key, dict)
    measure = _require(raw, "measure", dict)
    coverage = _require(raw, "coverage", dict)
    value = measure.get("value")
    if value is not None and not isinstance(value, (int, float)):
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"{key} carries no number"
        )
    try:
        return FigureView(
            value=float(value) if value is not None else None,
            availability=_require(measure, "availability", str),
            coverage=CoverageView(
                total=_require(coverage, "total", int),
                measured=_require(coverage, "measured", int),
                unavailable=_require(coverage, "unavailable", int),
                completeness=_require(coverage, "completeness", str),
                quotable=_require(coverage, "quotable", bool),
            ),
        )
    except ValueError as error:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"{key}: {error}"
        ) from error


def _identities(denominators: Any) -> IdentityReconciliation:
    """Read the declared episode identities, or refuse the declaration.

    The discrepancy lists are read as their lengths: this view never displays a
    key, and a count is what the reconciliation arithmetic needs. A list that
    names the same identity twice is refused, because its length would then
    overstate the discrepancy it describes.
    """
    raw = _require(denominators, "identities", dict)

    def names(key: str) -> int:
        value = _require(raw, key, list)
        rendered = [str(item) for item in value]
        if len(set(rendered)) != len(rendered):
            raise OperatorFault(
                OperatorReason.EVALUATION_EXPORT_INVALID,
                f"identities.{key} names the same identity twice",
            )
        return len(rendered)

    try:
        return IdentityReconciliation(
            expected_hash=_require(raw, "expected_hash", str),
            observed_hash=_require(raw, "observed_hash", str),
            expected=_require(raw, "expected", int),
            observed=_require(raw, "observed", int),
            unique_observed=_require(raw, "unique_observed", int),
            missing=names("missing"),
            unexpected=names("unexpected"),
            duplicates=names("duplicates"),
            unordered_pairs=names("unordered_pairs"),
            ordered_pairs=_require(raw, "ordered_pairs", int),
        )
    except ValueError as error:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"identities: {error}"
        ) from error


def read_export(body: Any) -> EvaluationExport:
    """Project one declared export onto this view's narrow schema.

    Only the fields this view displays are read, and every one of them must be
    present and well shaped. Nothing a caller supplies about completeness is
    believed: the arm set, the ordered call counts, the reconciliation, and the
    coverage are all re-checked here.
    """
    manifest = _require(body, "manifest", dict)
    denominators = _require(manifest, "denominators", dict)
    identities = _identities(denominators)
    calls = _require(denominators, "calls_per_case", int)
    if calls != ORDERED_CALLS_PER_CASE:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID,
            f"this view reconciles {ORDERED_CALLS_PER_CASE} ordered calls per case, "
            f"not {calls}",
        )
    arms: list[ArmFigures] = []
    for raw in _require(body, "arms", list):
        aggregates = {name: _figure(raw, name) for name in DISPLAYED_AGGREGATES}
        try:
            figures = ArmFigures(
                arm=_require(raw, "arm", str),
                episodes=_require(raw, "episodes", int),
                call_1_episodes=_require(raw, "call_1_episodes", int),
                call_2_episodes=_require(raw, "call_2_episodes", int),
                verified_completion=_rate(raw, "verified_completion"),
                harmed=_rate(raw, "harmed"),
                aggregates=aggregates,
            )
        except ValueError as error:
            raise OperatorFault(
                OperatorReason.EVALUATION_EXPORT_INVALID, str(error)
            ) from error
        arms.append(figures)
    if not arms:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, "the export reports no arm"
        )
    try:
        return EvaluationExport(
            experiment_id=_require(manifest, "experiment_id", str),
            arms=tuple(arms),
            experiment_complete=_require(body, "experiment_complete", bool),
            retention_decidable=_require(body, "retention_decidable", bool),
            quotable=_require(body, "quotable", bool),
            identities=identities,
            episodes_expected=_require(denominators, "episodes_expected", int),
            episodes_recorded=_require(denominators, "episodes_recorded", int),
            complete_pairs=_require(denominators, "complete_pairs", int),
            incomplete_pairs=_require(denominators, "incomplete_pairs", int),
            gaps=tuple(
                str(item)
                for item in list(body.get("completeness_gaps") or ())
                + list(body.get("measurement_gaps") or ())
            )[:64],
        )
    except ValueError as error:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, str(error)
        ) from error


def load_export(root: Path, relative: str = EXPORT_FILENAME) -> EvaluationExport:
    """Read and validate one repository-contained evaluation export."""
    path = resolve_export_path(root, relative)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise OperatorFault(
            OperatorReason.EVALUATION_EXPORT_INVALID, f"{relative} is not JSON"
        ) from error
    return read_export(body)


def _row(arm: ArmFigures, pairs: int) -> ArmRow:
    return ArmRow(
        arm=arm.arm,
        episodes=arm.episodes,
        verified_success=arm.verified_completion.display,
        harmful_choices=arm.harmed.display,
        diagnostic_reads=arm.aggregates["reads_per_success"].display,
        selector_latency_p50_ms=arm.aggregates["selector_latency_p50"].display,
        selector_latency_p95_ms=arm.aggregates["selector_latency_p95"].display,
        cost_usd=arm.aggregates["selector_cost"].display,
        call_1_episodes=arm.call_1_episodes,
        call_2_episodes=arm.call_2_episodes,
        quotable=arm.measured and arm.ordered_pairs(pairs) and arm.fully_covered,
    )


def benchmark_view(export: EvaluationExport | None = None) -> BenchmarkView:
    """Read a validated evaluation export into the view. Nothing is computed."""
    if export is None:
        return empty_view()
    rows = tuple(_row(arm, export.pairs_per_arm) for arm in export.arms)
    gaps = export.completeness_gaps()
    measured = {row.arm for row in rows if row.measured}
    complete = not gaps
    partial = bool(measured) and not complete
    decidable = export.retention_decidable and set(RETENTION_ARMS) <= measured
    notice = (
        "Every figure is read from the evaluation export; this view computes "
        "none of them. Charts are shown with their counts. Synthetic, offline."
    )
    if gaps:
        notice = (
            "This corpus is incomplete, so no figure here is quotable and the "
            f"retention decision stays Pending. Outstanding: {gaps[0]}. "
            "Synthetic, offline."
        )
    return BenchmarkView(
        experiment_id=export.experiment_id,
        corpus_status="complete" if complete else ("partial" if partial else "empty"),
        episodes_total=sum(row.episodes for row in rows),
        arms=rows,
        experiment_complete=export.experiment_complete and complete,
        retention_decidable=decidable,
        quotable=export.quotable and complete,
        retention_enabled=complete and decidable,
        retention_decision="pending",
        gaps=gaps[:64],
        notice=notice,
    )


__all__ = [
    "ARMS",
    "DISPLAYED_AGGREGATES",
    "EXPORT_FILENAME",
    "ORDERED_CALLS_PER_CASE",
    "RETENTION_ARMS",
    "RETENTION_RULE",
    "ArmFigures",
    "ArmRow",
    "BenchmarkView",
    "CoverageView",
    "EvaluationExport",
    "FigureView",
    "IdentityReconciliation",
    "RateView",
    "benchmark_view",
    "empty_arm",
    "empty_view",
    "load_export",
    "read_export",
    "resolve_export_path",
]
