"""WebService: Prüft Eingaben, begrenzt Ergebnisgröße und Tagesverbrauch der Websuche."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from datetime import date, datetime, tzinfo
from urllib.parse import urlsplit

from .models import SearchResult, WebLimitError, WebPage, WebValidationError
from .provider import WebProvider

logger = logging.getLogger(__name__)

MAX_QUERY_CHARS = 400
MAX_URL_CHARS = 2000
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".intranet")


class WebService:
    def __init__(
        self,
        provider: WebProvider,
        *,
        max_results: int = 5,
        max_chars: int = 4000,
        max_calls_per_day: int = 30,
        timezone: tzinfo | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._provider = provider
        self._max_results = max_results
        self._max_chars = max_chars
        self._max_calls_per_day = max_calls_per_day
        self._today = today or (lambda: datetime.now(timezone).date())
        # Zähler nur im Speicher: Ein Neustart setzt ihn zurück. Für ein Kostenlimit im Chat reicht das.
        self._calls_day: date | None = None
        self._calls = 0

    @property
    def provider_name(self) -> str:
        return self._provider.name

    async def search(self, query: str) -> list[SearchResult]:
        query = " ".join(query.split())
        if not query:
            raise WebValidationError("Die Suchanfrage ist leer.")
        if len(query) > MAX_QUERY_CHARS:
            raise WebValidationError(f"Die Suchanfrage ist zu lang (höchstens {MAX_QUERY_CHARS} Zeichen).")
        self._consume_call()
        results = await self._provider.search(query, self._max_results)
        return [
            SearchResult(title=r.title, url=r.url, content=_truncate(r.content, self._max_chars))
            for r in results[: self._max_results]
        ]

    async def read(self, url: str) -> WebPage:
        url = validate_url(url)
        self._consume_call()
        page = await self._provider.extract(url)
        return WebPage(url=page.url, content=_truncate(page.content, self._max_chars))

    async def aclose(self) -> None:
        await self._provider.aclose()

    def _consume_call(self) -> None:
        if self._max_calls_per_day <= 0:
            return
        today = self._today()
        if self._calls_day != today:
            self._calls_day, self._calls = today, 0
        if self._calls >= self._max_calls_per_day:
            logger.warning("Tageslimit der Websuche erreicht (%d Anfragen).", self._max_calls_per_day)
            raise WebLimitError("Das Tageslimit für die Websuche ist erreicht. Morgen geht es wieder.")
        self._calls += 1


def validate_url(url: str) -> str:
    """Erlaubt nur öffentliche http(s)-URLs mit Hostnamen (keine IP-Adressen, kein localhost, keine Zugangsdaten)."""
    url = url.strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise WebValidationError("Die Adresse ist leer oder zu lang.")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        parts.port  # noqa: B018 - löst bei ungültigem Port ValueError aus
    except ValueError:
        raise WebValidationError("Die Adresse ist ungültig.") from None
    if parts.scheme not in ("http", "https") or not host:
        raise WebValidationError("Nur Webadressen mit http:// oder https:// sind erlaubt.")
    if parts.username or parts.password:
        raise WebValidationError("Adressen mit Zugangsdaten sind nicht erlaubt.")
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES) or "." not in host or _is_ip(host):
        raise WebValidationError("Nur öffentliche Webseiten können gelesen werden.")
    return url


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " […gekürzt]"
