"""Tests für TavilyWebProvider – ausschließlich gegen einen gefälschten HTTP-Transport (kein echter Key)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from assistant_bot.config import WebSettings
from assistant_bot.web.factory import create_web_provider
from assistant_bot.web.models import (
    WebAuthenticationError,
    WebConnectionError,
    WebError,
    WebLimitError,
    WebValidationError,
)
from assistant_bot.web.tavily import TavilyWebProvider

KEY = "tvly-test-dummy-key-not-real"


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def provider(*responses: httpx.Response | Exception) -> tuple[TavilyWebProvider, Recorder]:
    recorder = Recorder(*responses)
    return TavilyWebProvider(KEY, transport=httpx.MockTransport(recorder)), recorder


def test_search_sends_basic_request_and_parses_results() -> None:
    web, recorder = provider(
        httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Pi 5", "url": "https://example.com/pi", "content": "Kostet 80 Euro.", "score": 0.9},
                    {"title": "Ohne URL", "content": "wird ignoriert"},
                ]
            },
        )
    )

    results = asyncio.run(web.search("Raspberry Pi 5 Preis", 3))

    request = recorder.requests[0]
    body = json.loads(request.content)
    assert request.url == "https://api.tavily.com/search"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert body["query"] == "Raspberry Pi 5 Preis"
    assert body["max_results"] == 3
    assert body["search_depth"] == "basic"
    assert [(r.title, r.url, r.content) for r in results] == [("Pi 5", "https://example.com/pi", "Kostet 80 Euro.")]


def test_extract_returns_page_text() -> None:
    web, recorder = provider(
        httpx.Response(200, json={"results": [{"url": "https://example.com/a", "raw_content": "  Text  "}]})
    )

    page = asyncio.run(web.extract("https://example.com/a"))

    body = json.loads(recorder.requests[0].content)
    assert recorder.requests[0].url == "https://api.tavily.com/extract"
    assert body["urls"] == ["https://example.com/a"]
    assert (page.url, page.content) == ("https://example.com/a", "Text")


def test_extract_failure_raises() -> None:
    web, _ = provider(
        httpx.Response(200, json={"results": [], "failed_results": [{"url": "https://x.de", "error": "x"}]})
    )

    with pytest.raises(WebError, match="nicht gelesen"):
        asyncio.run(web.extract("https://x.de"))


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, WebAuthenticationError),
        (429, WebLimitError),
        (432, WebLimitError),
        (433, WebLimitError),
        (434, WebLimitError),
        (400, WebValidationError),
        (500, WebConnectionError),
        (418, WebError),
    ],
)
def test_http_errors_are_mapped(status: int, error: type[WebError]) -> None:
    web, _ = provider(httpx.Response(status, json={"detail": {"error": "x"}}))

    with pytest.raises(error) as excinfo:
        asyncio.run(web.search("test", 5))

    assert KEY not in str(excinfo.value)


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("timeout"), httpx.ConnectError("down")],
)
def test_transport_errors_are_mapped(exc: Exception) -> None:
    web, _ = provider(exc)

    with pytest.raises(WebConnectionError):
        asyncio.run(web.search("test", 5))


def test_unexpected_payload_raises() -> None:
    web, _ = provider(httpx.Response(200, json={"foo": 1}))

    with pytest.raises(WebConnectionError):
        asyncio.run(web.search("test", 5))


def test_factory_creates_tavily_provider() -> None:
    web = create_web_provider(WebSettings(provider="tavily", api_key=KEY))

    assert isinstance(web, TavilyWebProvider)
    asyncio.run(web.aclose())
