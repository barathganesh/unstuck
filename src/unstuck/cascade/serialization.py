"""Canonical bounded serialization for the offline Decision Receipt engine.

Every selector payload, receipt, and match is serialized by one canonical
writer so that identity never depends on key order, whitespace, or numeric
formatting. Size caps are refused rather than silently truncated by this
module; the projector owns explicit bounded truncation.
"""

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from unstuck.domain import Contract

SELECTOR_INPUT_MAX_CHARS = 8000
TOOL_SUMMARY_MAX_CHARS = 1000
MAX_RECEIPT_REFERENCES = 4
MAX_SELECTOR_CALLS = 2
MAX_DIAGNOSTIC_READS = 2


def canonical_text(value: object, max_chars: int = SELECTOR_INPUT_MAX_CHARS) -> str:
    """Serialize to the one canonical form and refuse anything over the cap."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as error:
        raise ValueError("canonical text requires finite numbers") from error
    if len(text) > max_chars:
        raise ValueError("canonical text exceeds reserved capacity")
    return text


def canonical_digest(value: object) -> str:
    """Hash the canonical form without applying a payload cap."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def bounded_text(
    value: str, max_chars: int = TOOL_SUMMARY_MAX_CHARS
) -> tuple[str, bool]:
    """Cap verbose display text and report whether anything was removed."""
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


class StageMeasurement(Contract):
    """Explicit telemetry availability. An absent measurement is never zero."""

    availability: Literal[
        "measured",
        "not_measured",
        "not_provided",
        "not_called",
        "invalid",
        "provider_error",
    ] = "not_measured"
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def explicit_absence(self) -> Self:
        if (self.availability == "measured") != (self.value is not None):
            raise ValueError("measurement availability does not match value")
        return self


NOT_CALLED = StageMeasurement(availability="not_called")
