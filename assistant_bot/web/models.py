"""Providerunabhängige Datenmodelle und Fehler der Websuche."""

from __future__ import annotations

from dataclasses import dataclass


class WebError(Exception):
    """Basisklasse aller Fehler der Websuche. Die Meldung ist für Nutzer verständlich formuliert."""


class WebValidationError(WebError):
    """Ungültige Eingabe (leere Suche, unzulässige URL) oder vom Provider abgelehnte Anfrage."""


class WebAuthenticationError(WebError):
    """API-Key fehlt oder wurde abgelehnt."""


class WebLimitError(WebError):
    """Tageslimit der Anwendung, Anfragelimit oder Kontingent des Providers erreicht."""


class WebConnectionError(WebError):
    """Provider nicht erreichbar, Zeitüberschreitung oder Serverfehler."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    content: str  # Ausschnitt bzw. Zusammenfassung des Treffers


@dataclass(frozen=True, slots=True)
class WebPage:
    url: str
    content: str  # bereinigter Text der Seite
