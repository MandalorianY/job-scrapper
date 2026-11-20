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
    config = SearchConfig()
    query = 'site:jobs.example "chargé de communication" emploi CDI "Toulouse, France" -stage -alternance'

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == f"www.{config.search_engines.google_domain}":
            assert request.url.params["q"] == query
            return httpx.Response(429)
        if "duckduckgo" in request.url.host:
            assert request.url.params["q"] == query
            return httpx.Response(202, text='<form id="challenge-form"></form>')
        assert request.url.params["q"] == query
        return httpx.Response(200, text=(
            f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{encoded}">'
            'Communication</a></h2><p>Job in Toulouse</p></li>'
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await web_search._search(client, query, 3, config)

    assert asyncio.run(run()) == [web_search.SearchResult(target, "Communication", "Job in Toulouse")]


def test_free_google_search_uses_configured_domain() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.google.co.uk"
        return httpx.Response(200, text='<a href="https://jobs.example/job/1"><h3>Job title</h3></a>')

    async def run():
        config = SearchConfig(search_engines={"google_domain": "www.google.co.uk"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await web_search._search_google(client, "example role", 1, config)

    assert [result.url for result in asyncio.run(run())] == ["https://jobs.example/job/1"]


@pytest.mark.parametrize("bing_blocked", [True, False])
def test_free_search_distinguishes_total_failure_from_successful_empty_search(bing_blocked: bool) -> None:
    config = SearchConfig()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == f"www.{config.search_engines.google_domain}":
            return httpx.Response(429)
        if "duckduckgo" in request.url.host:
            return httpx.Response(202, text="challenge")
        return httpx.Response(200, text="Veuillez résoudre le défi" if bing_blocked else "No results")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await web_search._search(client, "communication Toulouse", 3, config)

    if bing_blocked:
        with pytest.raises(web_search.SearchUnavailableError, match="All free search providers failed"):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) == []
