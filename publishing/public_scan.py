"""Fail-closed leakage scan over every public file.

The policy this enforces is ``docs/public-scrub-policy.md``. Two things must
hold for the scan to pass:

1. Every pattern must catch its own synthetic positive control. A pattern that
   has stopped matching is a broken check, not a clean repository, so a failed
   control fails the scan.
2. No public file may produce a finding.

Three public artifacts are not plain text and are covered by a derived reading
rather than by a line scan:

* ``submission/pitch/unstuck-pitch.pptx`` is a zip of XML parts. Its
  inspectable text (the slide text runs, the image alt text, the speaker notes
  and the document properties) is extracted from those parts and scanned. The
  embedded frame images are not text and are covered by the frame check below.
* ``submission/pitch/frames/slide-01.png`` to ``slide-08.png`` carry rendered
  text. They are read back with optical character recognition when a recognizer
  is installed, and the recognized text is scanned.
* ``docs/architecture.svg`` is already text and is scanned as text.

Nothing here sends, uploads, publishes or submits anything. It reads files and
returns verdicts.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

PUBLIC_FILES: tuple[str, ...] = (
    "README.md",
    "docs/article.md",
    "docs/video-script.md",
    "docs/demo.html",
    "docs/architecture.svg",
    "docs/claim-register.md",
    "docs/submission-checklist.md",
    "docs/hackathon-validation.md",
    "docs/public-scrub-policy.md",
    "dist/public-demo/fixture.json",
    "dist/public-demo/timeline.json",
    "dist/public-demo/final-golden-summary.json",
    "dist/public-demo/loop-escape-proof.json",
    "recipes/diagnostic-tool-selection/README.md",
    "recipes/diagnostic-tool-selection/selector.py",
    "recipes/diagnostic-tool-selection/example.py",
    "recipes/diagnostic-tool-selection/question.json",
    "recipes/diagnostic-tool-selection/state.schema.json",
    "dist/video/README.md",
    "dist/video/validation.json",
    "dist/video/unstuck-demo.srt",
    "submission/video/README.md",
    "submission/video/edit-plan.json",
    "submission/video/build.py",
    "submission/video/control.svg",
    "submission/video/run-two.svg",
    "submission/video/ml-process.svg",
    "submission/video/ml-results.svg",
    "submission/video/credit.svg",
    "submission/video/proof.svg",
    "submission/video/aws.svg",
    "submission/video/recipe.svg",
    "submission/video/workflow-1-1.svg",
    "submission/video/workflow-1-2.svg",
    "submission/video/workflow-1-3.svg",
    "submission/video/workflow-1-4.svg",
    "submission/video/workflow-2-1.svg",
    "submission/video/workflow-2-2.svg",
    "submission/video/workflow-2-3.svg",
    "submission/video/workflow-2-4.svg",
    "submission/video/terminal-1.svg",
    "submission/video/terminal-2.svg",
    "submission/video/replay.py",
    "submission/video/intro.svg",
    "submission/video/agent-flow-1.svg",
    "submission/video/agent-flow-2.svg",
    "submission/video/agent-flow-3.svg",
    "submission/video/agent-flow-4.svg",
    "submission/video/code-aws.svg",
    "submission/video/code-choice.svg",
    "submission/video/code-receipt.svg",
    "submission/video/agent-result-1.svg",
    "submission/video/agent-result-2.svg",
    "submission/video/agent-run-1-0.svg",
    "submission/video/agent-run-1-1.svg",
    "submission/video/agent-run-1-2.svg",
    "submission/video/agent-run-1-3.svg",
    "submission/video/agent-run-1-4.svg",
    "submission/video/agent-run-1-5.svg",
    "submission/video/agent-run-1-6.svg",
    "submission/video/agent-run-1-7.svg",
    "submission/video/agent-run-1-8.svg",
    "submission/video/agent-run-1-9.svg",
    "submission/video/agent-run-2-0.svg",
    "submission/video/agent-run-2-1.svg",
    "submission/video/agent-run-2-10.svg",
    "submission/video/agent-run-2-11.svg",
    "submission/video/agent-run-2-12.svg",
    "submission/video/agent-run-2-13.svg",
    "submission/video/agent-run-2-2.svg",
    "submission/video/agent-run-2-3.svg",
    "submission/video/agent-run-2-4.svg",
    "submission/video/agent-run-2-5.svg",
    "submission/video/agent-run-2-6.svg",
    "submission/video/agent-run-2-7.svg",
    "submission/video/agent-run-2-8.svg",
    "submission/video/agent-run-2-9.svg",
    "submission/video/agent-run-training-0.svg",
    "submission/video/agent-run-training-1.svg",
    "submission/video/agent-run-training-2.svg",
    "submission/video/agent-run-training-3.svg",
    "submission/video/agent-run-training-4.svg",
    "submission/video/agent-run-training-5.svg",
    "submission/video/agent-run-training-6.svg",
    "submission/video/agent-run-training-7.svg",
    "submission/video/agent-run-training-8.svg",
    "submission/video/agent-run-training-9.svg",
    # The pitch deck ships with the submission, so every text file under it is
    # public. The deck HTML is the single source for the slide text, the alt
    # text and the speaker notes; the two build scripts and the package
    # manifest are the local tooling that renders it.
    "submission/pitch/README.md",
    "submission/pitch/unstuck-pitch.html",
    "submission/pitch/build/capture.mjs",
    "submission/pitch/build/make-pptx.mjs",
    "submission/pitch/build/package.json",
)

# The PowerPoint file and the frames are the same deck in non-text form.
PITCH_PPTX: str = "submission/pitch/unstuck-pitch.pptx"
PITCH_FRAMES: tuple[str, ...] = tuple(
    f"submission/pitch/frames/slide-{index:02d}.png" for index in range(1, 9)
)

# The policy file and the scan's own documentation name the forbidden patterns
# in order to forbid them, so they are exempt from the patterns they describe.
POLICY_FILES: frozenset[str] = frozenset({"docs/public-scrub-policy.md"})

#: This module's own content IS the denylist, so it cannot be scanned against it.
SCAN_SOURCE: str = "publishing/public_scan.py"


@dataclass(frozen=True)
class Check:
    """One forbidden pattern, its positive control, and its allowed exceptions."""

    name: str
    category: str
    pattern: str
    positive_control: str
    """A synthetic example that MUST match. Never a real value from any run."""
    negative_control: str = ""
    """An example that must NOT match, where a pattern is easy to over-broaden."""
    exempt_files: frozenset[str] = frozenset()
    allow: tuple[str, ...] = ()
    """Substrings that make a matched line acceptable."""
    flags: int = 0

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, self.flags)


# Generic field names the reusable recipe's public API contract requires. They
# are allowed to appear there, and the placeholder-value check below is what
# keeps them from carrying a real value.
RECIPE_FILES = frozenset(
    {
        "recipes/diagnostic-tool-selection/README.md",
        "recipes/diagnostic-tool-selection/selector.py",
        "recipes/diagnostic-tool-selection/example.py",
        "recipes/diagnostic-tool-selection/question.json",
        "recipes/diagnostic-tool-selection/state.schema.json",
    }
)

CHECKS: tuple[Check, ...] = (
    Check(
        name="benchmark-arm-label",
        category="benchmark arm identity",
        pattern=r"\bdecider_memory\b|[\"'`]\s*(fixed|llm)\s*[\"'`]\s*(?:,|\]|\}|:)|\barm_id\b|\bbaseline_id\b",
        positive_control='{"baseline_id": "decider_memory", "arm_id": "fixed"}',
        negative_control="the result is already fixed before narration runs",
    ),
    Check(
        name="benchmark-arm-field",
        category="benchmark arm identity",
        pattern=r"[\"']arm[\"']\s*:|\barms\s*[:=]\s*\d|\bthe (fixed|llm|decider_memory) arm\b",
        positive_control='"arm": "decider_memory"',
        negative_control="the without receipts comparison repeated the read",
    ),
    Check(
        name="hex-digest",
        category="hashes and digests",
        pattern=r"\b[0-9a-f]{16,}\b",
        positive_control="deadbeefdeadbeefdeadbeef0123456789abcdef0123456789abcdef01234567",
        negative_control="confidence 0.3067 over 40 real selector calls",
    ),
    Check(
        name="truncated-digest",
        category="hashes and digests",
        pattern=r"\b[0-9a-f]{8}(?:&hellip;|…|\.\.\.)",
        positive_control="abcd1234&hellip;",
        negative_control="and so on ...",
    ),
    Check(
        name="uuid",
        category="operational and correlation identity",
        pattern=r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        positive_control="00000000-1111-2222-3333-444444444444",
        negative_control="2026-10-16 to 2026-10-23",
    ),
    Check(
        name="identifier-field",
        category="operational and correlation identity",
        pattern=(
            r"\b(run_id|request_id|session_id|trace_id|span_id|correlation_id|model_call_id"
            r"|decision_id|diagnostic_call_id|evidence_id|evidence_ids|verification_id"
            r"|incident_id|case_id|case_id_hash|event_id|receipt_id|receipt_ids"
            r"|execution_key|idempotency_key|identity_hash)\b"
        ),
        positive_control='"run_id": "...", "receipt_ids": []',
        negative_control="a receipt is identified by the content of its binding set",
        exempt_files=RECIPE_FILES,
    ),
    Check(
        name="hash-field",
        category="hashes and digests",
        pattern=(
            r"\b(query_hash|base_query_hash|task_hash|context_hash|decision_context_hash"
            r"|selector_input_hash|input_hash|evidence_before_hash|evidence_after_hash"
            r"|disposition_before_hash|disposition_after_hash|versions_hash|policy_hash"
            r"|calibration_hash|namespace_hash|row_digest|problem_fingerprint"
            r"|model_revision_hash|arguments_hash)\b"
        ),
        positive_control='"model_revision_hash": "..."',
        negative_control="the pinned revision is not published, raw or digested",
        exempt_files=RECIPE_FILES,
    ),
    Check(
        name="private-run-label",
        category="operational and correlation identity",
        pattern=r"\brun-[A-Z]\b|\bcall-\d\b|\bcall-\d-duplicate\b",
        positive_control="run-A and call-2-duplicate",
        negative_control="call 2 is a genuinely new signed call",
    ),
    Check(
        name="aws-account-id",
        category="cloud account and resource identity",
        pattern=r"(?<![\d.])\d{12}(?![\d.])",
        positive_control="account 123456789012 owns the endpoint",
        negative_control="a bound of 20000 ms, unchanged",
    ),
    Check(
        name="aws-arn",
        category="cloud account and resource identity",
        pattern=r"\barn:aws[a-z-]*:",
        positive_control="arn:aws:iam::123456789012:role/Example",
        negative_control="Amazon SageMaker serves the checkpoint privately",
    ),
    Check(
        name="aws-resource-id",
        category="cloud account and resource identity",
        pattern=r"\b(vpc|subnet|sg|eni|rtb|igw|nat|vpce|ami|snap|vol|acct)-[0-9a-f]{8,}\b",
        positive_control="subnet-0123456789abcdef0 and sg-00112233445566778",
        negative_control="private subnets with network isolation on",
    ),
    Check(
        name="credential-pattern",
        category="people, credentials and truth",
        pattern=(
            r"\b(AKIA|ASIA)[0-9A-Z]{8,}\b|aws_secret_access_key|aws_session_token"
            r"|\bBearer\s+[A-Za-z0-9._-]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        ),
        positive_control="AKIAIOSFODNN7EXAMPLE",
        negative_control="no bearer token and no custom credential anywhere",
    ),
    Check(
        name="absolute-local-path",
        category="paths and locations",
        pattern=r"(?<![\w.])/(Users|home|private|tmp|var/folders)/|\b[A-Za-z]:\\\\",
        positive_control="/Users/someone/Documents/private/LEDGER.md",
        negative_control="artifacts land under a declared repository root",
    ),
    Check(
        name="private-venv-path",
        category="paths and locations",
        pattern=r"\.venv/",
        positive_control="evidence sits at .venv/private-run/LEDGER.md",
        negative_control="uv venv --python 3.12",
    ),
    Check(
        name="private-evidence-reference",
        category="paths and locations",
        pattern=(
            r"\bthe private ledger\b|\bLEDGER\.md\b|agentcore-window-telemetry"
            r"|telemetry-verification-v\d|unsigned-denial-v\d|responses-v\d/|inputs-v\d/"
        ),
        positive_control="recorded in the private ledger at LEDGER.md",
        negative_control="the private cloud-run record (not published)",
    ),
    Check(
        name="run-identifying-timestamp",
        category="time that identifies the private run",
        pattern=r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}",
        positive_control="2026-10-21T23:02:29.004317Z",
        negative_control="October 16 2026 to October 23 2026",
        # The recipe's examples need a syntactically real instant to run. The
        # one neutral placeholder below is allowed; no other instant is.
        allow=("your-", "example", "placeholder", "2026-01-01T00:00:00Z"),
    ),
    Check(
        name="process-gate-identifier",
        category="internal process identity",
        pattern=r"\bPF[1-9]\b|\bGM\d+\b|\bTST-[A-Z0-9][A-Z0-9.-]*\b|\bREQ-[A-Z0-9][A-Z0-9.-]*\b",
        positive_control="PF7 and REQ-PUB-03 and TST-04 and GM2",
        negative_control="the provider-boundary gate is still Pending",
    ),
    Check(
        name="requirement-code",
        category="internal process identity",
        pattern=r"\b(AC\d+\.\d+\.\d+|FR\d+\.\d+|NFR\d+\.\d+)\b",
        positive_control="AC4.1.2 and FR12.7 and NFR8.1",
        negative_control="dependency licence verification is an owner action",
    ),
    Check(
        name="checkpoint-revision-suffix",
        category="hashes and digests",
        pattern=r"strands-decider-2B-[a-z0-9-]+",
        positive_control="strands-decider-2B-example-v99",
        negative_control="Strands Decider 2B",
    ),
    Check(
        name="dataset-version-label",
        category="operational and correlation identity",
        pattern=r"\bv1[0-9]\b|\bds-\d+\b|dataset_version",
        positive_control='dataset_version "v17" over ds-17',
        negative_control="one declared synthetic fixture",
    ),
    Check(
        name="non-inclusive-term",
        category="style",
        pattern=r"\b(whitelist|blacklist|whiteday|blackday)\b|\b(master|slave)\b",
        positive_control="the whitelist is on the master node",
        negative_control="an allowlist of read-only diagnostics",
        flags=re.IGNORECASE,
    ),
    Check(
        name="em-dash",
        category="style",
        pattern="—",
        positive_control="a receipt — unlike a cache — refuses once",
        negative_control="a receipt, unlike a cache, refuses once",
    ),
    Check(
        name="email-address",
        category="people, credentials and truth",
        pattern=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        positive_control="someone@example.com",
        negative_control="contact the owner through the repository",
    ),
    Check(
        name="evaluator-truth-field",
        category="people, credentials and truth",
        pattern=r"\b(expected_answer|ground_truth|gold_answer|held_out_answer|answer_key|private_capability)\b",
        positive_control='"ground_truth": "inspect_schema_keys"',
        negative_control="the held-out corpus was not read and not run",
    ),
)


@dataclass(frozen=True)
class Finding:
    check: str
    category: str
    path: str
    line_number: int
    matched: str
    line: str


@dataclass(frozen=True)
class ControlResult:
    check: str
    positive_fired: bool
    negative_clean: bool

    @property
    def ok(self) -> bool:
        return self.positive_fired and self.negative_clean


def verify_controls(checks: Sequence[Check] = CHECKS) -> list[ControlResult]:
    """Prove every pattern still catches a synthetic example of what it forbids."""
    results: list[ControlResult] = []
    for check in checks:
        regex = check.regex
        positive_fired = bool(regex.search(check.positive_control))
        negative_clean = not (
            check.negative_control and regex.search(check.negative_control)
        )
        results.append(
            ControlResult(
                check=check.name,
                positive_fired=positive_fired,
                negative_clean=negative_clean,
            )
        )
    return results


def _iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for number, line in enumerate(handle, start=1):
            yield number, line.rstrip("\n")


def scan_file(
    relative_path: str,
    root: Path = REPO_ROOT,
    checks: Sequence[Check] = CHECKS,
) -> list[Finding]:
    path = root / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"declared public file is missing: {relative_path}")
    findings: list[Finding] = []
    for number, line in _iter_lines(path):
        for check in checks:
            if relative_path in check.exempt_files:
                continue
            if relative_path in POLICY_FILES:
                continue
            if any(token in line for token in check.allow):
                continue
            match = check.regex.search(line)
            if match is None:
                continue
            findings.append(
                Finding(
                    check=check.name,
                    category=check.category,
                    path=relative_path,
                    line_number=number,
                    matched=match.group(0),
                    line=line.strip()[:160],
                )
            )
    return findings


def scan(
    files: Iterable[str] = PUBLIC_FILES,
    root: Path = REPO_ROOT,
    checks: Sequence[Check] = CHECKS,
) -> list[Finding]:
    findings: list[Finding] = []
    for relative_path in files:
        findings.extend(scan_file(relative_path, root=root, checks=checks))
    return findings


def scan_text(
    label: str,
    text: str,
    checks: Sequence[Check] = CHECKS,
) -> list[Finding]:
    """Scan already-extracted text under a label that names where it came from."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for check in checks:
            if any(token in line for token in check.allow):
                continue
            match = check.regex.search(line)
            if match is None:
                continue
            findings.append(
                Finding(
                    check=check.name,
                    category=check.category,
                    path=label,
                    line_number=number,
                    matched=match.group(0),
                    line=line.strip()[:160],
                )
            )
    return findings


_XML_TEXT_RUN = re.compile(r"<a:t>(.*?)</a:t>", re.DOTALL)
_XML_DESCRIPTION = re.compile(r'(?:descr|title|name)="([^"]*)"')
_XML_PROPERTY = re.compile(r"<(?:cp|dc|dcterms|ap):[A-Za-z]+[^>]*>([^<]*)<")

# The parts of a PowerPoint bundle that carry text a reader or a tool can see.
# Everything else in the zip is either binary media or layout plumbing.
PPTX_TEXT_PARTS: tuple[str, ...] = ("ppt/slides/", "ppt/notesSlides/", "docProps/")


def pptx_inspectable_text(path: Path) -> list[tuple[str, str]]:
    """Every inspectable text part of a PPTX, as ``(part name, text)`` pairs.

    Reads the zip members only. No machine-local path is opened into the file
    and none is read out of it.
    """
    parts: list[tuple[str, str]] = []
    with zipfile.ZipFile(path) as bundle:
        for name in sorted(bundle.namelist()):
            if not name.endswith(".xml"):
                continue
            if not any(name.startswith(prefix) for prefix in PPTX_TEXT_PARTS):
                continue
            xml = bundle.read(name).decode("utf-8", errors="replace")
            pieces = [
                *_XML_TEXT_RUN.findall(xml),
                *_XML_DESCRIPTION.findall(xml),
                *_XML_PROPERTY.findall(xml),
            ]
            text = "\n".join(
                html.unescape(piece).strip() for piece in pieces if piece.strip()
            )
            if text:
                parts.append((name, text))
    return parts


def scan_pptx(
    relative_path: str = PITCH_PPTX,
    root: Path = REPO_ROOT,
    checks: Sequence[Check] = CHECKS,
) -> list[Finding]:
    path = root / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"declared public file is missing: {relative_path}")
    findings: list[Finding] = []
    for part, text in pptx_inspectable_text(path):
        findings.extend(scan_text(f"{relative_path}!{part}", text, checks=checks))
    return findings


OCR_COMMAND = "tesseract"

# Recognition cannot reliably tell a hyphen from a dash in a rendered glyph: it
# reads the hyphen in "second-decision" and in the tag "#tool-selection" as a
# long dash in the frames rendered here. Typography is authored in the deck
# source and is checked there, where the character is unambiguous. Every
# identity, path, credential and truth pattern still runs over the pixels.
OCR_EXCLUDED_CHECKS: frozenset[str] = frozenset({"em-dash"})
OCR_CHECKS: tuple[Check, ...] = tuple(
    check for check in CHECKS if check.name not in OCR_EXCLUDED_CHECKS
)


def ocr_available() -> bool:
    """Whether a text recognizer is installed for the frame check."""
    return shutil.which(OCR_COMMAND) is not None


def frame_text(path: Path) -> str:
    """Read a rendered frame back as text. Local process, no network."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, local binary
        [OCR_COMMAND, str(path), "stdout"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def scan_frames(
    relative_paths: Sequence[str] = PITCH_FRAMES,
    root: Path = REPO_ROOT,
    checks: Sequence[Check] = OCR_CHECKS,
) -> list[Finding]:
    """Scan the text recognized in each rendered frame.

    The frames are the deck's own pixels, so this is the one check that reads
    what a viewer actually sees rather than what the source says.
    """
    findings: list[Finding] = []
    for relative_path in relative_paths:
        path = root / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"declared public file is missing: {relative_path}")
        findings.extend(
            scan_text(
                f"{relative_path} (recognized text)", frame_text(path), checks=checks
            )
        )
    return findings


#
# ---------------------------------------------------------------------------
# The whole-tree scan
#
# `scan()` above covers the judge-facing set in this repository. A published
# export is a different question: there, EVERY text file ships, including the
# application source, the infrastructure definitions and the tests. Those files
# are held to the same rules with two documented differences, both already
# stated in the policy.
#
# 1. Generic field names are the public API contract of the code that uses
#    them. `receipt_id` in `src/unstuck/cascade/receipts.py` is not a leaked
#    identifier, it is the name of a field; renaming it to pass a prose scan
#    would change behaviour. The same holds for the internal-code patterns,
#    whose job is to keep process identifiers out of prose. These checks are
#    therefore waived for the code class and only there.
# 2. Code legitimately carries the SHAPE of a forbidden value: a validation
#    regex, a format template, a zero-filled example, a synthetic test literal.
#    A shape is waived; a real value is not. The waiver is a declared token
#    list, so every waived line can be read back from the scan's own output.
# ---------------------------------------------------------------------------

#: Patterns that forbid a NAME rather than a value. Waived for the code class.
FIELD_NAME_CHECKS: frozenset[str] = frozenset(
    {
        "identifier-field",
        "hash-field",
        "dataset-version-label",
        "benchmark-arm-label",
        "benchmark-arm-field",
        "process-gate-identifier",
        "requirement-code",
        # A request key in a test fixture ("call-1") names the call under test,
        # not a private run. The prose rule still forbids it in prose.
        "private-run-label",
        # A local cache or artifact directory is configured in code and in
        # pyproject, which is where it belongs. The rule exists to keep private
        # evidence paths out of prose.
        "private-venv-path",
    }
)

#: Patterns that forbid the SHAPE of a value. A leakage test must carry the
#: shapes it proves are rejected or redacted: the sanitizer tests hold a
#: synthetic access key, the W3C trace-context example identifiers, and the
#: `/Users/`, `/home/` and `/var/folders/` prefixes they assert never survive.
#: This is the policy's third category, "a file whose content IS the denylist",
#: and it is waived for the control class only. The standing claim is the one
#: the policy already makes for that category: shapes and synthetic examples,
#: and no real value from any run. That claim is enforced by exclusion rather
#: than by a shipped denylist of real values, because shipping such a list
#: would publish the very values it names: a test file that carries a real
#: value is kept out of the export entirely.
SHAPE_CHECKS: frozenset[str] = frozenset(
    {
        "uuid",
        "aws-account-id",
        "aws-arn",
        "aws-resource-id",
        "credential-pattern",
        "absolute-local-path",
        "hex-digest",
        "truncated-digest",
        "run-identifying-timestamp",
        # A field NAME, asserted to be rejected. No truth value is carried.
        "evaluator-truth-field",
    }
)

#: A matched line in the code class is a shape, not a value, when it carries
#: one of these. Each token is a reason, not a convenience.
CODE_ALLOW_TOKENS: tuple[str, ...] = (
    "pattern=",  # a validation regex for the shape
    'r"',  # a raw-string regex literal
    "r'",
    "{",  # a format template whose value is supplied at run time
    "example",  # a declared example value
    "EXAMPLE",
    "synthetic",  # a declared synthetic fixture value
    "placeholder",
    "your-",
    "0000",  # a zero-filled placeholder
    "0123456789abcdef",  # the hex alphabet, used to validate hex
)

#: Declared exceptions: a real value that is public and that the code needs.
#: ``(path, check, reason)``. Anything not listed here is a finding.
CODE_ALLOWANCES: tuple[tuple[str, str, str], ...] = (
    (
        "deployment/base-image.lock",
        "hex-digest",
        "Two digests of the PUBLIC upstream python:3.12-slim-bookworm image on "
        "public.ecr.aws. They pin a third-party base image so the container "
        "build is reproducible, and they identify nothing private.",
    ),
    (
        "deployment/sagemaker/base-image.lock",
        "hex-digest",
        "One digest of the same PUBLIC upstream python:3.12-slim-bookworm "
        "image, pinning the serving container's base. Nothing private.",
    ),
    (
        "deployment/Dockerfile",
        "absolute-local-path",
        "Paths inside the container filesystem, not on any machine.",
    ),
    (
        "deployment/sagemaker/Dockerfile",
        "absolute-local-path",
        "Paths inside the container filesystem, not on any machine.",
    ),
    (
        "PUBLIC-MANIFEST.json",
        "hex-digest",
        "The export's integrity manifest IS a list of digests: one sha256 per "
        "published file, over content that is published. It identifies nothing "
        "private and carries no absolute path.",
    ),
    (
        "PUBLIC-MANIFEST.json",
        "aws-account-id",
        "A run of twelve digits inside a sha256 digest is a coincidence of the "
        "hash, not an account identifier. Every match is inside a digest.",
    ),
    (
        "PUBLIC-MANIFEST.json",
        "dataset-version-label",
        "The shipped package's data file is named for the fixture it carries, "
        "so the manifest records that file name.",
    ),
    (
        "src/unstuck/events.py",
        "credential-pattern",
        "Two comment lines name the credential fields the redaction denylist "
        "covers, in order to redact them. No value is carried.",
    ),
)

#: Suffixes read as code or configuration rather than as prose.
CODE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".mjs", ".js", ".toml", ".lock", ".cfg", ".ini", ".sh", ".yaml", ".yml"}
)
CODE_NAMES: frozenset[str] = frozenset(
    {"Dockerfile", "requirements-runtime.txt", "requirements-serving.txt"}
)
#: Configuration carried in JSON is code, not prose. A declared example config
#: holds the SHAPE of an account, an ARN and a resource identifier, zero-filled;
#: the curated public JSON under `dist/` is data a reader reads and stays prose.
CODE_NAME_SUFFIXES: tuple[str, ...] = (".example.json",)
#: Everything under `src/` is the shipped package, including its data file and
#: the local UI assets: a field name there is the code's own contract.
CODE_PATH_PREFIXES: tuple[str, ...] = ("deployment/", "src/")

#: Files read as neither: binary, or covered by a derived reading.
BINARY_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".pptx", ".whl", ".gz", ".zip", ".pdf"}
)

#: Never scanned, never exported: install state and byte caches.
SKIP_DIRECTORIES: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".ruff_cache", ".pytest_cache"}
)


def file_class(relative_path: str) -> str:
    """``"code"``, ``"control"``, ``"prose"``, or ``"binary"`` for one file."""
    path = PurePosixPath(relative_path)
    if path.suffix.lower() in BINARY_SUFFIXES:
        return "binary"
    if path.parts and path.parts[0] == "tests":
        return "control"
    if path.name in CODE_NAMES or path.suffix in CODE_SUFFIXES:
        return "code"
    if path.name.endswith(CODE_NAME_SUFFIXES):
        return "code"
    if relative_path.startswith(CODE_PATH_PREFIXES):
        return "code"
    return "prose"


def checks_for(file_class_name: str) -> tuple[Check, ...]:
    if file_class_name == "code":
        return tuple(check for check in CHECKS if check.name not in FIELD_NAME_CHECKS)
    if file_class_name == "control":
        waived = FIELD_NAME_CHECKS | SHAPE_CHECKS
        return tuple(check for check in CHECKS if check.name not in waived)
    return CHECKS


def _allowed_by_declaration(relative_path: str, check: str) -> bool:
    return any(
        relative_path == path and check == name for path, name, _ in CODE_ALLOWANCES
    )


def scan_tree(
    root: Path,
    checks: Sequence[Check] = CHECKS,
) -> tuple[list[Finding], dict[str, int]]:
    """Scan every text file under ``root``, by class, and count what was read.

    This is the check that an export is clean as a whole rather than clean in
    the files someone remembered to declare.
    """
    findings: list[Finding] = []
    counts = {"prose": 0, "code": 0, "control": 0, "binary": 0, "pptx": 0, "frames": 0}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        relative_path = path.relative_to(root).as_posix()
        if relative_path in POLICY_FILES or relative_path == SCAN_SOURCE:
            continue
        kind = file_class(relative_path)
        counts[kind] += 1
        if kind == "binary":
            continue
        selected = [
            check
            for check in checks_for(kind)
            if check in checks
            and not _allowed_by_declaration(relative_path, check.name)
        ]
        if kind in {"code", "control"}:
            selected = [
                Check(
                    name=check.name,
                    category=check.category,
                    pattern=check.pattern,
                    positive_control=check.positive_control,
                    negative_control=check.negative_control,
                    exempt_files=check.exempt_files,
                    allow=(*check.allow, *CODE_ALLOW_TOKENS),
                    flags=check.flags,
                )
                for check in selected
            ]
        try:
            findings.extend(scan_file(relative_path, root=root, checks=selected))
        except UnicodeDecodeError:
            findings.append(
                Finding(
                    check="unreadable-text",
                    category="coverage",
                    path=relative_path,
                    line_number=0,
                    matched="",
                    line="not valid UTF-8, so it was not scanned as text",
                )
            )
    pptx = root / PITCH_PPTX
    if pptx.is_file():
        findings.extend(scan_pptx(PITCH_PPTX, root=root, checks=checks))
        counts["pptx"] = 1
    if ocr_available():
        frames = [name for name in PITCH_FRAMES if (root / name).is_file()]
        findings.extend(
            scan_frames(
                frames,
                root=root,
                checks=[
                    check for check in checks if check.name not in OCR_EXCLUDED_CHECKS
                ],
            )
        )
        counts["frames"] = len(frames)
    return findings, counts


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv or [])
    if arguments and arguments[0] == "--tree":
        return main_tree(Path(arguments[1]).resolve())
    return main_declared()


def main_tree(root: Path) -> int:
    """Scan an export tree whole: every text file, by class."""
    controls = verify_controls()
    broken = [result.check for result in controls if not result.ok]
    if broken:
        print(f"FAIL: {len(broken)} pattern(s) no longer catch their own example.")
        return 2
    print(f"Positive controls: {len(controls)} of {len(controls)} fired.")
    findings, counts = scan_tree(root)
    print(
        f"\nScanned {root}\n"
        f"  prose files : {counts['prose']}  (all {len(CHECKS)} patterns)\n"
        f"  code files  : {counts['code']}  "
        f"({len(CHECKS) - len(FIELD_NAME_CHECKS)} patterns; field-name and "
        "internal-code patterns waived, shapes allowed)\n"
        f"  test files  : {counts['control']}  "
        f"({len(CHECKS) - len(FIELD_NAME_CHECKS | SHAPE_CHECKS)} patterns; a "
        "leakage test must carry the shapes it proves are rejected)\n"
        f"  binary files: {counts['binary']}  (not text)\n"
        f"  PowerPoint  : {counts['pptx']}  (inspectable XML text)\n"
        f"  frames      : {counts['frames']}  (recognized text)"
    )
    for path, check, reason in CODE_ALLOWANCES:
        print(f"  declared exception: {path} [{check}] {reason}")
    if findings:
        print(f"\nFAIL: {len(findings)} finding(s).")
        for finding in findings:
            print(
                f"  {finding.path}:{finding.line_number}  [{finding.check}] "
                f"{finding.matched!r}\n      {finding.line}"
            )
        return 1
    print("\nPASS: zero findings over the whole tree.")
    return 0


def main_declared(argv: Sequence[str] | None = None) -> int:
    del argv
    controls = verify_controls()
    print("Positive controls:")
    for result in controls:
        state = "ok" if result.ok else "BROKEN"
        print(f"  {state:>6}  {result.check}")
    broken = [result for result in controls if not result.ok]
    if broken:
        print(
            f"\nFAIL: {len(broken)} pattern(s) no longer catch their own example. "
            "A broken check is not a clean repository."
        )
        return 2

    ocr = ocr_available()
    try:
        findings = scan()
        findings.extend(scan_pptx())
        if ocr:
            findings.extend(scan_frames())
    except FileNotFoundError as error:
        print(f"\nFAIL: {error}")
        return 2

    print(f"\nScanned {len(PUBLIC_FILES)} public files against {len(CHECKS)} patterns.")
    print(
        f"Scanned the inspectable text of {PITCH_PPTX} "
        "(slide runs, image alt text, speaker notes, document properties)."
    )
    if ocr:
        print(f"Scanned the recognized text of {len(PITCH_FRAMES)} rendered frames.")
    else:
        print(
            f"SKIPPED the rendered-frame text check: {OCR_COMMAND} is not installed. "
            "The frames are screenshots of the scanned deck source, so their text is "
            "covered at the source; install the recognizer to check the pixels too."
        )
    if findings:
        print(f"\nFAIL: {len(findings)} finding(s).")
        for finding in findings:
            print(
                f"  {finding.path}:{finding.line_number}  [{finding.check}] "
                f"{finding.matched!r}\n      {finding.line}"
            )
        return 1
    print("PASS: zero findings.")
    return 0


if __name__ == "__main__":  # pragma: no cover - command line entry point
    sys.exit(main(sys.argv[1:]))
