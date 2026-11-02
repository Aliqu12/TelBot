"""Vertrag, den jeder Aufgaben-Provider (Todoist, später z. B. Microsoft To Do) erfüllen muss."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Label, NewTask, Project, Section, Task, TaskChanges


class TaskProvider(Protocol):
    """Zugriff auf genau ein Aufgaben-Konto.

    Implementierungen übersetzen ihre technischen Fehler in die Fehlerklassen aus
    ``models`` (``TaskAuthenticationError``, ``TaskRateLimitError``, ...) und
    liefern Fälligkeiten in der konfigurierten Zeitzone.
    """

    name: str

    async def list_tasks(self, project_id: str | None = None) -> list[Task]:
        """Alle offenen Aufgaben (optional eines Projekts)."""
        ...

    async def get_task(self, task_id: str) -> Task:
        """Raises: TaskNotFoundError, wenn die ID unbekannt oder die Aufgabe erledigt ist."""
        ...

    async def create_task(self, task: NewTask) -> Task: ...

    async def update_task(self, task_id: str, changes: TaskChanges) -> Task: ...

    async def complete_task(self, task_id: str) -> None:
        """Erledigt eine Aufgabe; wiederkehrende Aufgaben springen zum nächsten Termin."""
        ...

    async def delete_task(self, task_id: str) -> None: ...

    async def move_task(self, task_id: str, *, project_id: str | None = None, section_id: str | None = None) -> Task:
        """Verschiebt eine Aufgabe in ein Projekt oder einen Abschnitt (genau eines von beiden angeben)."""
        ...

    async def list_completed_tasks(self, since: datetime, until: datetime, project_id: str | None = None) -> list[Task]:
        """Erledigte Aufgaben mit Erledigungszeitpunkt in [since, until). Höchstens 3 Monate Zeitraum."""
        ...

    # --- Projekte ---

    async def list_projects(self) -> list[Project]:
        """Aktive (nicht archivierte) Projekte."""
        ...

    async def list_archived_projects(self) -> list[Project]: ...

    async def create_project(self, name: str, parent_id: str | None = None) -> Project: ...

    async def rename_project(self, project_id: str, name: str) -> Project: ...

    async def archive_project(self, project_id: str) -> None: ...

    async def unarchive_project(self, project_id: str) -> None: ...

    async def delete_project(self, project_id: str) -> None:
        """Löscht das Projekt samt aller Abschnitte und Aufgaben endgültig."""
        ...

    # --- Abschnitte ---

    async def list_sections(self, project_id: str | None = None) -> list[Section]: ...

    async def create_section(self, project_id: str, name: str) -> Section: ...

    # --- Labels ---

    async def list_labels(self) -> list[Label]: ...

    async def create_label(self, name: str) -> Label: ...

    async def rename_label(self, label_id: str, name: str) -> Label: ...

    async def delete_label(self, label_id: str) -> None:
        """Löscht das Label und entfernt es von allen Aufgaben."""
        ...

    async def aclose(self) -> None: ...
