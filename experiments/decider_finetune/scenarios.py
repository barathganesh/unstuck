"""Synthetic scenario families, tool catalogs, and the frozen partition assignment.

A family is one deterministic public partition shape: arrival state, key
encodings, approved join modes, and whether any order matches the requested
date or country. A variant changes only label-neutral surface: the dimension
set, the order count and units, the requested date, and the opaque dataset
identity. Nothing here is truth. Which diagnostic read is productive for a
family is never declared; `oracle` derives it by executing every candidate
through the real controller and verifier.

Partitions are disjoint by family and by catalog. Train sees the production
catalog plus four renamed catalogs; calibration and test each see the
production catalog plus one renamed catalog no other partition sees, so a
held-out row can be answered only from what its cards say, not from an
identifier or wording memorized in training. Each catalog has its own option
order, so every capability occupies every position somewhere in train.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from unstuck.cascade.cards import Capability
from unstuck.decision import Probe, PublicProbe, default_catalog
from unstuck.domain import JoinMode, TaskSpec
from unstuck.fixtures import Country, Fixture, Order

Partition = Literal["train", "calibration", "test"]
PARTITIONS: tuple[Partition, ...] = ("train", "calibration", "test")
#: Audited families are labeled to show why they are excluded, and never ship.
Split = Literal["train", "calibration", "test", "audit"]
Encoding = Literal["plain", "country_prefix"]

#: The requested date every variant is offset from.
BASE_DATE = date(2026, 10, 18)
#: Label-neutral dimension sets, cycled by variant.
DIMENSIONS: tuple[tuple[str, ...], ...] = (
    ("US", "IN"),
    ("US", "IN", "DE"),
    ("GB", "FR"),
    ("JP", "BR", "CA"),
    ("MX", "KE"),
    ("AU", "SE", "PL"),
)
PREFIX = "country:"


@dataclass(frozen=True)
class CatalogEntry:
    """One published card surface bound to its private capability."""

    public_id: str
    capability: Capability
    description: str
    applies_when: str


@dataclass(frozen=True)
class Catalog:
    """One published tool catalog, in its own option order."""

    name: str
    entries: tuple[CatalogEntry, ...]

    def probes(self) -> tuple[Probe, ...]:
        """The private probe tuple, with the shipped observable facts."""
        facts = {
            probe.capability: probe.public.observable_facts
            for probe in default_catalog()
        }
        return tuple(
            Probe(
                PublicProbe(
                    id=entry.public_id,
                    description=entry.description,
                    applies_when=entry.applies_when,
                    observable_facts=facts[entry.capability],
                ),
                entry.capability,
            )
            for entry in self.entries
        )

    def public_id(self, capability: str) -> str:
        return next(e.public_id for e in self.entries if e.capability == capability)

    def capability(self, public_id: str) -> Capability:
        return next(e.capability for e in self.entries if e.public_id == public_id)

    @property
    def order(self) -> tuple[Capability, ...]:
        return tuple(entry.capability for entry in self.entries)


def _production() -> Catalog:
    return Catalog(
        name="production",
        entries=tuple(
            CatalogEntry(
                probe.public.id,
                probe.capability,
                probe.public.description,
                probe.public.applies_when,
            )
            for probe in default_catalog()
        ),
    )


def _renamed(
    name: str, order: str, texts: dict[Capability, tuple[str, str, str]]
) -> Catalog:
    """One renamed catalog. `order` spells the capability order, e.g. "SCP"."""
    letters: dict[str, Capability] = {
        "P": "inspect_pipeline",
        "S": "inspect_schema_keys",
        "C": "inspect_scope",
    }
    return Catalog(
        name=name,
        entries=tuple(
            CatalogEntry(
                texts[letters[letter]][0], letters[letter], *texts[letters[letter]][1:]
            )
            for letter in order
        ),
    )


PRODUCTION = _production()
CATALOGS: dict[str, Catalog] = {
    "production": PRODUCTION,
    "r1": _renamed(
        "r1",
        "SCP",
        {
            "inspect_pipeline": (
                "probe_feed_status",
                "Read whether the requested partition is marked ready and where its watermark stands",
                "it is not yet settled whether the requested partition has fully "
                "landed, so the rows could simply be missing for now",
            ),
            "inspect_schema_keys": (
                "probe_key_formats",
                "Read the declared key encodings on both join sides, their coverage, and normalized key uniqueness",
                "rows may exist and still fail to match, because it is not settled "
                "whether both join sides write the key in the same format",
            ),
            "inspect_scope": (
                "probe_request_bounds",
                "Read whether the available metadata spans the fixed requested scope",
                "it is not settled whether the request asks for a range the metadata "
                "covers, so the request itself may be what does not fit",
            ),
        },
    ),
    "r2": _renamed(
        "r2",
        "CPS",
        {
            "inspect_pipeline": (
                "check_partition_arrival",
                "Report the arrival state of the requested partition and its load watermark",
                "nobody has yet confirmed that the requested partition finished "
                "loading, so the absence of rows may be temporary",
            ),
            "inspect_schema_keys": (
                "check_join_encoding",
                "Report how each side of the join encodes its key, whether that is fully declared, and key uniqueness after normalizing",
                "the rows might be loaded but not pair up, since it is still unknown "
                "whether the two join sides declare one key encoding",
            ),
            "inspect_scope": (
                "check_scope_coverage",
                "Report whether metadata covers the immutable requested scope",
                "it is still unknown whether the requested range is one the metadata "
                "covers, so the mismatch may lie in the request",
            ),
        },
    ),
    "r3": _renamed(
        "r3",
        "SPC",
        {
            "inspect_pipeline": (
                "read_load_progress",
                "Inspect load progress for the requested partition, including its readiness flag",
                "the requested partition may still be in flight, so an empty output "
                "could mean the data has not arrived",
            ),
            "inspect_schema_keys": (
                "read_key_declarations",
                "Inspect the key encoding each join side declares, metadata coverage, and uniqueness of normalized keys",
                "data may already be present but not join, because it is open whether "
                "the join sides agree on how the key is written",
            ),
            "inspect_scope": (
                "read_scope_metadata",
                "Inspect whether the metadata reaches across the requested scope",
                "it is open whether the request targets a range the metadata reaches, "
                "so the request may be the part that is off",
            ),
        },
    ),
    "r4": _renamed(
        "r4",
        "PCS",
        {
            "inspect_pipeline": (
                "view_ingest_state",
                "Look up ingestion status and watermark for the requested partition",
                "it remains unknown whether ingestion for the requested partition "
                "has completed, so rows may not exist yet",
            ),
            "inspect_schema_keys": (
                "view_key_alignment",
                "Look up declared join key encodings, their completeness, and normalized key uniqueness",
                "records may be present yet unmatched, because it remains unknown "
                "whether both sides of the join encode the key alike",
            ),
            "inspect_scope": (
                "view_request_coverage",
                "Look up whether metadata covers the unchanged requested scope",
                "it remains unknown whether the metadata covers the range the "
                "request names, so the request could be the mismatch",
            ),
        },
    ),
    "r5": _renamed(
        "r5",
        "CSP",
        {
            "inspect_pipeline": (
                "fetch_partition_readiness",
                "Fetch the readiness state and high watermark of the requested partition",
                "it has not been established that the requested partition is fully "
                "loaded, so missing rows may just be late",
            ),
            "inspect_schema_keys": (
                "fetch_key_encodings",
                "Fetch both join sides' declared key encodings, declaration coverage, and normalized key uniqueness",
                "the rows could be there but fail to line up, because it has not been "
                "established that both join sides share a key encoding",
            ),
            "inspect_scope": (
                "fetch_scope_bounds",
                "Fetch whether the metadata bounds include the fixed requested scope",
                "it has not been established that the metadata includes the requested "
                "range, so the request may be outside what exists",
            ),
        },
    ),
    "r6": _renamed(
        "r6",
        "SCP",
        {
            "inspect_pipeline": (
                "query_arrival_watermark",
                "Query whether the requested slice is ready and how far its feed watermark has advanced",
                "it is unclear whether the requested slice has arrived yet, so the "
                "rows may be absent only for the moment",
            ),
            "inspect_schema_keys": (
                "query_key_shapes",
                "Query the key shape each join side declares, whether declarations are complete, and normalized key uniqueness",
                "the rows may be loaded already and still not pair up, because it is "
                "unclear whether both sides spell the join key the same way",
            ),
            "inspect_scope": (
                "query_scope_reach",
                "Query whether the metadata reaches the immutable requested scope",
                "it is unclear whether the request reaches for a range the metadata "
                "covers, so the request rather than the data may be off",
            ),
        },
    ),
}

#: Which catalogs each partition is rendered under. Renamed catalogs never
#: cross partitions; the production catalog is in all three because it is
#: what the deployed selector is actually shown.
PARTITION_CATALOGS: dict[Partition, tuple[str, ...]] = {
    "train": ("production", "r1", "r2", "r3", "r4"),
    "calibration": ("production", "r5"),
    "test": ("production", "r6"),
}
#: Variants per family, by partition.
VARIANTS: dict[Partition, int] = {"train": 6, "calibration": 4, "test": 4}


@dataclass(frozen=True)
class Family:
    """One public partition shape. Every field is ordinary public fixture data."""

    name: str
    job_state: Literal["pending", "complete", "failed"]
    orders_encoding: Encoding
    countries_encoding: Encoding
    approved: tuple[JoinMode, ...]
    #: `none`: no orders at all; `on_date`: orders on the requested date;
    #: `off_date`: orders only on another date.
    orders: Literal["none", "on_date", "off_date"]
    #: Filter the task to one dimension country that holds no order.
    filter_absent_country: bool = False


FAMILIES: dict[str, Family] = {
    family.name: family
    for family in (
        Family(
            "arrival_pending_plain",
            "pending",
            "plain",
            "plain",
            ("literal",),
            "on_date",
        ),
        Family(
            "arrival_pending_off_date",
            "pending",
            "plain",
            "plain",
            ("literal",),
            "off_date",
        ),
        Family(
            "arrival_pending_no_orders",
            "pending",
            "plain",
            "plain",
            ("literal",),
            "none",
        ),
        Family(
            "arrival_pending_prefixed",
            "pending",
            "country_prefix",
            "country_prefix",
            ("literal",),
            "on_date",
        ),
        Family(
            "key_drift_dimension",
            "complete",
            "plain",
            "country_prefix",
            ("literal", "strip_country_prefix"),
            "on_date",
        ),
        Family(
            "key_drift_fact",
            "complete",
            "country_prefix",
            "plain",
            ("literal", "strip_country_prefix"),
            "on_date",
        ),
        Family("empty_plain", "complete", "plain", "plain", ("literal",), "none"),
        Family(
            "empty_off_date", "complete", "plain", "plain", ("literal",), "off_date"
        ),
        Family(
            "empty_prefixed",
            "complete",
            "country_prefix",
            "country_prefix",
            ("literal",),
            "none",
        ),
        Family(
            "empty_filtered_country",
            "complete",
            "plain",
            "plain",
            ("literal",),
            "on_date",
            filter_absent_country=True,
        ),
        Family(
            "empty_drift_dimension",
            "complete",
            "plain",
            "country_prefix",
            ("literal", "strip_country_prefix"),
            "none",
        ),
        Family(
            "empty_drift_off_date",
            "complete",
            "plain",
            "country_prefix",
            ("literal", "strip_country_prefix"),
            "off_date",
        ),
        Family(
            "empty_drift_fact",
            "complete",
            "country_prefix",
            "plain",
            ("literal", "strip_country_prefix"),
            "none",
        ),
        # Audited, never assigned: the oracle must show why each is excluded.
        Family(
            "pending_key_drift",
            "pending",
            "plain",
            "country_prefix",
            ("literal", "strip_country_prefix"),
            "on_date",
        ),
        Family(
            "unapproved_key_drift",
            "complete",
            "plain",
            "country_prefix",
            ("literal",),
            "on_date",
        ),
        Family("failed_job", "failed", "plain", "plain", ("literal",), "on_date"),
    )
}

#: The frozen family assignment. Disjoint by construction and re-checked.
ASSIGNMENT: dict[Partition, tuple[str, ...]] = {
    "train": (
        "arrival_pending_plain",
        "arrival_pending_off_date",
        "key_drift_dimension",
        "empty_plain",
        "empty_off_date",
        "empty_drift_dimension",
    ),
    "calibration": (
        "arrival_pending_no_orders",
        "empty_prefixed",
        "empty_drift_off_date",
    ),
    "test": (
        "arrival_pending_prefixed",
        "key_drift_fact",
        "empty_filtered_country",
        "empty_drift_fact",
    ),
}
AUDITED: tuple[str, ...] = ("pending_key_drift", "unapproved_key_drift", "failed_job")


@dataclass(frozen=True)
class Scenario:
    """One concrete case: a family, a variant, and the catalog it is shown under."""

    partition: Split
    family: str
    variant: int
    catalog: str

    @property
    def key(self) -> str:
        return f"{self.partition}:{self.family}:{self.variant}:{self.catalog}"

    @property
    def opaque(self) -> str:
        """An identity that encodes nothing about the family or the label."""
        return hashlib.sha256(self.key.encode()).hexdigest()[:12]

    def fixture(self) -> Fixture:
        family = FAMILIES[self.family]
        codes = DIMENSIONS[self.variant % len(DIMENSIONS)]
        requested = BASE_DATE - timedelta(days=3 * self.variant)
        order_date = (
            requested + timedelta(days=2) if family.orders == "off_date" else requested
        )
        # With a country filter, orders exist only outside the filtered country.
        holders = codes[:-1] if family.filter_absent_country else codes
        count = 0 if family.orders == "none" else 2 + self.variant % 3
        orders = tuple(
            Order(
                order_id=f"o{self.opaque}{index}",
                country_id=_key(holders[index % len(holders)], family.orders_encoding),
                order_date_utc=order_date,
                units=2 + index + self.variant,
            )
            for index in range(count)
        )
        countries = tuple(
            Country(
                country_id=_key(code, family.countries_encoding),
                country_code=code,
                country_name=f"Dimension {code}",
            )
            for code in codes
        )
        complete = family.job_state == "complete"
        return Fixture(
            classification="synthetic",
            dataset_id=f"ds-{self.opaque[:8]}",
            dataset_version=f"v{int(self.opaque[8:], 16) % 40 + 2}",
            order_date_utc=requested,
            orders=orders,
            countries=countries,
            job_state=family.job_state,
            scope_complete=complete,
            metadata_complete=True,
            scope_covered=True,
            orders_key_encoding=family.orders_encoding,
            countries_key_encoding=family.countries_encoding,
            approved_join_modes=family.approved,
            arrival_after_seconds=None,
        )

    def task(self) -> TaskSpec:
        fixture = self.fixture()
        family = FAMILIES[self.family]
        codes = DIMENSIONS[self.variant % len(DIMENSIONS)]
        return TaskSpec(
            dataset_id=fixture.dataset_id,
            order_date_utc=fixture.order_date_utc,
            country_codes=(codes[-1],) if family.filter_absent_country else (),
        )


def _key(code: str, encoding: Encoding) -> str:
    return f"{PREFIX}{code}" if encoding == "country_prefix" else code


def scenarios(partition: Partition) -> tuple[Scenario, ...]:
    """Every case of one partition, in a fixed order."""
    return tuple(
        Scenario(partition, family, variant, catalog)
        for family in ASSIGNMENT[partition]
        for variant in range(VARIANTS[partition])
        for catalog in PARTITION_CATALOGS[partition]
    )


def audited() -> tuple[Scenario, ...]:
    """One production-catalog case per audited family, assigned nowhere."""
    return tuple(Scenario("audit", family, 0, "production") for family in AUDITED)
