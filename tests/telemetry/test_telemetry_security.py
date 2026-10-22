"""Security properties of the telemetry evidence path. Written first. Offline.

This leaf is the one that touches deployment identity, so it is the one worth
scanning. Five properties are checked by reading the code and the artifacts
rather than by trusting the docstrings.

No module in this leaf reaches the network or the ambient process. Not an AWS
SDK, not a socket, not an HTTP client, not `os.environ`, not `os.getenv`, not a
credential file. The import graph is checked directly, so a future edit that
adds one fails here.

No module in this leaf spells a deployment value. Account, Region, ARN, model
and log group are the owner's deployment inputs, and a constant would make the
code work on exactly one deployment while publishing someone's topology on
every other.

No evaluator vocabulary reaches the shipped package through this leaf. The
truth reader lives outside the distribution on purpose, and a telemetry module
that merely named it would undo that.

No public artifact carries a credential marker, an authority marker, an
account, a principal ARN, a local path, or an evaluator reference. The public
bundle and the publication record are both scanned as serialized text, which is
the form a human would actually read.

And the documentation is held to the same standard as the code: it states the
variable names and the shapes, and it names no deployment.

Focused command:
UV_CACHE_DIR="$PWD/.venv/uv-cache" uv run pytest tests/telemetry/test_telemetry_security.py -q
"""

import ast
import re
from pathlib import Path

import pytest

from publishing.telemetry import validate_public_bundle
from tests.telemetry.placeholders import (
    MODEL_REVISION_SENTINEL,
    MODEL_SENTINEL,
    SOURCE_LABEL_SENTINEL,
    STAGE_SENTINEL,
)
from tests.telemetry.test_telemetry_bundle import bundle
from tests.telemetry.test_telemetry_evidence_binding import sentinel_bundle
from unstuck.telemetry.bundle import hackathon_export
from unstuck.telemetry.runtime import FORBIDDEN_VARIABLES

#: Deployment identity that used to survive public projection: the stage, the
#: model on a span and on an invocation, and an operator's export filename.
IDENTITY_SENTINELS = (
    STAGE_SENTINEL,
    MODEL_SENTINEL,
    MODEL_REVISION_SENTINEL,
    SOURCE_LABEL_SENTINEL,
)

#: Every file this leaf owns, source and documentation alike.
OWNED_MODULES = (
    Path("src/unstuck/cloud/telemetry.py"),
    Path("src/unstuck/telemetry/runtime.py"),
    Path("src/unstuck/telemetry/sanitizer.py"),
    Path("src/unstuck/telemetry/signals.py"),
    Path("src/unstuck/telemetry/bundle.py"),
    Path("publishing/telemetry.py"),
)
OWNED_DOCS = (Path("docs/telemetry.md"),)

#: Modules that would make this leaf reach outside the process. `socket` and
#: `ssl` are here because an exporter is the obvious thing a later edit adds.
FORBIDDEN_IMPORTS = frozenset(
    {
        "boto3",
        "botocore",
        "requests",
        "httpx",
        "urllib",
        "urllib3",
        "http",
        "socket",
        "ssl",
        "aiohttp",
        "opentelemetry",
        "aws_cdk",
        "subprocess",
    }
)

#: Calls that would read or write the ambient process. The projection is a
#: typed map a deployment applies; nothing here applies it.
FORBIDDEN_CALLS = ("getenv", "putenv", "environb", "expandvars", "load_dotenv")

#: Markers that must never appear in a public artifact.
SENTINEL_MARKERS = (
    "authorization",
    "secret",
    "password",
    "api_key",
    "api-key",
    "credential",
    "bearer ",
    "evaluator",
    "hidden_cause",
    "hidden category",
    "expected_rows",
    "expected_action",
    "arn:",
    "/users/",
    "/home/",
    "/var/folders/",
)

#: Vocabulary the shipped package must not learn from this leaf.
TRUTH_VOCABULARY = ("TruthHandle", "hidden_cause", "expected_rows", "holdout_bucket")

ACCOUNT_FORM = re.compile(r"(?<!\d)\d{12}(?!\d)")
REGION_FORM = re.compile(r"\b[a-z]{2}(?:-[a-z]+)+-\d\b")
ARN_FORM = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:")
LOG_GROUP_FORM = re.compile(r"/aws/[a-z0-9-]+/[a-z0-9-]+")


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("relative", OWNED_MODULES, ids=lambda item: item.name)
def test_no_module_reaches_the_network_or_an_sdk(relative: Path) -> None:
    assert not imports_of(relative) & FORBIDDEN_IMPORTS


@pytest.mark.parametrize("relative", OWNED_MODULES, ids=lambda item: item.name)
def test_no_module_reads_or_writes_the_ambient_process(relative: Path) -> None:
    body = relative.read_text(encoding="utf-8")
    tree = ast.parse(body)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_CALLS, f"{relative} calls {node.attr}"
            assert node.attr != "environ", f"{relative} reads os.environ"
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_CALLS, f"{relative} calls {node.id}"


@pytest.mark.parametrize("relative", OWNED_MODULES, ids=lambda item: item.name)
def test_no_module_spells_a_deployment_value(relative: Path) -> None:
    body = relative.read_text(encoding="utf-8")
    for name, pattern in (
        ("account", ACCOUNT_FORM),
        ("region", REGION_FORM),
        ("arn", ARN_FORM),
        ("log group", LOG_GROUP_FORM),
    ):
        found = pattern.search(body)
        assert found is None, f"{relative} spells a {name}: {found.group()}"


@pytest.mark.parametrize("relative", OWNED_DOCS, ids=lambda item: item.name)
def test_the_documentation_names_no_deployment(relative: Path) -> None:
    body = relative.read_text(encoding="utf-8")
    for name, pattern in (("account", ACCOUNT_FORM), ("arn", ARN_FORM)):
        found = pattern.search(body)
        assert found is None, f"{relative} spells a {name}: {found.group()}"
    # A Region or log-group shape is allowed only as a visible placeholder.
    for pattern in (REGION_FORM, LOG_GROUP_FORM):
        for found in pattern.finditer(body):
            line = (
                body[: found.start()].rsplit("\n", 1)[-1]
                + body[found.start() :].split("\n", 1)[0]
            )
            assert "<" in line, f"{relative} spells a deployment value: {line}"


@pytest.mark.parametrize("relative", OWNED_MODULES, ids=lambda item: item.name)
def test_no_shipped_module_learns_the_truth_vocabulary(relative: Path) -> None:
    if not str(relative).startswith("src/"):
        return
    body = relative.read_text(encoding="utf-8")
    for marker in TRUTH_VOCABULARY:
        assert marker not in body, f"{relative} names {marker}"


def test_the_credential_variables_are_named_only_to_be_refused() -> None:
    body = Path("src/unstuck/telemetry/runtime.py").read_text(encoding="utf-8")
    for name in FORBIDDEN_VARIABLES:
        if not name.startswith("AWS_"):
            continue
        # Each appears exactly once, inside the refusal set.
        assert body.count(name) == 1, f"{name} appears outside the refusal set"


def test_the_public_bundle_carries_no_sentinel_marker() -> None:
    body = hackathon_export(bundle()).model_dump_json().lower()
    for marker in SENTINEL_MARKERS:
        assert marker not in body, f"the public bundle carries {marker}"


def test_the_publication_record_carries_no_sentinel_marker() -> None:
    record = validate_public_bundle(
        hackathon_export(bundle()), subject="telemetry-evidence-bundle"
    )
    body = record.model_dump_json().lower()
    for marker in SENTINEL_MARKERS:
        assert marker not in body, f"the publication record carries {marker}"


def test_security_no_identity_sentinel_reaches_the_publication_record() -> None:
    """Regression: stage, model and filename identity reached the public path.

    The scan is on the serialized record, which is the form a human reads, and
    each sentinel is unique so a leak names the field it escaped from.
    """
    exported = hackathon_export(sentinel_bundle())
    record = validate_public_bundle(exported, subject="telemetry-evidence-bundle")
    for body in (exported.model_dump_json(), record.model_dump_json()):
        for sentinel in IDENTITY_SENTINELS:
            assert sentinel not in body, f"a public artifact carries {sentinel}"


def test_the_projected_variables_carry_no_sentinel_marker() -> None:
    from tests.telemetry.test_telemetry_runtime_config import full

    for name, value in full().configured().variables.mapping.items():
        assert name not in FORBIDDEN_VARIABLES
        lowered = value.lower()
        for marker in ("authorization", "secret", "credential", "bearer ", "arn:"):
            assert marker not in lowered, f"{name} carries {marker}"
