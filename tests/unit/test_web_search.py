from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from scrapper.config import SearchConfig
from scrapper.scrapers import web_search


@pytest.mark.parametrize("target", ["https://jobs.example/offre/123?q=communication", "javascript:alert(1)"])
def test_bing_decodes_redirect_and_validates_destination(target: str) -> None:
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?u=a1{encoded}&ntb=1"
    assert web_search._unwrap_bing(href) == (target if target.startswith("https:") else None)


@pytest.mark.parametrize("href", [None, "https://www.bing.com/ck/a?u=a1a", "https://www.bing.com/ck/a"])
def test_bing_rejects_invalid_redirects(href: str | None) -> None:
    assert web_search._unwrap_bing(href) is None


def test_free_search_uses_bing_after_blocked_google_and_duckduckgo() -> None:
    target = "https://jobs.example/offre/123"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            return httpx.Response(429)
        if "duckduckgo" in request.url.host:
            return httpx.Response(202, text='<form id="challenge-form"></form>')
        return httpx.Response(200, text=(
            f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{encoded}">'
            'Communication</a></h2><p>Job in Toulouse</p></li>'
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await web_search._search(client, "communication Toulouse", 3, SearchConfig())

    assert asyncio.run(run()) == [web_search.SearchResult(target, "Communication", "Job in Toulouse")]


@pytest.mark.parametrize("bing_blocked", [True, False])
def test_free_search_distinguishes_total_failure_from_successful_empty_search(bing_blocked: bool) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            return httpx.Response(429)
        if "duckduckgo" in request.url.host:
            return httpx.Response(202, text="challenge")
        return httpx.Response(200, text="Veuillez résoudre le défi" if bing_blocked else "No results")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await web_search._search(client, "communication Toulouse", 3, SearchConfig())

    if bing_blocked:
        with pytest.raises(web_search.SearchUnavailableError, match="All free search providers failed"):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == []


@pytest.mark.parametrize(
    ("url", "accepted"),
    [("https://en.wikipedia.org/wiki/Communication", False),
     ("https://example.org/articles/communication", False),
     ("https://example.org/jobs/communication-123", True)],
)
def test_web_search_requires_listing_evidence_beyond_job_keywords(url: str, accepted: bool) -> None:
    html = '<h1>Chargé de communication</h1><main>Communication à Toulouse. CDI, emploi, poste.</main>'

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await web_search._extract_offer(
                client, web_search.SearchResult(url, "Communication", None), "communication", "Toulouse",
                SearchConfig(location_scope={"enabled": False}),
            )

    assert (asyncio.run(run()) is not None) is accepted
