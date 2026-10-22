"""Durable scenario identity for the local run authority.

The run store keys a run by its idempotency key alone, and every declared
scenario shares one immutable task and one dataset version, so the store's own
duplicate check cannot tell two scenarios apart. A key first used for one
scenario therefore satisfied that check when it was reused for another, and the
caller was handed the earlier scenario's run as though it were theirs.

This module closes that gap without touching the run store. It records, beside
the run authority and inside the same repository boundary, the scenario token
and the canonical fixture identity each run was created from. An exact
duplicate (the same key with the same scenario and the same fixture identity)
resumes the recorded run. The same key with a different scenario or a different
fixture identity fails closed with a typed mismatch, and nothing is returned.

The record is a file, not process state, so it survives a restart of the
process that wrote it. Every path is opened with no-follow semantics relative to
a verified directory descriptor, so a link planted inside the repository cannot
redirect a binding, and a replaced binding file is never followed outside.

Nothing here decides an outcome, selects a tool, or authorizes a repair.
"""

import json
import os
import stat
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import ValidationError, model_validator

from unstuck.domain import Contract, Digest, canonical_hash
from unstuck.fixtures import Fixture
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.contracts import SafeLabel
from unstuck.operator.scenarios import Scenario

#: The directory, inside the run repository root, the bindings are kept in.
BINDING_DIRECTORY = ".scenario-bindings"


def fixture_identity(fixture: Fixture) -> str:
    """The canonical identity of one built fixture, independent of its token."""
    return canonical_hash(fixture.model_dump(mode="json"))


class ScenarioBinding(Contract):
    """What one run was created from. Immutable once recorded."""

    run_id: UUID
    scenario: SafeLabel
    case_id: SafeLabel
    fixture_identity: Digest
    idempotency_hash: Digest

    @model_validator(mode="after")
    def bound(self) -> Self:
        if not self.scenario.strip() or not self.case_id.strip():
            raise ValueError("a binding names its scenario and its case")
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        """Everything a duplicate invocation must restate exactly."""
        return self.scenario, self.case_id, self.fixture_identity

    @classmethod
    def of(
        cls,
        run_id: UUID,
        scenario: Scenario,
        fixture: Fixture,
        idempotency_key: str,
    ) -> "ScenarioBinding":
        return cls(
            run_id=run_id,
            scenario=scenario.token,
            case_id=scenario.case_id,
            fixture_identity=fixture_identity(fixture),
            idempotency_hash=canonical_hash(idempotency_key),
        )


class ScenarioLedger:
    """The persisted scenario binding of every local run, by run identity."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        for parent in (self.root, *self.root.parents):
            if parent.is_symlink():
                raise OperatorFault(
                    OperatorReason.SCENARIO_BINDING_UNREADABLE,
                    "the run repository root resolves through a link",
                )
        self.directory = self.root / BINDING_DIRECTORY
        try:
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as error:
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE, str(self.directory)
            ) from error

    def _descriptor(self) -> int:
        if self.directory.is_symlink():
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE,
                "the binding directory is a link",
            )
        try:
            return os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE, str(self.directory)
            ) from error

    def read(self, run_id: UUID) -> ScenarioBinding | None:
        """The recorded binding of one run, or None when none was recorded."""
        name = f"{UUID(str(run_id))}.json"
        descriptor = self._descriptor()
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE, name
            ) from error
        finally:
            os.close(descriptor)
        try:
            with os.fdopen(handle, encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OperatorFault(
                        OperatorReason.SCENARIO_BINDING_UNREADABLE,
                        f"{name} is not a regular file",
                    )
                return ScenarioBinding.model_validate_json(stream.read())
        except (OSError, UnicodeError, ValidationError) as error:
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE, name
            ) from error

    def bind(self, binding: ScenarioBinding) -> ScenarioBinding:
        """Record this run's scenario, or refuse a different one for its key.

        A binding is written exactly once. An exact duplicate returns the
        recorded binding, so a repeated invocation resumes its own run. Any
        other difference is a typed mismatch: the caller asked for a scenario
        this run was not created from, and no run is returned for it.
        """
        name = f"{binding.run_id}.json"
        body = json.dumps(
            binding.model_dump(mode="json", exclude={"schema_version"}),
            indent=2,
            sort_keys=True,
        )
        descriptor = self._descriptor()
        try:
            handle = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
        except FileExistsError:
            recorded = self.read(binding.run_id)
            if recorded is None or recorded.identity != binding.identity:
                raise OperatorFault(
                    OperatorReason.SCENARIO_MISMATCH,
                    (
                        f"this idempotency key is already bound to scenario "
                        f"{recorded.scenario if recorded else 'unreadable'}, not "
                        f"{binding.scenario}"
                    ),
                ) from None
            return recorded
        except OSError as error:
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE, name
            ) from error
        finally:
            os.close(descriptor)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise OperatorFault(
                OperatorReason.SCENARIO_BINDING_UNREADABLE, name
            ) from error
        return binding


__all__ = [
    "BINDING_DIRECTORY",
    "ScenarioBinding",
    "ScenarioLedger",
    "fixture_identity",
]
