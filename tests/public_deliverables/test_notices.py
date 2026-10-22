"""LICENSE and NOTICE (REQ-PUB-01, NFR8.1). Offline.

The licence body must be the unmodified Apache-2.0 text, and the notice must
name every pinned dependency while asserting no licence for any of them.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LICENSE = ROOT / "LICENSE"
NOTICE = ROOT / "NOTICE"

#: Section headings of the Apache-2.0 body, in order.
APACHE_SECTIONS = (
    "Apache License",
    "Version 2.0, January 2004",
    "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
    "1. Definitions.",
    "2. Grant of Copyright License.",
    "3. Grant of Patent License.",
    "4. Redistribution.",
    "5. Submission of Contributions.",
    "6. Trademarks.",
    "7. Disclaimer of Warranty.",
    "8. Limitation of Liability.",
    "9. Accepting Warranty or Additional Liability.",
    "END OF TERMS AND CONDITIONS",
    "APPENDIX: How to apply the Apache License to your work.",
)


def pinned() -> dict[str, str]:
    """Every dependency pinned in pyproject.toml, as name to version."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    specs: list[str] = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        specs.extend(extra)
    pins: dict[str, str] = {}
    for spec in specs:
        name, _, version = spec.partition("==")
        assert version, f"{spec} is not pinned with =="
        pins[name] = version
    return pins


def test_the_licence_is_the_unmodified_apache_body_offline() -> None:
    text = LICENSE.read_text(encoding="utf-8")
    position = 0
    for section in APACHE_SECTIONS:
        found = text.find(section, position)
        assert found >= 0, f"missing or out of order: {section}"
        position = found + len(section)
    # The placeholders stay as the licence ships them; filling them in would be
    # a modification of the body.
    assert "Copyright [yyyy] [name of copyright owner]" in text
    assert "http://www.apache.org/licenses/LICENSE-2.0" in text


def test_the_licence_body_carries_no_local_edit_offline() -> None:
    text = LICENSE.read_text(encoding="utf-8")
    for local in ("Unstuck", "Decider", "Pending", "NFR8", "hackathon"):
        assert local not in text, f"the licence body was edited: {local}"


def test_the_notice_names_every_pinned_dependency_offline() -> None:
    notice = NOTICE.read_text(encoding="utf-8")
    for name, version in pinned().items():
        assert name in notice, f"NOTICE does not name {name}"
        pattern = rf"{re.escape(name)}\s+{re.escape(version)}\b"
        assert re.search(pattern, notice), f"NOTICE pins {name} at a wrong version"


def test_the_notice_asserts_no_licence_offline() -> None:
    notice = NOTICE.read_text(encoding="utf-8")
    pins = pinned()
    unverified = notice.lower().count("licence unverified")
    # One line per dependency, plus the model and the container base image.
    assert unverified >= len(pins) + 2
    # The status is stated in plain words, not as an internal process code.
    assert "verification pending" in notice
    assert notice.count("verification pending") >= len(pins) + 2
    assert "UNVERIFIED" in notice
    # No specific licence is named, which would be an unverified assertion.
    for named in ("MIT License", "BSD", "Apache License, Version 2.0 (the"):
        assert named not in notice, f"NOTICE asserts a licence: {named}"


def test_the_notice_names_the_model_and_the_base_image_offline() -> None:
    notice = NOTICE.read_text(encoding="utf-8")
    assert "Decider 2B" in notice
    assert "deployment/base-image.lock" in notice
    assert "operator-supplied" in notice


def test_the_notice_covers_the_bundled_recipe_offline() -> None:
    notice = NOTICE.read_text(encoding="utf-8")
    assert "recipes/diagnostic-tool-selection/" in notice
    assert "no third-party dependency" in notice


def test_the_notice_points_at_the_licence_offline() -> None:
    assert "LICENSE" in NOTICE.read_text(encoding="utf-8")


#: The internal process identifiers the public scrub policy forbids in a public
#: file: requirement codes, acceptance-criterion codes, provider-fact gates,
#: gate-matrix entries, test identifiers and requirement identifiers.
INTERNAL_CODE = re.compile(
    r"\b(?:AC\d+\.\d+\.\d+|FR\d+\.\d+|NFR\d+\.\d+"
    r"|PF[1-9]|GM\d+|TST-[A-Z0-9][A-Z0-9.-]*|REQ-[A-Z0-9][A-Z0-9.-]*)\b"
)


def test_the_notice_carries_no_internal_requirement_code_offline() -> None:
    found = INTERNAL_CODE.findall(NOTICE.read_text(encoding="utf-8"))
    assert not found, (
        f"NOTICE carries internal process identifiers: {sorted(set(found))}"
    )


def test_the_notice_states_the_pending_status_in_plain_words_offline() -> None:
    notice = NOTICE.read_text(encoding="utf-8")
    # Every dependency, the model and the base image carry a plain-language
    # status, and the explanation beside them is plain language too.
    assert notice.count("licence unverified, verification pending") >= len(pinned())
    assert "its verification is pending" in notice
    assert "Licence verification means reading each upstream licence text" in notice
    assert "Completing the verification is an owner action" in notice
