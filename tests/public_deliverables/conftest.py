# Unit command: UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/public_deliverables -q
"""Offline fixtures for the public-deliverables unit.

The recipe is imported from a copy of its own directory with nothing else on
the path, so a hidden dependency on this repository fails a test rather than
passing silently. No test publishes, uploads, pushes, or opens a socket.
"""

import importlib
import shutil
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from recipe_paths import RECIPE, RECIPE_FILES, REPO, SENTINEL_MARKERS

__all__ = ["RECIPE", "RECIPE_FILES", "REPO", "SENTINEL_MARKERS"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network is forbidden in public-deliverables tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "bind", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def recipe_copy(tmp_path: Path) -> Path:
    """A copy of the recipe directory, alone."""
    target = tmp_path / "recipe"
    target.mkdir()
    for name in RECIPE_FILES:
        shutil.copy(RECIPE / name, target / name)
    return target


@pytest.fixture
def isolated_selector(recipe_copy: Path) -> Iterator[ModuleType]:
    """Import the recipe with ONLY its own directory importable.

    This is the independence check with teeth: if `selector.py` reached for
    anything in this repository, the import would fail here.
    """
    saved_path = list(sys.path)
    saved_modules = dict(sys.modules)
    stdlib = [entry for entry in sys.path if "site-packages" not in entry]
    sys.path = [str(recipe_copy), *[item for item in stdlib if item not in {"", "."}]]
    for name in ("selector", "example"):
        sys.modules.pop(name, None)
    try:
        module = importlib.import_module("selector")
        yield module
    finally:
        sys.path = saved_path
        sys.modules.clear()
        sys.modules.update(saved_modules)
