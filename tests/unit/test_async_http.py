from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from scrapper.scrapers import async_http


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "connection", 403, 404, 408, 429, 500, 502, 503, 504])
async def test_custom_http_retries_only_temporary_errors(monkeypatch: pytest.MonkeyPatch, failure: str | int) -> None:
    monkeypatch.setattr(async_http, "_DEFAULT_RETRY_DELAY", 0)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            return httpx.Response(200)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("connection failed", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await async_http.get_checked_page(client, "https://jobs.example/search")

    if failure in {403, 404}:
        assert (response.status_code, calls) == (failure, 1)
    else:
        assert (response.status_code, calls) == (200, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["60", format_datetime(datetime.now(UTC) + timedelta(minutes=5), usegmt=True)])
async def test_retry_after_pauses_only_the_affected_host(monkeypatch: pytest.MonkeyPatch, retry_after: str) -> None:
    loop = asyncio.get_running_loop()
    clock = loop.time
    requested_hosts: list[str] = []
    elapsed = 0

    def respond(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if len(requested_hosts) == 1:
            return httpx.Response(429, headers={"retry-after": retry_after})
        return httpx.Response(200)

    monkeypatch.setattr(loop, "time", lambda: clock() + elapsed)
    throttle = async_http.HostThrottle(1, 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for path in ("first", "second"):
            with pytest.raises(async_http.HostCooldownError, match="HTTP 429"):
                await throttle.get(client, f"https://jobs.example/{path}")
        assert (await throttle.get(client, "https://other.example/search")).status_code == 200
        elapsed = 600
        assert (await throttle.get(client, "https://jobs.example/third")).status_code == 200

    assert requested_hosts == ["jobs.example", "other.example", "jobs.example"]
