"""Vertrag, den jeder Websuche-Provider (Tavily, später z. B. Brave) erfüllen muss."""

from __future__ import annotations

from typing import Protocol

from .models import SearchResult, WebPage


class WebProvider(Protocol):
    """Implementierungen übersetzen ihre technischen Fehler in die Fehlerklassen aus ``models``."""

    name: str

    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...

    async def extract(self, url: str) -> WebPage:
        """Liest den Text einer Seite. Raises: WebError, wenn die Seite nicht gelesen werden kann."""
        ...

    async def aclose(self) -> None: ...
