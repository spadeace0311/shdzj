from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import yaml


class UnsafeUrlError(ValueError):
    """Raised when a fetch target violates the source whitelist or network policy."""


class OnlineSearchDisabledError(RuntimeError):
    """Raised when online fetching is disabled."""


class FetchClient(Protocol):
    def stream(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any: ...


AddressResolver = Any


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    allowed_domains: tuple[str, ...]
    allowed_content_types: tuple[str, ...]
    max_bytes: int
    max_redirects: int

    @classmethod
    def from_yaml(cls, payload: dict[str, Any]) -> "FetchPolicy":
        domains = payload.get("allowed_domains")
        content_types = payload.get("allowed_content_types")
        if not isinstance(domains, list) or not domains:
            raise ValueError("allowed_domains must be a non-empty list")
        if not isinstance(content_types, list) or not content_types:
            raise ValueError("allowed_content_types must be a non-empty list")
        max_bytes = int(payload.get("max_bytes", 10_485_760))
        max_redirects = int(payload.get("max_redirects", 3))
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        return cls(
            allowed_domains=tuple(
                _normalize_domain(str(domain)) for domain in domains
            ),
            allowed_content_types=tuple(
                _normalize_content_type(str(value)) for value in content_types
            ),
            max_bytes=max_bytes,
            max_redirects=max_redirects,
        )

    def is_allowed(self, url: str) -> bool:
        try:
            _validate_target(url, self)
        except UnsafeUrlError:
            return False
        return True


@dataclass(frozen=True, slots=True)
class FetchedWebDocument:
    requested_url: str
    final_url: str
    http_status: int
    content_type: str | None
    headers: dict[str, str]
    body: bytes
    checksum: str
    fetched_at: datetime

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


_SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "last-modified",
    "etag",
    "cache-control",
}
_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
}
_METADATA_SUFFIXES = (
    ".metadata.google.internal",
    ".metadata.azure.internal",
    ".metadata.aws.internal",
)
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


def validate_fetch_url(url: str, policy: FetchPolicy) -> None:
    _validate_target(url, policy)


async def fetch_web_document(
    url: str,
    policy: FetchPolicy,
    client: Any,
    *,
    resolver: Any = None,
    online_search_enabled: bool | None = None,
) -> FetchedWebDocument:
    enabled = (
        _settings().online_search_enabled
        if online_search_enabled is None
        else online_search_enabled
    )
    if not enabled:
        raise OnlineSearchDisabledError("online search is disabled")

    requested_url = url
    current_url = url
    for redirect_count in range(policy.max_redirects + 1):
        validate_fetch_url(current_url, policy)
        await _validate_resolved_addresses(current_url, resolver)
        async with _stream_request(client, current_url) as response:
            if response.status_code in _REDIRECT_STATUS_CODES:
                location = response.headers.get("location")
                if location is None:
                    break
                if redirect_count >= policy.max_redirects:
                    raise UnsafeUrlError("too many redirects")
                current_url = urljoin(current_url, location)
                continue

            content_type = _response_content_type(response.headers)
            _validate_content_type(content_type, policy)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > policy.max_bytes:
                    raise UnsafeUrlError(
                        f"download exceeds max_bytes={policy.max_bytes}"
                    )

            return FetchedWebDocument(
                requested_url=requested_url,
                final_url=current_url,
                http_status=int(response.status_code),
                content_type=content_type,
                headers=_safe_headers(response.headers),
                body=bytes(body),
                checksum=hashlib.sha256(body).hexdigest(),
                fetched_at=datetime.now(UTC),
            )

    raise UnsafeUrlError("redirect did not produce a final response")


def load_fetch_policy(path: str | Path | None = None) -> FetchPolicy:
    config_path = Path(path or "/config/knowledge/source-whitelist.yaml")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("knowledge source whitelist must be a mapping")
    return FetchPolicy.from_yaml(payload)


def _validate_target(url: str, policy: FetchPolicy) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeUrlError("invalid URL") from exc
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("only http and https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL is missing a host")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")
    if parsed.port not in {None, 80, 443}:
        raise UnsafeUrlError("non-standard ports are not allowed")

    host = parsed.hostname.rstrip(".").lower()
    if host in _METADATA_HOSTS or host.endswith(_METADATA_SUFFIXES):
        raise UnsafeUrlError("cloud metadata address is blocked")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _ensure_global_unicast(ip)
    elif not _domain_is_allowed(host, policy.allowed_domains):
        raise UnsafeUrlError("domain is not in the allowed source whitelist")


async def _validate_resolved_addresses(url: str, resolver: Any) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise UnsafeUrlError("URL is missing a host")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if resolver is not None:
        addresses = resolver(host, port)
        if asyncio.iscoroutine(addresses):
            addresses = await addresses
    else:
        addresses = await _system_resolve(host, port)
    if not isinstance(addresses, list) and not isinstance(addresses, tuple):
        raise UnsafeUrlError("DNS resolver returned an invalid result")
    if not addresses:
        raise UnsafeUrlError("DNS did not resolve the host")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeUrlError("DNS returned an invalid address") from exc
        _ensure_global_unicast(ip)


async def _system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    info = await loop.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
    )
    return [str(item[4][0]) for item in info]


def _ensure_global_unicast(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if ip.is_loopback:
        raise UnsafeUrlError("loopback/private address is blocked")
    if ip.is_private:
        raise UnsafeUrlError("private address is blocked")
    if ip.is_link_local:
        raise UnsafeUrlError("link-local address is blocked")
    if ip.is_multicast:
        raise UnsafeUrlError("multicast address is blocked")
    if ip.is_reserved:
        raise UnsafeUrlError("reserved address is blocked")
    if ip.is_unspecified:
        raise UnsafeUrlError("unspecified address is blocked")


@asynccontextmanager
async def _stream_request(client: Any, url: str) -> Any:
    if hasattr(client, "stream"):
        async with client.stream("GET", url, follow_redirects=False) as response:
            yield response
    else:
        response = await client.get(url, follow_redirects=False)
        yield response


def _validate_content_type(
    content_type: str | None,
    policy: FetchPolicy,
) -> None:
    normalized = _normalize_content_type(content_type or "")
    if not normalized:
        raise UnsafeUrlError("response is missing content-type")
    if normalized not in policy.allowed_content_types:
        raise UnsafeUrlError("response content-type is not allowed")


def _response_content_type(headers: Any) -> str | None:
    if hasattr(headers, "get"):
        return headers.get("content-type")
    return dict(headers).get("content-type")


def _safe_headers(headers: Any) -> dict[str, str]:
    if hasattr(headers, "items"):
        raw_headers = headers.items()
    else:
        raw_headers = dict(headers).items()
    return {
        str(key).lower(): str(value)
        for key, value in raw_headers
        if str(key).lower() in _SAFE_RESPONSE_HEADERS
    }


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not domain:
        raise ValueError("allowed domain must not be empty")
    if any(character in domain for character in ("/", "\\", "@", ":")):
        raise ValueError(f"invalid allowed domain: {value}")
    return domain


def _normalize_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _domain_is_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in allowed_domains
    )


def _settings():
    from app.config import settings

    return settings
