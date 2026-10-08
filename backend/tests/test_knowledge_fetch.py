from __future__ import annotations

import hashlib

import httpx
import pytest

from app.knowledge.fetch import (
    FetchPolicy,
    OnlineSearchDisabledError,
    UnsafeUrlError,
    fetch_web_document,
    validate_fetch_url,
)


def test_fetch_policy_blocks_private_and_redirect_targets() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html", "application/pdf"],
        }
    )

    with pytest.raises(UnsafeUrlError, match="domain"):
        validate_fetch_url("https://example.com/report", policy)
    with pytest.raises(UnsafeUrlError, match="private"):
        validate_fetch_url("http://127.0.0.1/report", policy)


def test_fetch_policy_allows_exact_domain_and_subdomain() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
        }
    )

    assert policy.is_allowed("https://cea.gov.cn/report")
    assert policy.is_allowed("https://www.cea.gov.cn/report")
    assert not policy.is_allowed("https://notcea.gov.cn/report")
    assert not policy.is_allowed("ftp://cea.gov.cn/report")


@pytest.mark.asyncio
async def test_online_search_disabled_returns_before_dns_or_http() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
        }
    )
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OnlineSearchDisabledError):
            await fetch_web_document(
                "https://cea.gov.cn/report",
                policy,
                client,
                online_search_enabled=False,
            )

    assert calls == []


@pytest.mark.asyncio
async def test_fetch_rejects_private_dns_answer() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
        }
    )

    async def resolver(host: str, port: int = 443) -> list[str]:
        del port
        assert host == "rebind.cea.gov.cn"
        return ["127.0.0.1"]

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeUrlError, match="private"):
            await fetch_web_document(
                "https://rebind.cea.gov.cn/report",
                policy,
                client,
                resolver=resolver,
                online_search_enabled=True,
            )


@pytest.mark.asyncio
async def test_fetch_revalidates_every_redirect_target() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
        }
    )

    async def resolver(host: str, port: int = 443) -> list[str]:
        del port
        return {
            "cea.gov.cn": ["93.184.216.34"],
            "www.cea.gov.cn": ["93.184.216.34"],
        }[host]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cea.gov.cn":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/report"})
        return httpx.Response(200, text="unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeUrlError, match="private"):
            await fetch_web_document(
                "https://cea.gov.cn/report",
                policy,
                client,
                resolver=resolver,
                online_search_enabled=True,
            )


@pytest.mark.asyncio
async def test_fetch_streams_body_checksum_and_safe_headers() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
            "max_bytes": 1024,
        }
    )

    async def resolver(host: str, port: int = 443) -> list[str]:
        del port
        assert host == "cea.gov.cn"
        return ["93.184.216.34"]

    body = b"<html><body>report</body></html>"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "secret=value",
                "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetched = await fetch_web_document(
            "https://cea.gov.cn/report",
            policy,
            client,
            resolver=resolver,
            online_search_enabled=True,
        )

    assert fetched.final_url == "https://cea.gov.cn/report"
    assert fetched.http_status == 200
    assert fetched.body == body
    assert fetched.text == body.decode("utf-8")
    assert fetched.checksum == hashlib.sha256(body).hexdigest()
    assert fetched.headers["content-type"] == "text/html; charset=utf-8"
    assert fetched.headers["last-modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert "set-cookie" not in fetched.headers


@pytest.mark.asyncio
async def test_fetch_rejects_disallowed_content_type() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
        }
    )

    async def resolver(host: str, port: int = 443) -> list[str]:
        del port
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain", headers={"content-type": "text/plain"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeUrlError, match="content-type"):
            await fetch_web_document(
                "https://cea.gov.cn/report",
                policy,
                client,
                resolver=resolver,
                online_search_enabled=True,
            )


@pytest.mark.asyncio
async def test_fetch_stops_after_max_redirects() -> None:
    policy = FetchPolicy.from_yaml(
        {
            "allowed_domains": ["cea.gov.cn"],
            "allowed_content_types": ["text/html"],
            "max_redirects": 1,
        }
    )

    async def resolver(host: str, port: int = 443) -> list[str]:
        del port
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://www.cea.gov.cn/report"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeUrlError, match="redirect"):
            await fetch_web_document(
                "https://cea.gov.cn/report",
                policy,
                client,
                resolver=resolver,
                online_search_enabled=True,
            )
