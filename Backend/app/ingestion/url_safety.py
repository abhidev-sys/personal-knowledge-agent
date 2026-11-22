"""URL validation and SSRF defenses. Every outbound fetch goes through this module."""
import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Union

from app.core.exceptions import TransientIngestionError, UnsafeUrlError

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Resolver = Callable[[str, int], list[str]]

ALLOWED_SCHEMES = {"http": 80, "https": 443}
ALLOWED_PORTS = {80, 443}
MAX_URL_LENGTH = 2048

_BLOCKED_HOSTS = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata.google.internal",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan")
_TRACKING_PARAM = re.compile(
    r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref_src|_hsenc|_hsmi)$", re.I
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


@dataclass(frozen=True)
class ValidatedUrl:
    scheme: str
    host: str  # lowercase ASCII (IDNA) host name or IP literal, without brackets
    port: int
    path: str
    query: str

    @property
    def request_target(self) -> str:
        return self.path + (f"?{self.query}" if self.query else "")

    @property
    def normalized(self) -> str:
        """Stable form used as a duplicate key: no fragment, no default port, no tracking params."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == ALLOWED_SCHEMES[self.scheme] else f":{self.port}"
        kept = [p for p in self.query.split("&") if p and not _TRACKING_PARAM.match(p.split("=")[0])]
        query = f"?{'&'.join(kept)}" if kept else ""
        return f"{self.scheme}://{host}{port}{self.path}{query}"


def validate_url(raw: str) -> ValidatedUrl:
    """Syntax and policy checks only (no DNS). Raises UnsafeUrlError."""
    from urllib.parse import urlsplit

    value = (raw or "").strip()
    if not value:
        raise UnsafeUrlError("The URL is empty.")
    if len(value) > MAX_URL_LENGTH:
        raise UnsafeUrlError("The URL is too long.")
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        raise UnsafeUrlError("The URL contains spaces or control characters.")
    try:
        parts = urlsplit(value)
        port = parts.port
        hostname = parts.hostname
    except ValueError as exc:
        raise UnsafeUrlError("The URL is not valid.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError("Only http and https URLs are allowed.")
    if not hostname:
        raise UnsafeUrlError("The URL has no host name.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("URLs containing a user name or password are not allowed.")
    if port is None:
        port = ALLOWED_SCHEMES[scheme]
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError("Only ports 80 and 443 are allowed.")
    return ValidatedUrl(
        scheme=scheme,
        host=_normalize_host(hostname),
        port=port,
        path=parts.path or "/",
        query=parts.query,
    )


def normalize_url(raw: str) -> str:
    return validate_url(raw).normalized


def _normalize_host(hostname: str) -> str:
    try:
        ipaddress.ip_address(hostname)
        return hostname  # IP literal: checked later against the public-address rule
    except ValueError:
        pass
    try:
        host = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("The host name is not valid.") from exc
    host = host.rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError("This host is not allowed.")
    return host


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return False
        if ip in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return ip.is_global and not ip.is_multicast


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def resolve_public_ips(host: str, port: int, resolver: Resolver = default_resolver) -> list[IPAddress]:
    """Resolve `host`; EVERY returned address must be public, otherwise the URL is refused."""
    try:
        raw = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            raw = resolver(host, port)
        except OSError as exc:
            raise TransientIngestionError("Could not resolve the host name right now.") from exc
    if not raw:
        raise TransientIngestionError("Could not resolve the host name right now.")
    addresses: list[IPAddress] = []
    for text in raw:
        try:
            ip = ipaddress.ip_address(text.split("%")[0])
        except ValueError as exc:
            raise UnsafeUrlError("The host resolved to an invalid address.") from exc
        if not is_public_ip(ip):
            raise UnsafeUrlError("The URL points to a private or internal address, which is not allowed.")
        addresses.append(ip)
    return addresses


def assert_safe_destination(
    raw: str, resolver: Resolver = default_resolver
) -> tuple[ValidatedUrl, list[IPAddress]]:
    validated = validate_url(raw)
    return validated, resolve_public_ips(validated.host, validated.port, resolver)