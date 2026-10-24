"""Cheap contracts: the config templates, the frozen assignment, identities, labels.

Nothing here builds a corpus. The label tests run a handful of real controller
calls each.
"""

import ast
import json
import os
import re
from pathlib import Path

import pytest
import yaml

from experiments.decider_finetune.identity import checkpoint_digest, prompt_digest
from experiments.decider_finetune.oracle import (
    Config,
    label_after,
    label_fresh,
    pinned_identities,
)
from experiments.decider_finetune.scenarios import (
    ASSIGNMENT,
    AUDITED,
    CATALOGS,
    PARTITION_CATALOGS,
    Scenario,
)
from unstuck.domain import EvidenceRecord

HERE = Path("experiments/decider_finetune")
#: An upstream trainer checkout, named only through the environment so no
#: machine-local path is written into the tree.
_UPSTREAM = os.environ.get("STRANDS_DECIDER_UPSTREAM", "")
UPSTREAM = Path(_UPSTREAM) if _UPSTREAM else None
#: `training/train.py` TrainConfig fields at upstream commit d942e551.
TRAIN_CONFIG_FIELDS = frozenset(
    "train_files val_files val_fraction val_split_seed base_model num_slots "
    "head_hidden head_dropout max_length use_lora lora_r lora_alpha lora_targets "
    "force_bos host_embeddings freeze_torso head_type pointer_dim head_init "
    "kl_frozen_weight kl_frozen_reference kl_only_files kl_only_weight teacher_file "
    "teacher_weight kl_direction init_from epochs micro_batch_size grad_accum lr "
    "head_lr weight_decay warmup_ratio max_grad_norm ema_decay gradient_checkpointing "
    "shuffle_options group_by_length length_group_mega ordinal_smoothing "
    "reverse_score_prob output_dir seed log_every eval_every save_every max_steps "
    "attn_implementation precompute_frozen_kl".split()
)
#: `INIT_FROM_ARCH`: the keys a checkpoint fixes and a config must omit.
INIT_FROM_ARCH = frozenset(
    "base_model num_slots head_type pointer_dim head_hidden use_lora lora_r "
    "lora_alpha lora_targets force_bos".split()
)
CONFIGS = ("head-only.yaml", "lora-head-fallback.yaml")


def load(name: str) -> dict[str, object]:
    return dict(yaml.safe_load((HERE / "configs" / name).read_text()))


@pytest.mark.skipif(
    UPSTREAM is None or not UPSTREAM.is_dir(),
    reason="set STRANDS_DECIDER_UPSTREAM to an upstream checkout",
)
def test_config_field_lists_match_the_pinned_upstream_trainer():
    assert UPSTREAM is not None
    tree = ast.parse((UPSTREAM / "training/train.py").read_text())
    config = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TrainConfig"
    )
    fields = {s.target.id for s in config.body if isinstance(s, ast.AnnAssign)}  # type: ignore[union-attr]
    arch = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and getattr(n.targets[0], "id", "") == "INIT_FROM_ARCH"
    )
    assert fields == TRAIN_CONFIG_FIELDS
    assert set(ast.literal_eval(arch.value)) == INIT_FROM_ARCH


@pytest.mark.parametrize("name", CONFIGS)
def test_config_is_an_official_init_from_config(name):
    config = load(name)
    assert set(config) <= TRAIN_CONFIG_FIELDS
    assert not set(config) & INIT_FROM_ARCH
    assert "max_length" not in config
    assert config["init_from"] == config["kl_frozen_reference"]
    # init_from is the public open-model release the documented download names.
    template = (HERE / "configs/head-only.yaml").read_text()
    download = re.search(r"--revision \S+ --local-dir (\S+)", template)
    assert download is not None and download.group(1) == config["init_from"]
    assert float(config["kl_frozen_weight"]) > 0  # type: ignore[arg-type]
    assert config["precompute_frozen_kl"] is True and config["group_by_length"] is True
    assert config["shuffle_options"] is True and config["head_init"] == "random"
    assert isinstance(config["seed"], int) and config["max_steps"] == 0
    assert config["train_files"] == ["data/unstuck/train.jsonl"]
    assert "val_files" not in config and "teacher_file" not in config


def test_config_head_only_freezes_the_torso_and_the_fallback_is_documented_only():
    assert load("head-only.yaml")["freeze_torso"] is True
    assert load("lora-head-fallback.yaml")["freeze_torso"] is False
    text = (HERE / "configs/lora-head-fallback.yaml").read_text()
    assert text.startswith("# FALLBACK, DOCUMENTED ONLY. Never run automatically")
    sources = list(HERE.rglob("*.py"))
    referencing = sorted(
        p.name for p in sources if "lora-head-fallback" in p.read_text()
    )
    # Hashed into the freeze record so it cannot change unnoticed; never launched.
    assert referencing == ["evaluate.py"]
    assert not any(
        word in p.read_text()
        for p in sources
        for word in ("subprocess", "os.system", "training.train")
    )


def test_config_calibration_command_reads_only_the_calibration_partition():
    readme = (HERE / "README.md").read_text()
    assert "calibration.jsonl --split all" in readme
    assert not [
        line for line in readme.splitlines() if "calibrate " in line and "test." in line
    ]
    for name in CONFIGS:
        text = (HERE / "configs" / name).read_text()
        assert "test.jsonl" not in text


def test_assignment_is_disjoint_and_catalogs_never_cross_partitions():
    families = [f for group in ASSIGNMENT.values() for f in group]
    assert len(families) == len(set(families))
    assert not set(families) & set(AUDITED)
    renamed = {
        p: set(names) - {"production"} for p, names in PARTITION_CATALOGS.items()
    }
    assert not renamed["train"] & renamed["calibration"]
    assert not renamed["train"] & renamed["test"]
    assert not renamed["calibration"] & renamed["test"]
    surfaces = [
        (e.public_id, e.description, e.applies_when)
        for c in CATALOGS.values()
        for e in c.entries
    ]
    for column in zip(*surfaces, strict=True):
        assert len(set(column)) == len(column)


def test_scenario_identity_encodes_no_family():
    a = Scenario("train", "empty_plain", 0, "production")
    b = Scenario("train", "empty_plain", 1, "production")
    assert a.fixture().dataset_id != b.fixture().dataset_id
    assert "empty" not in a.fixture().dataset_id and "empty" not in a.opaque


def test_prompt_identity_is_shared_with_the_isolated_runner():
    digest = prompt_digest("q", "s", [("a", "x"), ("b", "y")])
    assert digest == prompt_digest("q", "s", [["a", "x"], ["b", "y"]])
    assert digest != prompt_digest("q", "s", [("b", "y"), ("a", "x")])


def test_checkpoint_identity_changes_with_any_byte(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "head.safetensors").write_text("1")
    first, files = checkpoint_digest(tmp_path / "a")
    (tmp_path / "a" / "head.safetensors").write_text("2")
    assert checkpoint_digest(tmp_path / "a")[0] != first and list(files) == [
        "head.safetensors"
    ]


def test_pinned_identities_are_restored_after_the_call():
    with pinned_identities("scope"):
        pinned = EvidenceRecord.model_fields["evidence_id"].default_factory
    import unstuck.cascade.engine as engine

    restored = EvidenceRecord.model_fields["evidence_id"].default_factory
    assert pinned is not restored and engine.uuid4.__module__ == "uuid"  # type: ignore[attr-defined]


CONFIG = Config(model_revision="scripted-v1")


@pytest.mark.parametrize(
    ("family", "capability"),
    [
        ("arrival_pending_plain", "inspect_pipeline"),
        ("key_drift_dimension", "inspect_schema_keys"),
        ("empty_drift_fact", "inspect_schema_keys"),
    ],
)
def test_label_comes_from_executing_every_candidate(family, capability):
    scenario = Scenario("train", family, 0, "r2")
    labeled = label_fresh(scenario, 1, CONFIG)
    assert labeled is not None
    assert [CATALOGS["r2"].capability(p) for p in labeled.productive] == [capability]
    others = [o for o in labeled.outcomes if not o.productive]
    assert len(others) == 2 and all(
        o.first_read_outcome == "disposition_unchanged" for o in others
    )
    state = json.loads(labeled.captured.prompt.state_text)
    assert state["history"]["status"] == "empty" and state["pass_index"] == 1


def test_label_after_a_dead_end_precludes_it_before_the_first_decision():
    scenario = Scenario("train", "key_drift_dimension", 0, "production")
    result = label_after(scenario, "inspect_pipeline", CONFIG)
    assert result is not None
    first, labeled = result
    assert first.reason_code == "diagnostic_reads_exhausted"
    assert labeled.captured.precluded_tool_ids == ("inspect_pipeline",)
    assert labeled.captured.history_status == "matched"
    assert labeled.productive == ("inspect_schema_keys",)


def test_audited_twin_is_unlabelable_because_its_schema_read_is_a_false_repair():
    labeled = label_fresh(
        Scenario("audit", "pending_key_drift", 0, "production"), 1, CONFIG
    )
    assert labeled is not None
    by_capability = {o.capability: o for o in labeled.outcomes}
    assert by_capability["inspect_schema_keys"].false_repair
    assert labeled.productive == ("inspect_pipeline",)


def test_generation_is_deterministic_for_one_case():
    scenario = Scenario("test", "key_drift_fact", 0, "production")
    a = label_fresh(scenario, 2, CONFIG)
    b = label_fresh(scenario, 2, CONFIG)
    assert a is not None and b is not None and a.captured.wire == b.captured.wire
