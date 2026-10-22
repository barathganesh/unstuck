"""Loopback bind, Host, Origin, and media-type guards.

`docs/spec/design.md` section 13 fixes these checks. The bind is loopback only
and a non-loopback host is refused before a socket exists. `Host` must equal
the loopback authority exactly, which is what defeats DNS rebinding: a name
that resolves to 127.0.0.1 still arrives with its own Host header and is
refused. Every state-changing request additionally requires an exact `Origin`
and `application/json`.

No CORS header is served, ever. That is the point: a browser on another origin
has no business reaching this surface, so there is nothing to negotiate.
"""

from collections.abc import Mapping

from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.contracts import LOOPBACK_HOSTS, BindAddress

JSON_MEDIA_TYPE = "application/json"


def loopback_bind(host: str = "127.0.0.1", port: int = 8081) -> BindAddress:
    """Resolve the bind, refusing anything that is not loopback."""
    if host not in LOOPBACK_HOSTS:
        raise OperatorFault(
            OperatorReason.NON_LOOPBACK_HOST,
            f"{host} is not a loopback address; the operator API is local only",
        )
    try:
        return BindAddress(host=host, port=port)
    except ValueError as error:
        raise OperatorFault(OperatorReason.NON_LOOPBACK_HOST, str(error)) from error


def check_host(header: str | None, bind: BindAddress) -> None:
    """The Host header must equal a loopback authority exactly."""
    if header is None or header not in bind.authorities:
        raise OperatorFault(
            OperatorReason.HOST_REJECTED,
            "Host must be the exact loopback authority",
        )


def check_origin(header: str | None, bind: BindAddress) -> None:
    """A state-changing request must carry an exact loopback Origin."""
    if header is None or header not in bind.origins:
        raise OperatorFault(
            OperatorReason.ORIGIN_REJECTED,
            "Origin must be the exact loopback origin",
        )


def check_media_type(header: str | None) -> None:
    """A state-changing request must declare JSON."""
    if header is None:
        raise OperatorFault(
            OperatorReason.MEDIA_TYPE_REJECTED, f"{JSON_MEDIA_TYPE} required"
        )
    if header.split(";", 1)[0].strip().lower() != JSON_MEDIA_TYPE:
        raise OperatorFault(
            OperatorReason.MEDIA_TYPE_REJECTED, f"{JSON_MEDIA_TYPE} required"
        )


class LocalGuard:
    """Applies the section 13 checks. Reads need Host; writes need all three."""

    def __init__(self, bind: BindAddress):
        self.bind = bind

    def read(self, headers: Mapping[str, str]) -> None:
        check_host(headers.get("host"), self.bind)

    def write(self, headers: Mapping[str, str]) -> None:
        check_host(headers.get("host"), self.bind)
        check_origin(headers.get("origin"), self.bind)
        check_media_type(headers.get("content-type"))


__all__ = [
    "JSON_MEDIA_TYPE",
    "LocalGuard",
    "check_host",
    "check_media_type",
    "check_origin",
    "loopback_bind",
]
