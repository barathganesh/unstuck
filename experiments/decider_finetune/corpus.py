"""Build, freeze and verify the receipt-aware Decider 2B fine-tuning corpus.

Usage, from the repository root:

    python -m experiments.decider_finetune.corpus build --out DIR --model-revision REV
    python -m experiments.decider_finetune.corpus verify --corpus DIR [--no-replay]
    python -m experiments.decider_finetune.corpus scope --baseline START.json

`build` captures every row from the real controller (`oracle`), labels it by
counterfactual execution, refuses any public twin whose families disagree,
and writes three partitions, each as three files:

* `<partition>.jsonl`: the official `training/data/format.py` `Example` schema,
  `kind="choice"`, `state` the exact production state text, `instructions`
  the production question, `options` the offered `[option_id, criterion]`
  pairs in offered order, `label` the index of the one productive option.
* `<partition>.requests.jsonl`: the exact production wire request per row.
  This is the only file an inference run reads. It carries no truth.
* `<partition>.truth.jsonl`: what every offered option did when executed.

`manifest.json` binds every file by sha256 and `manifest.sha256` binds the
manifest. Every written file is made read-only. Nothing in the output depends
on wall-clock time, a random draw, or an absolute path.
"""

import argparse
import json
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deployment.sagemaker.server.contract import ServeRequest
from experiments.decider_finetune.identity import file_sha256, prompt_digest
from experiments.decider_finetune.oracle import (
    REVIEW,
    Config,
    Labeled,
    choose,
    label_after,
    label_fresh,
    productive,
    run_call,
)
from experiments.decider_finetune.scenarios import (
    ASSIGNMENT,
    CATALOGS,
    FAMILIES,
    PARTITION_CATALOGS,
    PARTITIONS,
    VARIANTS,
    Partition,
    Scenario,
    audited,
)
from unstuck.cascade.cards import ToolCapabilityCard
from unstuck.cascade.choice import REVIEW_CRITERION, choice_request, criterion_for
from unstuck.cascade.projector import QUESTION, SelectorInput
from unstuck.cascade.repository import LocalReceiptRepository
from unstuck.cascade.serialization import canonical_text

SCHEMA = "decider-finetune-corpus-v1"
#: The official training repository revision the Example schema is read from.
UPSTREAM_COMMIT = "d942e5515db750f80a51032629ec312455a87976"
KINDS: tuple[str, ...] = ("fresh_d1", "receipt_d1")
#: Fresh first decisions are captured under both production read budgets.
FRESH_BUDGETS: tuple[int, ...] = (1, 2)
MIN_FAMILIES = 3
MIN_CAPABILITIES = 2
SOURCES: tuple[str, ...] = ("scenarios.py", "oracle.py", "corpus.py", "identity.py")
HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
#: Words that only the evaluator side uses. None may reach a model input.
TRUTH_WORDS: tuple[str, ...] = ("productive", "acceptable", "expected", "label", *KINDS)


@dataclass(frozen=True)
class Plan:
    """Which families, variants and catalogs each partition is built from."""

    assignment: Mapping[Partition, tuple[str, ...]] = field(
        default_factory=lambda: dict(ASSIGNMENT)
    )
    variants: Mapping[Partition, int] = field(default_factory=lambda: dict(VARIANTS))
    catalogs: Mapping[Partition, tuple[str, ...]] = field(
        default_factory=lambda: dict(PARTITION_CATALOGS)
    )

    def scenarios(self, partition: Partition) -> tuple[Scenario, ...]:
        return tuple(
            Scenario(partition, family, variant, catalog)
            for family in self.assignment[partition]
            for variant in range(self.variants[partition])
            for catalog in self.catalogs[partition]
        )

    def describe(self) -> dict[str, Any]:
        return {
            p: {
                "families": list(self.assignment[p]),
                "variants": self.variants[p],
                "catalogs": list(self.catalogs[p]),
            }
            for p in PARTITIONS
        }


def _row(
    scenario: Scenario,
    kind: str,
    budget: int,
    prior: str | None,
    labeled: Labeled,
) -> dict[str, Any]:
    catalog = CATALOGS[scenario.catalog]
    prompt = labeled.captured.prompt
    winners = labeled.productive
    return {
        "partition": scenario.partition,
        "family": scenario.family,
        "catalog": scenario.catalog,
        "variant": scenario.variant,
        "kind": kind,
        "budget": budget,
        "prior_capability": None if prior is None else catalog.capability(prior),
        "prompt_sha256": prompt.digest,
        "question": prompt.question,
        "state_text": prompt.state_text,
        "options": [list(o) for o in prompt.options],
        "wire": labeled.captured.wire.decode(),
        "offered": list(labeled.captured.available_tool_ids),
        "offered_capabilities": [
            catalog.capability(i) for i in labeled.captured.available_tool_ids
        ],
        "precluded": list(labeled.captured.precluded_tool_ids),
        "history_status": labeled.captured.history_status,
        "label_id": winners[0] if len(winners) == 1 else None,
        "label_capability": (
            catalog.capability(winners[0]) if len(winners) == 1 else None
        ),
        "productive_count": len(winners),
        "outcomes": [
            {
                "public_id": o.public_id,
                "capability": o.capability,
                "first_read_outcome": o.first_read_outcome,
                "reason_code": o.reason_code,
                "verified": o.verified,
                "productive": o.productive,
                "false_repair": o.false_repair,
            }
            for o in labeled.outcomes
        ],
    }


def scenario_rows(scenario: Scenario, config: Config) -> list[dict[str, Any]]:
    """Every receipt-aware request one scenario produces, each labeled."""
    rows: list[dict[str, Any]] = []
    offered: tuple[str, ...] = ()
    for budget in FRESH_BUDGETS:
        fresh = label_fresh(scenario, budget, config)
        if fresh is None:
            continue
        rows.append(_row(scenario, "fresh_d1", budget, None, fresh))
        if budget == 1:
            offered = fresh.captured.available_tool_ids
    for prior in offered:
        after = label_after(scenario, prior, config)
        if after is not None:
            rows.append(_row(scenario, "receipt_d1", 1, prior, after[1]))
    return rows


def signature(row: Mapping[str, Any]) -> str:
    """The public decision situation, with catalog wording and opaque ids removed.

    Two rows with one signature are indistinguishable to any selector except by
    identifiers and paraphrase, so they must agree on the productive read.
    """
    state = json.loads(row["state_text"])
    catalog = CATALOGS[row["catalog"]]
    reports = []
    for item in state["observations"]:
        facts = json.loads(item["summary"])
        reports.append(
            [item["kind"], sorted((k, v) for k, v in facts.items() if "hash" not in k)]
        )
    history = sorted(
        [m["selected_capability"], m["outcome_class"], m["match_class"]]
        for m in state["history"]["matches"]
    )
    return json.dumps(
        {
            "pass": state["pass_index"],
            "reports": reports,
            "history": history,
            "offered": sorted(row["offered_capabilities"]),
            "precluded": sorted(
                catalog.capability(p["public_tool_id"])
                for p in state.get("precluded", [])
            ),
            "budgets": state["budgets"],
        },
        sort_keys=True,
    )


def _example(row: Mapping[str, Any], weight: float) -> dict[str, Any]:
    """One official `Example`, field for field and in `asdict` order."""
    return {
        "kind": "choice",
        "state": row["state_text"],
        "instructions": row["question"],
        "options": row["options"],
        "label": [o[0] for o in row["options"]].index(row["label_id"]),
        "task": row["family"],
        "weight": weight,
        "instruction_variants": [],
    }


def _weights(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    """Balance the label capabilities in train. Calibration and test stay 1.0."""
    counts = Counter(r["label_capability"] for r in rows)
    total = len(rows)
    return [
        round(total / (len(counts) * counts[r["label_capability"]]), 6) for r in rows
    ]


def _map(
    function: Callable[..., Any], items: Sequence[Any], *args: Any, workers: int
) -> list[Any]:
    if workers <= 1:
        return [function(item, *args) for item in items]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(function, items, *[[a] * len(items) for a in args]))


def _audit(scenario: Scenario, config: Config) -> dict[str, Any] | None:
    fresh = label_fresh(scenario, 1, config)
    if fresh is None:
        return None
    return _row(scenario, "fresh_d1", 1, None, fresh)


def _dump(path: Path, lines: Iterable[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build(
    out: Path, config: Config, *, plan: Plan | None = None, workers: int | None = None
) -> dict[str, Any]:
    """Generate, label, check and freeze the corpus. Refuses an existing `out`."""
    plan = plan or Plan()
    workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 1)
    out.mkdir(parents=True, exist_ok=False)
    by_partition: dict[str, list[dict[str, Any]]] = {}
    for partition in PARTITIONS:
        batches = _map(
            scenario_rows, plan.scenarios(partition), config, workers=workers
        )
        by_partition[partition] = [row for batch in batches for row in batch]
    unlabeled: Counter[str] = Counter()
    for name, rows in by_partition.items():
        for row in rows:
            if row["label_id"] is None:
                unlabeled[
                    f"{row['family']}:{row['kind']}:{row['productive_count']}"
                ] += 1
        by_partition[name] = [r for r in rows if r["label_id"] is not None]
    situations: dict[str, set[str]] = defaultdict(set)
    for rows in by_partition.values():
        for row in rows:
            situations[signature(row)].add(row["label_capability"])
    conflicts = sorted(s for s, labels in situations.items() if len(labels) > 1)
    if conflicts:
        raise SystemExit(
            f"public twins disagree on the productive read: {conflicts[:2]}"
        )
    excluded = []
    for audit in _map(_audit, audited(), config, workers=workers):
        if audit is None:
            continue
        twin = situations.get(signature(audit), set())
        reason = (
            "no unique productive first read"
            if audit["label_id"] is None and not audit["productive_count"]
            else "several productive first reads"
            if audit["label_id"] is None
            else "public twin of an included family with another productive read"
            if twin and audit["label_capability"] not in twin
            else "not assigned"
        )
        excluded.append(
            {
                "family": audit["family"],
                "reason": reason,
                "productive": [
                    o["capability"] for o in audit["outcomes"] if o["productive"]
                ],
                "false_repairs": [
                    o["capability"] for o in audit["outcomes"] if o["false_repair"]
                ],
            }
        )
    files: dict[str, dict[str, str]] = {}
    summary: dict[str, Any] = {}
    for partition_name, rows in by_partition.items():
        weights = _weights(rows) if partition_name == "train" else [1.0] * len(rows)
        names = {
            "examples": f"{partition_name}.jsonl",
            "requests": f"{partition_name}.requests.jsonl",
            "truth": f"{partition_name}.truth.jsonl",
        }
        _dump(
            out / names["examples"],
            (
                json.dumps(_example(r, w), ensure_ascii=False)
                for r, w in zip(rows, weights, strict=True)
            ),
        )
        _dump(out / names["requests"], (r["wire"] for r in rows))
        _dump(
            out / names["truth"],
            (_canonical({k: v for k, v in r.items() if k != "wire"}) for r in rows),
        )
        files[partition_name] = {
            name: file_sha256(out / filename) for name, filename in names.items()
        }
        summary[partition_name] = {
            "rows": len(rows),
            "families": sorted({r["family"] for r in rows}),
            "catalogs": sorted({r["catalog"] for r in rows}),
            "kinds": dict(sorted(Counter(r["kind"] for r in rows).items())),
            "label_capabilities": dict(
                sorted(Counter(r["label_capability"] for r in rows).items())
            ),
        }
    integrated = [
        {
            "partition": s.partition,
            "family": s.family,
            "variant": s.variant,
            "catalog": s.catalog,
        }
        for s in plan.scenarios("test")
    ]
    (out / "integrated.json").write_text(_canonical(integrated) + "\n")
    manifest = {
        "schema": SCHEMA,
        "classification": "synthetic",
        "upstream_commit": UPSTREAM_COMMIT,
        "example_schema": "training/data/format.py:Example",
        "config": {
            "model_revision": config.model_revision,
            "acceptance_tier": config.acceptance_tier,
            "fresh_budgets": list(FRESH_BUDGETS),
            "receipt_budget": 1,
            "arm": "decider_memory",
        },
        "prompt_version": prompt_version(),
        "plan": plan.describe(),
        "generator_sha256": {name: file_sha256(HERE / name) for name in SOURCES},
        "files": files,
        "integrated_sha256": file_sha256(out / "integrated.json"),
        "partitions": summary,
        "unlabeled_rows": dict(sorted(unlabeled.items())),
        "excluded_families": excluded,
        "public_situations": len(situations),
    }
    (out / "manifest.json").write_text(_canonical(manifest) + "\n")
    (out / "manifest.sha256").write_text(file_sha256(out / "manifest.json") + "\n")
    for path in out.iterdir():
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return manifest


def prompt_version() -> dict[str, str]:
    """What fixes the rendered prompt apart from the state: question and review text."""
    return {"question": QUESTION, "review_criterion": REVIEW_CRITERION}


# ---- verification -----------------------------------------------------------


@dataclass
class Corpus:
    root: Path
    manifest: dict[str, Any]
    examples: dict[str, list[dict[str, Any]]]
    requests: dict[str, list[str]]
    truth: dict[str, list[dict[str, Any]]]

    @classmethod
    def load(cls, root: Path) -> "Corpus":
        manifest = json.loads((root / "manifest.json").read_text())
        examples: dict[str, list[dict[str, Any]]] = {}
        requests: dict[str, list[str]] = {}
        truth: dict[str, list[dict[str, Any]]] = {}
        for p in PARTITIONS:
            examples[p] = [
                json.loads(x) for x in (root / f"{p}.jsonl").read_text().splitlines()
            ]
            requests[p] = (root / f"{p}.requests.jsonl").read_text().splitlines()
            truth[p] = [
                json.loads(x)
                for x in (root / f"{p}.truth.jsonl").read_text().splitlines()
            ]
        return cls(root, manifest, examples, requests, truth)

    def integrated(self) -> list[Scenario]:
        items = json.loads((self.root / "integrated.json").read_text())
        return [
            Scenario(i["partition"], i["family"], i["variant"], i["catalog"])
            for i in items
        ]


def check_files(root: Path) -> list[str]:
    """Manifest drift: every bound file, the manifest itself, and read-only bits."""
    failures = []
    manifest_path = root / "manifest.json"
    if (root / "manifest.sha256").read_text().strip() != file_sha256(manifest_path):
        failures.append("manifest.json does not match manifest.sha256")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != SCHEMA:
        failures.append("unknown corpus schema")
    bound = {"manifest.json", "manifest.sha256", "integrated.json"}
    for partition, names in manifest["files"].items():
        for name, digest in names.items():
            filename = {
                "examples": f"{partition}.jsonl",
                "requests": f"{partition}.requests.jsonl",
                "truth": f"{partition}.truth.jsonl",
            }[name]
            bound.add(filename)
            if file_sha256(root / filename) != digest:
                failures.append(f"{filename} drifted from the manifest")
    if file_sha256(root / "integrated.json") != manifest["integrated_sha256"]:
        failures.append("integrated.json drifted from the manifest")
    extra = sorted(p.name for p in root.iterdir() if p.name not in bound)
    if extra:
        failures.append(f"unbound files: {extra}")
    for path in root.iterdir():
        if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            failures.append(f"{path.name} is writable")
    return failures


def check_prompt_shape(corpus: Corpus) -> list[str]:
    """Every row is exactly what production sends, in both file forms."""
    failures = []
    expected_revision = corpus.manifest["config"]["model_revision"]
    for p in PARTITIONS:
        rows = zip(corpus.examples[p], corpus.requests[p], corpus.truth[p], strict=True)
        for index, (example, wire, truth) in enumerate(rows):
            where = f"{p}[{index}]"
            request = ServeRequest.model_validate_json(wire)
            options = [[o.option_id, o.criterion] for o in request.options]
            if request.model_revision != expected_revision:
                failures.append(f"{where}: mixed model revision")
            if request.question != QUESTION or example["instructions"] != QUESTION:
                failures.append(f"{where}: question differs from production")
            if example["state"] != request.state_text or example["options"] != options:
                failures.append(f"{where}: example does not re-render to its wire row")
            if example["kind"] != "choice" or example["instruction_variants"]:
                failures.append(f"{where}: not a plain choice example")
            if (
                options[-1] != [REVIEW, REVIEW_CRITERION]
                or request.options[-1].executable
            ):
                failures.append(f"{where}: review option is not last and inert")
            selector = SelectorInput.model_validate_json(request.state_text)
            if canonical_text(selector) != request.state_text:
                failures.append(f"{where}: state is not the canonical selector input")
            rebuilt = choice_request(selector)
            if [[o.option_id, o.criterion] for o in rebuilt.options] != options:
                failures.append(f"{where}: criteria differ from the shipped builder")
            cards = {c.public_tool_id: c for c in selector.cards}
            for option_id, criterion in options[:-1]:
                card = ToolCapabilityCard.model_validate(cards[option_id])
                if criterion != criterion_for(card):
                    failures.append(f"{where}: criterion is not the card's own")
            if (
                prompt_digest(request.question, request.state_text, options)
                != truth["prompt_sha256"]
            ):
                failures.append(f"{where}: prompt identity mismatch")
    return failures


def _model_text(corpus: Corpus, partition: str) -> list[str]:
    return [
        e["state"] + "\n" + "\n".join(o[1] + " " + o[0] for o in e["options"])
        for e in corpus.examples[partition]
    ]


def check_leakage(corpus: Corpus) -> list[str]:
    """Nothing held out is seen in training, and no truth reaches an input."""
    failures = []
    families = {p: {t["family"] for t in corpus.truth[p]} for p in PARTITIONS}
    catalogs = {
        p: {t["catalog"] for t in corpus.truth[p]} - {"production"} for p in PARTITIONS
    }
    for i, a in enumerate(PARTITIONS):
        for b in PARTITIONS[i + 1 :]:
            if families[a] & families[b]:
                failures.append(
                    f"families shared by {a} and {b}: {families[a] & families[b]}"
                )
            if catalogs[a] & catalogs[b]:
                failures.append(f"renamed catalogs shared by {a} and {b}")
    seen = {
        p: {json.loads(e["state"])["dataset_id"] for e in corpus.examples[p]}
        for p in PARTITIONS
    }
    prompts = {p: {t["prompt_sha256"] for t in corpus.truth[p]} for p in PARTITIONS}
    states = {p: {e["state"] for e in corpus.examples[p]} for p in PARTITIONS}
    for i, a in enumerate(PARTITIONS):
        for b in PARTITIONS[i + 1 :]:
            if seen[a] & seen[b]:
                failures.append(f"dataset ids shared by {a} and {b}")
            if prompts[a] & prompts[b] or states[a] & states[b]:
                failures.append(f"identical prompts in {a} and {b}")
    for p in PARTITIONS:
        if len(prompts[p]) != len(corpus.truth[p]):
            failures.append(f"duplicate prompts inside {p}")
    held_out = set()
    for name in catalogs["test"] | catalogs["calibration"]:
        for entry in CATALOGS[name].entries:
            held_out |= {entry.public_id, entry.description, entry.applies_when}
    for p in ("train",):
        for text in _model_text(corpus, p):
            if any(token in text for token in held_out):
                failures.append("a held-out catalog surface appears in train")
                break
    for name in catalogs["test"]:
        for entry in CATALOGS[name].entries:
            surface = (entry.public_id, entry.description, entry.applies_when)
            if any(
                any(s in t for s in surface) for t in _model_text(corpus, "calibration")
            ):
                failures.append("the test catalog appears in calibration")
    tokens = sorted(set(FAMILIES) | set(TRUTH_WORDS))
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE)
    for p in PARTITIONS:
        for index, text in enumerate(_model_text(corpus, p)):
            match = pattern.search(text)
            if match:
                failures.append(
                    f"{p}[{index}]: truth token {match.group(0)!r} in a model input"
                )
                break
    integrated = corpus.integrated()
    if {s.partition for s in integrated} != {"test"} or {
        s.family for s in integrated
    } != families["test"]:
        failures.append("integrated cases are not exactly the test families")
    return failures


def check_order(corpus: Corpus) -> list[str]:
    rows = corpus.truth["train"]
    orders = {tuple(CATALOGS[r["catalog"]].order) for r in rows}
    positions = {
        (cap, index)
        for r in rows
        if len(r["offered_capabilities"]) == 3
        for index, cap in enumerate(r["offered_capabilities"])
    }
    failures = []
    if len(orders) < 3:
        failures.append(f"only {len(orders)} capability orders in train")
    caps = {cap for r in rows for cap in r["offered_capabilities"]}
    missing = [
        (c, i) for c in sorted(caps) for i in range(3) if (c, i) not in positions
    ]
    if missing:
        failures.append(f"capability positions never seen in train: {missing}")
    return failures


def check_ids(corpus: Corpus) -> list[str]:
    rows = corpus.truth["train"]
    failures = []
    renamed = {r["catalog"] for r in rows} - {"production"}
    if "production" not in {r["catalog"] for r in rows} or len(renamed) < 3:
        failures.append(
            "train needs the production catalog and at least 3 renamed ones"
        )
    for name in renamed:
        production = CATALOGS["production"].entries
        for entry in CATALOGS[name].entries:
            if any(
                entry.public_id == p.public_id
                or entry.description == p.description
                or entry.applies_when == p.applies_when
                for p in production
            ):
                failures.append(f"catalog {name} reuses a production surface")
    offered: Counter[str] = Counter()
    labeled: Counter[str] = Counter()
    for r in rows:
        offered.update(r["offered"])
        labeled[r["label_id"]] += 1
    always = sorted(i for i in offered if labeled[i] == offered[i])
    if always:
        failures.append(f"public ids that are the label whenever offered: {always}")
    return failures


def fixture_rule(scenario: Scenario) -> str:
    """Independent cross-check from fixture semantics, never the label source."""
    return (
        "inspect_pipeline"
        if scenario.fixture().job_state == "pending"
        else "inspect_schema_keys"
    )


def _replay(row: dict[str, Any], config: Config) -> str | None:
    """Re-execute one row's label through the real stack. None means it held."""
    scenario = Scenario(row["partition"], row["family"], row["variant"], row["catalog"])
    store = LocalReceiptRepository()
    call_index = 1
    if row["kind"] == "receipt_d1":
        prior = CATALOGS[row["catalog"]].public_id(row["prior_capability"])
        run_call(
            scenario,
            call_index=1,
            budget=1,
            store=store,
            answerer=choose(prior),
            config=config,
        )
        call_index = 2
    trace = run_call(
        scenario,
        call_index=call_index,
        budget=row["budget"],
        store=store,
        answerer=choose(row["label_id"]),
        config=config,
    )
    if not trace.captured or trace.captured[0].prompt.digest != row["prompt_sha256"]:
        return "replayed request differs"
    if not productive(trace, scenario):
        return "label is not productive on replay"
    if row["label_capability"] != fixture_rule(scenario):
        return "label disagrees with the independent fixture rule"
    return None


def check_labels(corpus: Corpus, *, replay: bool, workers: int) -> list[str]:
    failures = []
    config = Config(
        model_revision=corpus.manifest["config"]["model_revision"],
        acceptance_tier=corpus.manifest["config"]["acceptance_tier"],
    )
    for p in PARTITIONS:
        rows = corpus.truth[p]
        families = {r["family"] for r in rows}
        caps = {r["label_capability"] for r in rows}
        if len(families) < MIN_FAMILIES or len(caps) < MIN_CAPABILITIES:
            failures.append(f"{p}: {len(families)} families, {len(caps)} capabilities")
        for index, (row, example) in enumerate(
            zip(rows, corpus.examples[p], strict=True)
        ):
            winners = [o["public_id"] for o in row["outcomes"] if o["productive"]]
            if (
                winners != [row["label_id"]]
                or example["options"][example["label"]][0] != row["label_id"]
            ):
                failures.append(
                    f"{p}[{index}]: label is not the unique productive option"
                )
        if replay:
            problems = _map(_replay, rows, config, workers=workers)
            failures += [f"{p}[{i}]: {m}" for i, m in enumerate(problems) if m]
    situations: dict[str, set[str]] = defaultdict(set)
    for p in PARTITIONS:
        for row in corpus.truth[p]:
            situations[signature(row)].add(row["label_capability"])
    if any(len(v) > 1 for v in situations.values()):
        failures.append("public twins disagree")
    return failures


def check_calibration(corpus: Corpus) -> list[str]:
    failures = []
    cal = {t["family"] for t in corpus.truth["calibration"]}
    test = {t["family"] for t in corpus.truth["test"]}
    if cal & test:
        failures.append("calibration and test share families")
    files = corpus.manifest["files"]
    if files["calibration"]["examples"] == files["test"]["examples"]:
        failures.append("calibration and test files are identical")
    text = README.read_text() if README.is_file() else ""
    command = re.search(
        r"training\.evaluate calibrate .*calibration\.jsonl.*--split all", text
    )
    if not command or re.search(r"calibrate .*test\.jsonl", text):
        failures.append(
            "README does not calibrate on calibration.jsonl with --split all only"
        )
    return failures


def verify(
    root: Path, *, replay: bool = True, workers: int | None = None
) -> dict[str, list[str]]:
    workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 1)
    results = {"CORPUS_VERIFIED": check_files(root)}
    corpus = Corpus.load(root)
    results["PROMPT_SHAPE_OK"] = check_prompt_shape(corpus)
    results["LEAKAGE_NONE"] = check_leakage(corpus)
    results["ORDER_PERMUTED"] = check_order(corpus)
    results["IDS_NOT_PREDICTIVE"] = check_ids(corpus)
    results["LABELS_REDERIVED" if replay else "LABELS_CONSISTENT"] = check_labels(
        corpus, replay=replay, workers=workers
    )
    results["CALIBRATION_ISOLATED"] = check_calibration(corpus)
    return results


# ---- scope ------------------------------------------------------------------

ALLOWED_NEW = (
    "experiments/__init__.py",
    "experiments/decider_finetune/",
    "tests/decider_finetune/",
)
FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:from|import)\s+(boto3|botocore|socket|http|urllib|requests|httpx|uvicorn|fastapi|aiohttp)\b",
    re.MULTILINE,
)


def tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for current, dirs, names in os.walk(root):
        rel = Path(current).relative_to(root)
        if rel.parts and rel.parts[0] in {".venv", ".unlazy"}:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".pytest_cache"}]
        for name in names:
            path = Path(current) / name
            key = str(path.relative_to(root))
            files[key] = (
                "symlink:" + os.readlink(path)
                if path.is_symlink()
                else file_sha256(path)
            )
    return files


def scope(baseline: Path, root: Path = Path(".")) -> list[str]:
    start = json.loads(baseline.read_text())["files"]
    now = tree(root)
    failures = []
    for name in sorted(set(start) | set(now)):
        if name not in now:
            failures.append(f"removed: {name}")
        elif name not in start:
            if not name.startswith(ALLOWED_NEW):
                failures.append(f"added outside the experiment: {name}")
        elif start[name] != now[name]:
            failures.append(f"modified: {name}")
    for name in now:
        if name.startswith(ALLOWED_NEW) and name.endswith(".py"):
            if FORBIDDEN_IMPORTS.search((root / name).read_text()):
                failures.append(f"network or AWS import: {name}")
    return failures


def _report(results: Mapping[str, list[str]]) -> int:
    status = 0
    for marker, failures in results.items():
        if failures:
            status = 1
            print(f"FAILED {marker}: {len(failures)}")
            for failure in failures[:20]:
                print(f"  {failure}")
        else:
            print(marker)
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corpus")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--model-revision", required=True)
    b.add_argument("--workers", type=int)
    v = sub.add_parser("verify")
    v.add_argument("--corpus", type=Path, required=True)
    v.add_argument("--no-replay", action="store_true")
    v.add_argument("--workers", type=int)
    s = sub.add_parser("scope")
    s.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        manifest = build(
            args.out, Config(model_revision=args.model_revision), workers=args.workers
        )
        print(json.dumps(manifest["partitions"], sort_keys=True))
        print(json.dumps(manifest["excluded_families"], sort_keys=True))
        return 0
    if args.command == "verify":
        return _report(
            verify(args.corpus, replay=not args.no_replay, workers=args.workers)
        )
    failures = scope(args.baseline)
    for failure in failures:
        print(failure)
    print("SCOPE_OK" if not failures else f"SCOPE_FAILED {len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
