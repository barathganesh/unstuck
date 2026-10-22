"""Static versioned Tool Capability Cards.

A card is the public, immutable description of one diagnostic read: what it
observes, what it costs, and the contract version it was published under. The
internal canonical capability is never part of the card, so a selector can
reason about options without learning the private execution surface.

Cards are static. Runtime availability is supplied separately on every call so
that a temporarily unavailable tool changes the decision context without
rewriting the published catalog.
"""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field

from unstuck.cascade.serialization import canonical_digest
from unstuck.decision import Probe, default_catalog
from unstuck.domain import Contract, Identifier

CATALOG_VERSION = "cards-v1"
TOOL_CONTRACT_VERSION = "tools-v1"
DATA_SCHEMA_VERSION = "schema-v1"

Capability = Literal["inspect_pipeline", "inspect_schema_keys", "inspect_scope"]

_ALIASES: dict[str, tuple[str, ...]] = {
    "inspect_pipeline": ("inspect-pipeline", "probe_pipeline"),
    "inspect_schema_keys": ("inspect-schema-keys", "probe_schema_keys"),
    "inspect_scope": ("inspect-scope", "probe_scope"),
}


class ToolCapabilityCard(Contract):
    """Public static description of one diagnostic read."""

    public_tool_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=512)]
    #: The published condition under which observing these facts distinguishes
    #: something. Carried from the probe's own public surface, so it enters
    #: `card_hash` and `catalog_hash` and a renamed catalog publishes renamed
    #: conditions. It names no other tool and no private fact.
    applies_when: Annotated[str, Field(min_length=1, max_length=256)]
    input_schema: Literal["no_arguments"] = "no_arguments"
    observable_facts: Annotated[
        tuple[Identifier, ...], Field(min_length=1, max_length=8)
    ]
    cost_class: Literal["local_read"] = "local_read"
    catalog_version: Identifier = CATALOG_VERSION
    tool_contract_version: Identifier = TOOL_CONTRACT_VERSION

    @property
    def card_hash(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class CardBinding:
    """Binds one public card to its private canonical capability and aliases."""

    card: ToolCapabilityCard
    capability: Capability
    aliases: tuple[str, ...] = field(default=())


def _binding(probe: Probe) -> CardBinding:
    return CardBinding(
        card=ToolCapabilityCard(
            public_tool_id=probe.public.id,
            description=probe.public.description,
            applies_when=probe.public.applies_when,
            observable_facts=probe.public.observable_facts,
        ),
        capability=probe.capability,
        aliases=_ALIASES.get(probe.public.id, ()),
    )


def capability_cards(
    catalog: tuple[Probe, ...] | None = None,
) -> tuple[CardBinding, ...]:
    """Publish the static card catalog derived from the private probe catalog."""
    return tuple(_binding(probe) for probe in (catalog or default_catalog()))


def card_index(bindings: tuple[CardBinding, ...]) -> dict[str, CardBinding]:
    """Index bindings by public identifier and by every published alias."""
    index: dict[str, CardBinding] = {}
    for binding in bindings:
        for label in (binding.card.public_tool_id, *binding.aliases):
            if label in index:
                raise ValueError("duplicate capability label")
            index[label] = binding
    return index


def canonical_capability(
    label: str, bindings: tuple[CardBinding, ...] | None = None
) -> Capability:
    """Resolve a public identifier or alias to its canonical capability.

    Aliases are presentation labels only. They resolve to the same capability,
    so a renamed tool can neither reset a budget nor evade receipt matching.
    """
    binding = card_index(bindings or capability_cards()).get(label)
    if binding is None:
        raise ValueError("unknown capability label")
    return binding.capability


def card_for(
    label: str, bindings: tuple[CardBinding, ...] | None = None
) -> ToolCapabilityCard:
    binding = card_index(bindings or capability_cards()).get(label)
    if binding is None:
        raise ValueError("unknown capability label")
    return binding.card


def catalog_hash(bindings: tuple[CardBinding, ...]) -> str:
    """Identity of the published catalog, independent of private capabilities."""
    return canonical_digest(
        [binding.card.model_dump(mode="json") for binding in bindings]
    )
