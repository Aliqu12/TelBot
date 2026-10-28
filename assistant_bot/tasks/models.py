"""Providerunabhängige Datenmodelle und Fehler der Aufgaben-Integration.

Diese Typen sind der gemeinsame Vertrag zwischen ``TaskService`` und allen
Providern. Provider-spezifische Strukturen (z. B. Todoist-JSON) verlassen die
jeweilige Provider-Implementierung nicht.

Zeitangaben sind immer zeitzonenbewusst (``datetime`` mit ``tzinfo``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


class TaskError(Exception):
    """Basisklasse aller Aufgaben-Fehler. Die Meldung ist für Nutzer verständlich formuliert."""


class TaskValidationError(TaskError):
    """Ungültige Eingabe (z. B. leerer Titel, Uhrzeit ohne Datum) oder vom Provider abgelehnte Werte."""


class TaskNotFoundError(TaskError):
    """Die Aufgabe (oder das Projekt) existiert nicht (mehr)."""


class TaskAmbiguousError(TaskError):
    """Mehrere Aufgaben passen zur Beschreibung. Es wird bewusst keine ausgewählt."""

    def __init__(self, message: str, candidates: list[Task]) -> None:
        super().__init__(message)
        self.candidates = candidates


class TaskAuthenticationError(TaskError):
    """Zugangsdaten fehlen oder wurden abgelehnt (HTTP 401/403)."""


class TaskRateLimitError(TaskError):
    """Das Anfragelimit des Providers ist erreicht (HTTP 429)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TaskConnectionError(TaskError):
    """Provider nicht erreichbar oder Serverfehler (5xx)."""


class TaskTimeoutError(TaskError):
    """Keine rechtzeitige Antwort. Bei Schreibzugriffen ist unklar, ob die Änderung angekommen ist."""


@dataclass(frozen=True, slots=True)
class TaskDue:
    """Fälligkeit einer Aufgabe in der konfigurierten Zeitzone."""

    date: date  # Kalendertag (lokal)
    time: datetime | None = None  # Zeitpunkt, falls mit Uhrzeit (zeitzonenbewusst, lokal)
    is_recurring: bool = False
    text: str = ""  # Menschenlesbare Beschreibung des Providers, z. B. "jeden Sonntag"


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str  # Provider-interne, opake ID
    content: str
    description: str = ""
    project_id: str | None = None
    priority: int = 4  # 1 = höchste (wie "P1" in Todoist), 4 = normal
    due: TaskDue | None = None
    duration_minutes: int | None = None
    labels: tuple[str, ...] = ()
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    name: str
    is_inbox: bool = False


@dataclass(frozen=True, slots=True)
class DueInput:
    """Gewünschte Fälligkeit beim Anlegen/Ändern. Genau eine Variante ist gesetzt."""

    on: date | None = None  # ganztägig
    at: datetime | None = None  # mit Uhrzeit (zeitzonenbewusst)
    recurrence: str | None = None  # wiederkehrend, natürliche Sprache (Deutsch), z. B. "jeden Sonntag"
    clear: bool = False  # Fälligkeit entfernen


@dataclass(frozen=True, slots=True)
class NewTask:
    content: str
    description: str = ""
    project_id: str | None = None
    priority: int | None = None  # 1 (höchste) bis 4 (normal); None = Standard des Providers
    due: DueInput | None = None
    duration_minutes: int | None = None
    labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskChanges:
    """Änderungen an einer Aufgabe. ``None`` = Feld unverändert lassen."""

    content: str | None = None
    description: str | None = None
    priority: int | None = None
    due: DueInput | None = None
    duration_minutes: int | None = None
    clear_duration: bool = False
    labels: tuple[str, ...] | None = field(default=None)

    @property
    def is_empty(self) -> bool:
        return (
            self.content is None
            and self.description is None
            and self.priority is None
            and self.due is None
            and self.duration_minutes is None
            and not self.clear_duration
            and self.labels is None
        )
