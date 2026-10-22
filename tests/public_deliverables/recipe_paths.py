"""Shared locations and sentinels for the public-deliverables suite.

These live in a uniquely named module rather than in `conftest.py` because a
test module that imports `conftest` by name resolves whichever suite's
`conftest` happens to be first on the path, which depends on the order the
suites were collected in. A unique name is the same value from every run.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "recipes" / "diagnostic-tool-selection"
RECIPE_FILES = (
    "question.json",
    "state.schema.json",
    "selector.py",
    "example.py",
    "README.md",
)

SENTINEL_MARKERS = (
    "authorization",
    "secret",
    "password",
    "api_key",
    "credential",
    "bearer ",
    "evaluator",
    "kappa-",
    "expected_rows",
    "hidden_category",
    "jev",
    "bma",
)

__all__ = ["RECIPE", "RECIPE_FILES", "REPO", "SENTINEL_MARKERS"]
