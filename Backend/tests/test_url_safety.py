import ipaddress
import socket

import pytest

from app.core.exceptions import TransientIngestionError, UnsafeUrlError
from app.ingestion.url_safety import (
    assert_safe_destination,
    is_public_ip,
    normalize_url,
    validate_url,
)


def public_resolver(host, port):
    return ["93.184.216.34"]


BAD_URLS = [
    "", "   ", "example.com/no-scheme", "ftp://example.com/x", "file:///etc/passwd",
    "javascript:alert(1)", "http:///nohost", "http://[::1", "http://exa mple.com/",
    "http://user:pw@example.com/", "http://example.com:22/", "http://example.com:8080/",
    "http://example.com:0/", "https://" + "a" * 3000 + ".com",
    "http://localhost/", "http://foo.localhost/", "http://metadata.google.internal/",
    "http://printer.local/",
    "http://127.0.0.1/", "http://10.0.0.5/", "http://172.16.0.1/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/", "http://100.64.0.1/",
    "http://224.0.0.1/",
    "http://[::1]/", "http://[fe80::1]/", "http://[fc00::1]/", "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_rejects_dangerous_urls(url):
    with pytest.raises(UnsafeUrlError):
        assert_safe_destination(url, public_resolver)


def test_accepts_normal_public_url():
    validated, ips = assert_safe_destination("https://EXAMPLE.com:443/a?b=1#frag", public_resolver)
    assert validated.host == "example.com" and validated.port == 443
    assert str(ips[0]) == "93.184.216.34"


def test_dns_pointing_to_private_address_is_rejected():
    with pytest.raises(UnsafeUrlError):
        assert_safe_destination("http://evil.example/", lambda h, p: ["127.0.0.1"])


def test_one_private_address_among_public_ones_rejects_the_host():
    with pytest.raises(UnsafeUrlError):
        assert_safe_destination("http://mixed.example/", lambda h, p: ["93.184.216.34", "10.0.0.8"])


def test_decimal_ip_trick_is_caught_after_resolution():
    # "2130706433" is how some resolvers spell 127.0.0.1
    with pytest.raises(UnsafeUrlError):
        assert_safe_destination("http://2130706433/", lambda h, p: ["127.0.0.1"])


def test_unresolvable_host_is_a_transient_failure():
    def failing(host, port):
        raise socket.gaierror("no such host")

    with pytest.raises(TransientIngestionError):
        assert_safe_destination("http://nowhere.example/", failing)


@pytest.mark.parametrize(
    "address,expected",
    [
        ("8.8.8.8", True), ("1.1.1.1", True), ("2606:4700:4700::1111", True), ("::ffff:8.8.8.8", True),
        ("127.0.0.1", False), ("10.1.2.3", False), ("172.31.0.1", False), ("192.168.0.1", False),
        ("169.254.169.254", False), ("100.64.0.1", False), ("224.0.0.1", False),
        ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public_ip(address, expected):
    assert is_public_ip(ipaddress.ip_address(address)) is expected


def test_normalize_url_strips_fragment_default_port_and_tracking():
    assert (
        normalize_url("HTTPS://Example.COM:443/Path/?utm_source=x&a=1&fbclid=z#frag")
        == "https://example.com/Path/?a=1"
    )
    assert normalize_url("http://example.com") == "http://example.com/"


def test_validate_url_exposes_request_target():
    assert validate_url("https://example.com/a?b=1").request_target == "/a?b=1"