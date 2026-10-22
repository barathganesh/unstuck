"""Deployment-supplied telemetry configuration, written first. Offline.

Account and Region are the owner's Isengard deployment inputs. The defect this
file pins is the obvious one: a constant. A telemetry module that spells an
account, a Region, a runtime, an endpoint, a model, a log group, or an ARN in
its own source works on exactly one deployment and silently publishes someone's
topology on every other. So every one of those values is typed here and
supplied by validated deployment configuration, the contract ships no default
for any of them, and the absence of one keeps the live telemetry gate blocked
rather than filling the hole with a plausible guess.

The documented OpenTelemetry variables are projected rather than read. Nothing
here touches `os.environ`, builds a client, or loads a credential: the
projection is a typed map a deployment applies, and a variable that would carry
an access key, a signing secret, or a session credential is refused by the
contract rather than removed afterwards.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_runtime_config.py -q
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.telemetry.placeholders import (
    SYNTHETIC_ACCOUNT,
    SYNTHETIC_AGENT,
    SYNTHETIC_ENDPOINT,
    SYNTHETIC_LOG_GROUP,
    SYNTHETIC_LOG_STREAM,
    SYNTHETIC_MODEL,
    SYNTHETIC_MODEL_REVISION,
    SYNTHETIC_PARTITION,
    SYNTHETIC_REGION,
    SYNTHETIC_RUNTIME,
    SYNTHETIC_RUNTIME_ARN,
    SYNTHETIC_SERVICE,
    SYNTHETIC_STAGE,
)
from unstuck.cloud.telemetry import (
    REQUIRED_LIVE_FIELDS,
    TelemetryRuntimeConfig,
)
from unstuck.telemetry.runtime import (
    FORBIDDEN_VARIABLES,
    OTEL_VARIABLES,
    VARIABLE_TREATMENT,
    ConfigVariable,
    OtelConfigMap,
    scoped_digest,
)

#: The modules this leaf owns. Every one is scanned for a deployment constant.
OWNED_SOURCES = (
    Path("src/unstuck/cloud/telemetry.py"),
    Path("src/unstuck/telemetry/runtime.py"),
    Path("src/unstuck/telemetry/signals.py"),
    Path("src/unstuck/telemetry/bundle.py"),
    Path("publishing/telemetry.py"),
)

#: Shapes that would make a module work on one deployment only. The Region form
#: is the AWS one; the account form is any twelve consecutive digits.
ACCOUNT_FORM = re.compile(r"(?<!\d)\d{12}(?!\d)")
REGION_FORM = re.compile(r"\b[a-z]{2}(?:-[a-z]+)+-\d\b")
ARN_FORM = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:")
LOG_GROUP_FORM = re.compile(r"/aws/[a-z0-9-]+/")


def full() -> TelemetryRuntimeConfig:
    """Every deployment input supplied, so the live gate has nothing missing."""
    return TelemetryRuntimeConfig(
        account_id=SYNTHETIC_ACCOUNT,
        region=SYNTHETIC_REGION,
        partition=SYNTHETIC_PARTITION,
        agent_id=SYNTHETIC_AGENT,
        runtime_id=SYNTHETIC_RUNTIME,
        endpoint_name=SYNTHETIC_ENDPOINT,
        runtime_arn=SYNTHETIC_RUNTIME_ARN,
        model_id=SYNTHETIC_MODEL,
        model_revision=SYNTHETIC_MODEL_REVISION,
        log_group=SYNTHETIC_LOG_GROUP,
        log_stream=SYNTHETIC_LOG_STREAM,
        unified_telemetry=True,
        observability_enabled=True,
        transaction_search="enabled",
        service_name=SYNTHETIC_SERVICE,
        service_stage=SYNTHETIC_STAGE,
        classification="synthetic",
    )


def test_an_unconfigured_runtime_supplies_no_identity_at_all_offline() -> None:
    config = TelemetryRuntimeConfig()
    for name in REQUIRED_LIVE_FIELDS:
        assert getattr(config, name) is None
    assert config.missing_live_fields == REQUIRED_LIVE_FIELDS
    assert config.inputs_complete is False
    assert config.configured().dimensions.identity_bearing is False


def test_the_configured_dimensions_declare_their_source_offline() -> None:
    dimensions = full().configured().dimensions
    assert dimensions.source == "runtime_configuration"
    assert dimensions.account_id == SYNTHETIC_ACCOUNT
    assert dimensions.region == SYNTHETIC_REGION
    assert dimensions.log_group == SYNTHETIC_LOG_GROUP
    assert dimensions.identity_bearing is True


def test_a_complete_configuration_leaves_nothing_missing_offline() -> None:
    config = full()
    assert config.missing_live_fields == ()
    assert config.inputs_complete is True


def test_one_absent_deployment_input_blocks_the_live_gate_offline() -> None:
    for name in REQUIRED_LIVE_FIELDS:
        config = TelemetryRuntimeConfig.model_validate(
            full().model_dump() | {name: None}
        )
        assert config.missing_live_fields == (name,)
        assert config.inputs_complete is False


def test_complete_inputs_alone_are_not_live_readiness_offline() -> None:
    """Regression: readiness ignored enablement, classification and the gate.

    `full()` supplies every deployment input and is still not ready, because it
    is a synthetic offline configuration. Each remaining condition is then
    satisfied in turn, and only the complete set clears the gate.
    """
    configured = full().configured()
    assert configured.inputs_complete is True
    assert configured.live_ready is False
    assert full().live_evidence_unmet == (
        "live_classification",
        "live_evidence_gate",
        "observed_records",
    )

    disabled = TelemetryRuntimeConfig.model_validate(
        full().model_dump() | {"classification": "live", "observability_enabled": False}
    )
    assert disabled.configured().live_ready is False
    assert "observability_enabled" in disabled.live_evidence_unmet

    ungated = TelemetryRuntimeConfig.model_validate(
        full().model_dump() | {"classification": "live"}
    )
    assert ungated.configured().live_ready is False
    assert "live_evidence_gate" in ungated.live_evidence_unmet

    ready = TelemetryRuntimeConfig.model_validate(
        full().model_dump()
        | {"classification": "live", "live_evidence_gate": "established"}
    )
    assert ready.configured().live_ready is True
    # Observed records never exist offline, so the condition is always named.
    assert ready.live_evidence_unmet == ("observed_records",)


def test_an_incomplete_configuration_names_its_missing_inputs_offline() -> None:
    bare = TelemetryRuntimeConfig()
    assert bare.live_evidence_unmet == (
        "deployment_inputs",
        "observability_enabled",
        "live_classification",
        "live_evidence_gate",
        "observed_records",
    )
    assert bare.configured().live_ready is False


def test_the_live_evidence_gate_defaults_to_pending_offline() -> None:
    """The gate is the owner's, so nothing in this repository establishes it."""
    assert TelemetryRuntimeConfig().live_evidence_gate == "pending"
    assert full().live_evidence_gate == "pending"
    assert full().configured().live_evidence_gate == "pending"
    assert full().configured().public().live_evidence_gate == "pending"


def test_no_owned_module_spells_a_deployment_constant_offline() -> None:
    for relative in OWNED_SOURCES:
        body = relative.read_text(encoding="utf-8")
        for name, pattern in (
            ("account", ACCOUNT_FORM),
            ("region", REGION_FORM),
            ("arn", ARN_FORM),
            ("log group", LOG_GROUP_FORM),
        ):
            found = pattern.search(body)
            assert found is None, f"{relative} spells a {name}: {found}"


def test_the_projection_covers_exactly_the_documented_variables_offline() -> None:
    variables = full().configured().variables
    assert set(OTEL_VARIABLES) == set(VARIABLE_TREATMENT)
    assert variables.accounted == set(OTEL_VARIABLES)
    assert variables.absent == ()
    assert variables.redacted == ()
    assert set(variables.mapping) == set(OTEL_VARIABLES)


def test_the_projection_carries_the_configured_identity_offline() -> None:
    mapping = full().configured().variables.mapping
    assert mapping["AGENT_OBSERVABILITY_ENABLED"] == "true"
    assert mapping["UNIFIED_TRACES_DESTINATION_ENABLED"] == "true"
    assert SYNTHETIC_LOG_GROUP in mapping["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]
    assert SYNTHETIC_LOG_STREAM in mapping["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]
    assert SYNTHETIC_LOG_GROUP in mapping["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
    assert SYNTHETIC_SERVICE in mapping["OTEL_RESOURCE_ATTRIBUTES"]


def test_a_variable_with_no_configured_input_is_absent_not_empty_offline() -> None:
    bare = TelemetryRuntimeConfig(observability_enabled=False).configured()
    assert "OTEL_EXPORTER_OTLP_LOGS_HEADERS" in bare.variables.absent
    assert "OTEL_EXPORTER_OTLP_TRACES_HEADERS" in bare.variables.absent
    assert "OTEL_RESOURCE_ATTRIBUTES" in bare.variables.absent
    assert bare.variables.value("OTEL_EXPORTER_OTLP_LOGS_HEADERS") is None
    assert bare.variables.mapping["AGENT_OBSERVABILITY_ENABLED"] == "false"


def test_a_disabled_runtime_still_states_both_documented_flags_offline() -> None:
    mapping = (
        TelemetryRuntimeConfig(observability_enabled=False, unified_telemetry=False)
        .configured()
        .variables.mapping
    )
    assert mapping["AGENT_OBSERVABILITY_ENABLED"] == "false"
    assert mapping["UNIFIED_TRACES_DESTINATION_ENABLED"] == "false"


@pytest.mark.parametrize("name", sorted(FORBIDDEN_VARIABLES))
def test_a_credential_variable_is_refused_by_the_contract_offline(name: str) -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(
            variables=(ConfigVariable.model_construct(name=name, value="x"),),
            absent=OTEL_VARIABLES,
        )


def test_an_undocumented_variable_is_refused_offline() -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(
            variables=(
                ConfigVariable.model_construct(name="OTEL_UNDOCUMENTED", value="x"),
            ),
            absent=OTEL_VARIABLES,
        )


def test_a_variable_value_carrying_a_secret_marker_is_refused_offline() -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(
            variables=(
                ConfigVariable(
                    name="OTEL_RESOURCE_ATTRIBUTES",
                    value="authorization=Bearer abcdefghijklmnop",
                ),
            ),
            absent=tuple(
                name for name in OTEL_VARIABLES if name != "OTEL_RESOURCE_ATTRIBUTES"
            ),
        )


def test_every_documented_variable_is_accounted_for_exactly_once_offline() -> None:
    with pytest.raises(ValidationError):
        OtelConfigMap(variables=(), absent=OTEL_VARIABLES[:-1])
    with pytest.raises(ValidationError):
        OtelConfigMap(
            variables=(ConfigVariable(name="OTEL_PYTHON_DISTRO", value="aws_distro"),),
            absent=OTEL_VARIABLES,
        )


def test_the_public_projection_removes_the_identity_bearing_variables_offline() -> None:
    public = full().configured().public()
    assert public.visibility == "public"
    assert public.dimensions.identity_bearing is False
    assert public.dimensions.identity_hash is not None
    withheld = {
        name for name, treatment in VARIABLE_TREATMENT.items() if treatment == "redact"
    }
    assert set(public.variables.redacted) == withheld
    assert withheld.isdisjoint(public.variables.mapping)
    assert SYNTHETIC_LOG_GROUP not in str(public.model_dump())
    assert SYNTHETIC_ACCOUNT not in str(public.model_dump())


def test_the_public_projection_is_idempotent_offline() -> None:
    once = full().configured().public()
    assert once.public() == once


def test_the_configuration_hash_is_stable_and_total_offline() -> None:
    assert full().config_hash == full().config_hash
    for name in REQUIRED_LIVE_FIELDS:
        changed = TelemetryRuntimeConfig.model_validate(
            full().model_dump() | {name: None}
        )
        assert changed.config_hash != full().config_hash
    assert full().configured().config_hash == full().config_hash
    assert full().configured().public().config_hash == full().config_hash


def test_identity_hashes_are_domain_separated_offline() -> None:
    assert scoped_digest("account", SYNTHETIC_ACCOUNT) != scoped_digest(
        "principal", SYNTHETIC_ACCOUNT
    )
    assert scoped_digest("account", SYNTHETIC_ACCOUNT) == scoped_digest(
        "account", SYNTHETIC_ACCOUNT
    )


def test_the_arn_must_agree_with_the_declared_account_and_region_offline() -> None:
    with pytest.raises(ValidationError):
        TelemetryRuntimeConfig.model_validate(
            full().model_dump() | {"account_id": "9" * 12}
        )


def test_a_pricing_figure_without_provenance_is_refused_offline() -> None:
    with pytest.raises(ValidationError):
        TelemetryRuntimeConfig.model_validate(
            full().model_dump() | {"currency": "USD", "pricing_source": None}
        )
    priced = TelemetryRuntimeConfig.model_validate(
        full().model_dump()
        | {
            "pricing_source": "published-price-list",
            "pricing_date": "2026-10-20",
            "currency": "USD",
        }
    )
    assert priced.pricing().availability == "measured"
    assert TelemetryRuntimeConfig().pricing().availability == "not_supplied"


def test_nothing_in_the_projection_reads_the_ambient_credential_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("telemetry configuration reads no ambient value")

    monkeypatch.setattr("os.environ", {})
    monkeypatch.setattr("os.getenv", denied)
    configured = full().configured()
    assert set(configured.variables.mapping) == set(OTEL_VARIABLES)
