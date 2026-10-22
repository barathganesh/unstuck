"""Security-negative tests for the loopback boundary. Offline.

The local API is reachable from the operator's own machine and nowhere else.
These tests assert the refusals, not the happy path: a non-loopback bind is
refused before a socket exists, and the Host and Origin checks are the
DNS-rebinding defence of `docs/spec/design.md` section 13.
"""

import inspect

import pytest

from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.contracts import BindAddress
from unstuck.operator.security import (
    LocalGuard,
    check_host,
    check_media_type,
    check_origin,
    loopback_bind,
)

from .conftest import LOOPBACK_AUTHORITY, LOOPBACK_ORIGIN

NON_LOOPBACK = ("0.0.0.0", "::", "0", "192.168.1.10", "10.0.0.5", "example.invalid")
FOREIGN_HOSTS = (
    "evil.invalid",
    "127.0.0.1.evil.invalid",
    "localhost:9999",
    "127.0.0.1:9999",
    "127.0.0.2:8081",
    "[::1]:8081",
    "",
)
FOREIGN_ORIGINS = (
    "http://evil.invalid",
    "https://127.0.0.1:8081",
    "http://127.0.0.1:9999",
    "http://localhost",
    "null",
    "",
)


@pytest.mark.parametrize("host", NON_LOOPBACK)
def test_a_non_loopback_bind_is_refused_before_a_socket_exists_offline(
    host: str,
) -> None:
    with pytest.raises(OperatorFault) as raised:
        loopback_bind(host, 8081)
    assert raised.value.reason is OperatorReason.NON_LOOPBACK_HOST
    assert host in raised.value.detail


def test_the_default_bind_is_loopback_offline() -> None:
    bind = loopback_bind("127.0.0.1", 8081)
    assert isinstance(bind, BindAddress)
    assert bind.authority == LOOPBACK_AUTHORITY
    assert loopback_bind("localhost", 8081).host == "localhost"
    signature = inspect.signature(loopback_bind)
    assert signature.parameters["host"].default in (
        inspect.Parameter.empty,
        "127.0.0.1",
    )


@pytest.mark.parametrize("header", FOREIGN_HOSTS)
def test_a_foreign_host_header_is_rejected_offline(header: str) -> None:
    bind = BindAddress()
    with pytest.raises(OperatorFault) as raised:
        check_host(header, bind)
    assert raised.value.reason is OperatorReason.HOST_REJECTED


def test_a_missing_host_header_is_rejected_offline() -> None:
    with pytest.raises(OperatorFault) as raised:
        check_host(None, BindAddress())
    assert raised.value.reason is OperatorReason.HOST_REJECTED


def test_the_exact_loopback_authorities_are_accepted_offline() -> None:
    bind = BindAddress()
    for header in ("127.0.0.1:8081", "localhost:8081"):
        check_host(header, bind)


@pytest.mark.parametrize("header", FOREIGN_ORIGINS)
def test_a_foreign_or_missing_origin_is_rejected_on_a_write_offline(
    header: str,
) -> None:
    with pytest.raises(OperatorFault) as raised:
        check_origin(header, BindAddress())
    assert raised.value.reason is OperatorReason.ORIGIN_REJECTED
    with pytest.raises(OperatorFault):
        check_origin(None, BindAddress())


def test_the_exact_loopback_origins_are_accepted_offline() -> None:
    bind = BindAddress()
    for header in (LOOPBACK_ORIGIN, "http://localhost:8081"):
        check_origin(header, bind)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ],
)
def test_a_non_json_media_type_is_rejected_offline(header: str | None) -> None:
    with pytest.raises(OperatorFault) as raised:
        check_media_type(header)
    assert raised.value.reason is OperatorReason.MEDIA_TYPE_REJECTED


def test_json_media_types_are_accepted_offline() -> None:
    for header in ("application/json", "application/json; charset=utf-8"):
        check_media_type(header)


def test_the_guard_applies_host_only_on_read_and_all_three_on_write_offline() -> None:
    guard = LocalGuard(BindAddress())
    guard.read({"host": LOOPBACK_AUTHORITY})
    with pytest.raises(OperatorFault):
        guard.read({"host": "evil.invalid"})
    guard.write(
        {
            "host": LOOPBACK_AUTHORITY,
            "origin": LOOPBACK_ORIGIN,
            "content-type": "application/json",
        }
    )
    with pytest.raises(OperatorFault) as missing_origin:
        guard.write({"host": LOOPBACK_AUTHORITY, "content-type": "application/json"})
    assert missing_origin.value.reason is OperatorReason.ORIGIN_REJECTED
    with pytest.raises(OperatorFault) as wrong_type:
        guard.write({"host": LOOPBACK_AUTHORITY, "origin": LOOPBACK_ORIGIN})
    assert wrong_type.value.reason is OperatorReason.MEDIA_TYPE_REJECTED


def test_the_unit_defines_no_cors_header_anywhere_offline() -> None:
    import unstuck.operator as package

    root = __import__("pathlib").Path(str(package.__file__)).parent
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".html", ".js", ".css"}:
            text = path.read_text(encoding="utf-8").lower()
            assert "access-control-allow" not in text, path
            assert "cors" not in text or "no cors" in text, path
