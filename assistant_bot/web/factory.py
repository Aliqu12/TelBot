"""Auswahl des Websuche-Providers anhand der Konfiguration (WEB_SEARCH_PROVIDER)."""

from __future__ import annotations

from ..config import WebSettings
from .provider import WebProvider
from .tavily import TavilyWebProvider


def create_web_provider(settings: WebSettings) -> WebProvider:
    """Erzeugt den konfigurierten Provider.

    Neue Provider (z. B. Brave) werden hier ergänzt und in
    ``config.SUPPORTED_WEB_PROVIDERS`` eingetragen – ``WebService`` bleibt unverändert.
    """
    if settings.provider == "tavily":
        return TavilyWebProvider(settings.api_key, timeout_seconds=settings.timeout_seconds)
    raise ValueError(f"Unbekannter Websuche-Provider: {settings.provider!r}")
