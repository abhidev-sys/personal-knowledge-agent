import socket

import httpx
import pytest

from app.core.exceptions import IngestionError, TransientIngestionError, UnsafeUrlError
from app.ingestion.fetcher import SafeFetcher

PUBLIC = "93.184.216.34"
HOSTS = {
    "good.example": [PUBLIC],
    "multi.example": [PUBLIC, "8.8.8.8"],
    "evil.example": ["127.0.0.1"],
}
HTML = (
    "<html><head><title>T</title></head><body><article><p>"
    + "hello world " * 10
    + "</p></article></body></html>"
)


def resolver(host, port):
    if host in HOSTS:
        return HOSTS[host]
    raise socket.gaierror("unknown host")


def make(handler, **overrides):
    options = {"max_bytes": 1000, "max_redirects": 3}
    options.update(overrides)
    return SafeFetcher(resolver=resolver, transport=httpx.MockTransport(handler), **options)


def ok(request):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=HTML)


def test_fetch_success_pins_the_validated_ip():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request)

    page = make(handler).fetch("https://good.example/page?x=1")
    assert page.content_type == "text/html" and b"hello world" in page.content
    assert page.final_url == "https://good.example/page?x=1"
    assert seen[0].url.host == PUBLIC  # connected to the checked IP, not re-resolved
    assert seen[0].headers["host"] == "good.example"
    assert seen[0].url.path == "/page" and seen[0].url.query == b"x=1"


def test_final_url_is_normalized():
    assert make(ok).fetch("https://good.example/a?utm_source=x&b=2#frag").final_url == (
        "https://good.example/a?b=2"
    )


def test_blocked_before_any_request_when_dns_is_private():
    seen = []
    with pytest.raises(UnsafeUrlError):
        make(lambda r: seen.append(r) or ok(r)).fetch("http://evil.example/")
    assert seen == []


def test_redirect_to_private_host_is_blocked_and_never_requested():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://evil.example/admin"})

    with pytest.raises(UnsafeUrlError):
        make(handler).fetch("https://good.example/start")
    assert len(seen) == 1


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "file:///etc/passwd", "ftp://good.example/x"])
def test_redirect_to_bad_targets_is_blocked(location):
    handler = lambda request: httpx.Response(302, headers={"location": location})  # noqa: E731
    with pytest.raises(UnsafeUrlError):
        make(handler).fetch("https://good.example/start")


def test_relative_redirect_is_followed():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return ok(request)

    assert make(handler).fetch("https://good.example/start").final_url.endswith("/final")


def test_redirect_loop_is_cut_off():
    handler = lambda request: httpx.Response(302, headers={"location": "https://good.example/loop"})  # noqa: E731
    with pytest.raises(IngestionError, match="redirect"):
        make(handler).fetch("https://good.example/loop")


def test_declared_oversize_is_rejected():
    handler = lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 5000)  # noqa: E731
    with pytest.raises(IngestionError, match="too large"):
        make(handler).fetch("https://good.example/")


def test_streamed_oversize_is_rejected_without_content_length():
    def handler(request):
        def body():
            yield b"x" * 600
            yield b"x" * 600

        return httpx.Response(200, headers={"content-type": "text/html"}, content=body())

    with pytest.raises(IngestionError, match="too large"):
        make(handler).fetch("https://good.example/")


def test_pdf_url_gets_a_helpful_error():
    handler = lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")  # noqa: E731
    with pytest.raises(IngestionError, match="PDF"):
        make(handler).fetch("https://good.example/doc")


def test_unsupported_content_type_is_rejected():
    handler = lambda r: httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")  # noqa: E731
    with pytest.raises(IngestionError, match="Unsupported"):
        make(handler).fetch("https://good.example/img")


def test_plain_text_is_allowed():
    handler = lambda r: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hi")  # noqa: E731
    assert make(handler).fetch("https://good.example/t").content == b"hi"


def test_404_is_permanent_and_500_is_transient():
    not_found = lambda r: httpx.Response(404)  # noqa: E731
    broken = lambda r: httpx.Response(503)  # noqa: E731
    with pytest.raises(IngestionError):
        make(not_found).fetch("https://good.example/")
    with pytest.raises(TransientIngestionError):
        make(broken).fetch("https://good.example/")


def test_rate_limit_is_transient():
    with pytest.raises(TransientIngestionError):
        make(lambda r: httpx.Response(429)).fetch("https://good.example/")


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TransientIngestionError):
        make(handler).fetch("https://good.example/")


def test_falls_back_to_the_next_ip_when_connect_fails():
    def handler(request):
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("refused", request=request)
        return ok(request)

    assert b"hello world" in make(handler).fetch("https://multi.example/").content


def test_unknown_host_is_transient():
    with pytest.raises(TransientIngestionError):
        make(ok).fetch("https://unknown.example/")