"""TavilyWebProvider: Websuche und Seitenextraktion über die Tavily-API (httpx).

Dokumentation: https://docs.tavily.com/documentation/api-reference/endpoint/search

- Authentifizierung per API-Key (``Authorization: Bearer ...``).
- Seiten werden von Tavily abgerufen, nicht von dieser Anwendung. Der Bot ruft
  also selbst keine beliebigen URLs auf (kein SSRF ins eigene Netz).
- Suchanfragen werden bewusst nicht geloggt (können persönliche Daten enthalten).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import (
    SearchResult,
    WebAuthenticationError,
    WebConnectionError,
    WebError,
    WebLimitError,
    WebPage,
    WebValidationError,
)

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.tavily.com"


class TavilyWebProvider:
    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        body = {
            "query": query,
            "max_results": max_results,
            # "basic" kostet 1 Credit; "advanced" (2 Credits) wird bewusst nicht genutzt.
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        data = await self._post("/search", body)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise WebConnectionError("Die Websuche hat unerwartet geantwortet.")
        return [
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                content=str(item.get("content") or "").strip(),
            )
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]

    async def extract(self, url: str) -> WebPage:
        data = await self._post("/extract", {"urls": [url], "extract_depth": "basic", "format": "text"})
        results = data.get("results") if isinstance(data, dict) else None
        for item in results if isinstance(results, list) else []:
            if isinstance(item, dict) and (content := str(item.get("raw_content") or "").strip()):
                return WebPage(url=str(item.get("url") or url), content=content)
        raise WebError("Die Seite konnte nicht gelesen werden (nicht erreichbar, gesperrt oder ohne Text).")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            logger.warning("Tavily: Zeitüberschreitung (%s).", path)
            raise WebConnectionError("Die Websuche hat nicht rechtzeitig geantwortet.") from exc
        except httpx.TransportError as exc:
            logger.warning("Tavily nicht erreichbar: %s", type(exc).__name__)
            raise WebConnectionError("Die Websuche ist gerade nicht erreichbar.") from exc

        status = response.status_code
        if status < 300:
            try:
                return response.json()
            except ValueError as exc:
                raise WebConnectionError("Die Websuche hat unerwartet geantwortet.") from exc

        logger.warning("Tavily-Anfrage abgelehnt: %s -> HTTP %s", path, status)
        if status in (401, 403):
            raise WebAuthenticationError("Die Websuche hat den Zugriff abgelehnt. Bitte TAVILY_API_KEY prüfen.")
        if status == 429:
            raise WebLimitError("Zu viele Suchanfragen in kurzer Zeit. Bitte gleich nochmal versuchen.")
        if status in (432, 433):
            raise WebLimitError("Das Kontingent der Websuche ist aufgebraucht.")
        if status == 434:
            raise WebLimitError("Die Websuche ist gerade überlastet. Bitte später nochmal versuchen.")
        if status in (400, 422):
            raise WebValidationError("Die Websuche hat die Anfrage abgelehnt.")
        if status >= 500:
            raise WebConnectionError("Die Websuche ist gerade nicht erreichbar (Serverfehler).")
        raise WebError(f"Die Websuche ist fehlgeschlagen (HTTP {status}).")
