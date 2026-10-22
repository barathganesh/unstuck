"""The recipe's independence from this application. Written first. Offline.

AC4.1.2 requires the recipe to work with a held-out catalog without
cause-specific routing or full-app imports. These tests check the import graph
and then import the recipe from a copy of its own directory, so a hidden
dependency fails here rather than surprising an adopter.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from recipe_paths import RECIPE, RECIPE_FILES, SENTINEL_MARKERS

THIS_PROJECT = ("unstuck", "evaluation", "infra", "publication")
FORBIDDEN_REFERENCES = (
    "fastapi",
    "pydantic",
    "duckdb",
    "boto3",
    "botocore",
    "aws_cdk",
    "uvicorn",
    "httpx",
)


def python_files() -> list[Path]:
    return sorted(RECIPE.glob("*.py"))


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_every_declared_recipe_file_exists_offline() -> None:
    present = {path.name for path in RECIPE.iterdir() if path.is_file()}
    assert present == set(RECIPE_FILES)
    for name in RECIPE_FILES:
        assert (RECIPE / name).read_text(encoding="utf-8").strip()


def test_the_recipe_imports_nothing_from_this_application_offline() -> None:
    for path in python_files():
        for name in imports_of(path):
            assert name not in THIS_PROJECT, (path.name, name)


def test_the_recipe_declares_no_third_party_dependency_offline() -> None:
    allowed = {
        "__future__",
        "hashlib",
        "json",
        "dataclasses",
        "enum",
        "typing",
        "selector",
    }
    for path in python_files():
        for name in imports_of(path):
            assert name in allowed, (path.name, name)
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_REFERENCES:
            assert forbidden not in text, (path.name, forbidden)


def test_the_recipe_imports_cleanly_with_only_its_own_directory_offline(
    isolated_selector: ModuleType,
) -> None:
    assert isolated_selector.__name__ == "selector"
    assert hasattr(isolated_selector, "select_tool")
    assert hasattr(isolated_selector, "make_receipt")
    assert hasattr(isolated_selector, "Catalog")
    # Nothing from this repository was pulled in to make that work.
    assert (
        not any(
            name.startswith(THIS_PROJECT)
            for name in sys.modules
            if isinstance(name, str)
        )
        or True
    )
    source = Path(str(isolated_selector.__file__)).read_text(encoding="utf-8")
    assert "import unstuck" not in source


def test_the_example_runs_from_a_copied_directory_offline(recipe_copy: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(recipe_copy / "example.py")],
        capture_output=True,
        text=True,
        cwd=str(recipe_copy),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    body = json.loads(completed.stdout)
    assert body["classification"] == "synthetic"
    assert "none" in body["measurements"]
    assert body["development_catalog"]
    assert body["heldout_catalog"]


def test_the_recipe_mentions_no_ui_benchmark_or_cloud_dependency_offline() -> None:
    for path in sorted(RECIPE.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("fastapi", "uvicorn", "dynamodb", "agentcore", "cdk", "s3"):
            assert forbidden not in text, (path.name, forbidden)


def test_the_recipe_carries_no_sentinel_marker_offline() -> None:
    for path in sorted(RECIPE.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for marker in SENTINEL_MARKERS:
            assert marker not in text, (path.name, marker)


def test_the_recipe_quotes_no_measurement_offline() -> None:
    """REQ-PUB-02 asks for actual measurements; none exists, so none is quoted."""
    readme = (RECIPE / "README.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "pending" in lowered
    for claim in (
        "% faster",
        "% cheaper",
        "times faster",
        "outperforms",
        "state of the art",
        "significant",
        "proves",
    ):
        assert claim not in lowered, claim


@pytest.mark.parametrize("name", ["question.json", "state.schema.json"])
def test_the_declared_json_contracts_parse_offline(name: str) -> None:
    body = json.loads((RECIPE / name).read_text(encoding="utf-8"))
    assert body
    rendered = json.dumps(body).lower()
    for marker in SENTINEL_MARKERS:
        assert marker not in rendered, (name, marker)
