"""Download web pages with SSRF protection.

For every hop: validate URL -> resolve DNS -> require ALL addresses public -> connect to the
validated IP (Host header + TLS server name stay the original host) -> follow redirects
manually so each destination is checked again. Size, time and content-type are bounded.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from app.core.exceptions import IngestionError, TransientIngestionError
from app.ingestion.url_safety import (
    IPAddress,
    Resolver,
    ValidatedUrl,
    assert_safe_destination,
    default_resolver,
)

log = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
USER_AGENT = "PersonalKnowledgeAgent/0.1 (personal knowledge tool)"


@dataclass(frozen=True)
class FetchedPage:
    final_url: str  # normalized URL after redirects
    content: bytes
    content_type: str
    fetched_at: datetime


@dataclass(frozen=True)
class _Redirect:
    location: str


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        total_timeout: float = 30.0,
        max_bytes: int = 5 * 1024 * 1024,
        max_redirects: int = 5,
        resolver: Resolver = default_resolver,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._total_timeout = total_timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver
        self._transport = transport  # tests inject httpx.MockTransport here

    def fetch(self, url: str) -> FetchedPage:
        current = url
        deadline = time.monotonic() + self._total_timeout
        for _ in range(self._max_redirects + 1):
            validated, ips = assert_safe_destination(current, self._resolver)
            result = self._get(validated, ips, deadline)
            if isinstance(result, _Redirect):
                current = urljoin(validated.normalized, result.location)
                continue
            content, content_type = result
            return FetchedPage(
                final_url=validated.normalized,
                content=content,
                content_type=content_type,
                fetched_at=datetime.now(timezone.utc),
            )
        raise IngestionError("Too many redirects.")

    def _get(
        self, url: ValidatedUrl, ips: list[IPAddress], deadline: float
    ) -> _Redirect | tuple[bytes, str]:
        last_error: Exception | None = None
        for ip in ips:
            literal = f"[{ip}]" if ip.version == 6 else str(ip)
            target = f"{url.scheme}://{literal}:{url.port}{url.request_target}"
            headers = {
                "Host": url.host,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
            }
            try:
                return self._request(target, headers, url.host, deadline)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc  # try the next address
            except httpx.TimeoutException as exc:
                raise TransientIngestionError("The site took too long to respond.") from exc
            except httpx.HTTPError as exc:
                raise TransientIngestionError("Could not download the page.") from exc
        raise TransientIngestionError("Could not connect to the site.") from last_error

    def _request(
        self, target: str, headers: dict[str, str], host: str, deadline: float
    ) -> _Redirect | tuple[bytes, str]:
        with httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
            trust_env=False,  # never route through proxies from environment variables
        ) as client:
            with client.stream(
                "GET", target, headers=headers, extensions={"sni_hostname": host}
            ) as response:
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise IngestionError("The site sent a redirect without a destination.")
                    return _Redirect(location)
                if status in (408, 425, 429) or status >= 500:
                    raise TransientIngestionError(f"The site returned HTTP {status}; try again later.")
                if status >= 400 or not 200 <= status < 300:
                    raise IngestionError(f"The site returned HTTP {status}.")

                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type == "application/pdf":
                    raise IngestionError(
                        "This URL is a PDF. Download it and upload the file instead."
                    )
                if content_type not in ALLOWED_CONTENT_TYPES:
                    raise IngestionError(f"Unsupported content type: {content_type or 'unknown'}.")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._max_bytes:
                    raise IngestionError("The page is too large.")

                parts: list[bytes] = []
                total = 0
                for part in response.iter_bytes():  # decoded bytes: also bounds decompression
                    total += len(part)
                    if total > self._max_bytes:
                        raise IngestionError("The page is too large.")
                    if time.monotonic() > deadline:
                        raise TransientIngestionError("The site was too slow to download.")
                    parts.append(part)
                return b"".join(parts), content_type