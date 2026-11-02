"""TaskService: providerunabhängige Aufgabenlogik.

- Filter nach Zeitraum (heute, morgen, überfällig, 7 Tage, Datum, ohne Datum) in
  der konfigurierten Zeitzone.
- Nummerierung der zuletzt gezeigten Aufgaben pro Chat ("erledige Nummer 2") und
  Suche nach Namen mit Mehrdeutigkeitsprüfung: Passen mehrere Aufgaben, wird
  nachgefragt statt geraten.
- Schutz vor Duplikaten beim Anlegen (gleiche Aufgabe kurz hintereinander).
- Schutz wiederkehrender Aufgaben: Ein festes Datum beendet die Wiederholung
  nur, wenn das ausdrücklich gewünscht ist.
- Löschen (Aufgaben, Projekte, Labels) nur nach Bestätigung per Telegram-Button
  (``confirm_deletion``); das Sprachmodell kann Löschungen lediglich vorbereiten.
- Projekte, Abschnitte und Labels werden über ihren Namen angesprochen
  (eindeutiger Treffer nötig, sonst Nachfrage). Duplikate werden abgelehnt.
- Erledigte Aufgaben eines Zeitraums (Basis für Rückblicke).

Zustände (Übersichten, Duplikat-Schutz, Löschanfragen) liegen bewusst nur im
Arbeitsspeicher.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from enum import Enum
from typing import TypeVar

from .models import (
    DueInput,
    Label,
    NewTask,
    Project,
    Section,
    Task,
    TaskAmbiguousError,
    TaskChanges,
    TaskDue,
    TaskError,
    TaskNotFoundError,
    TaskTimeoutError,
    TaskValidationError,
)
from .provider import TaskProvider

logger = logging.getLogger(__name__)

SCOPES = ("today", "tomorrow", "overdue", "week", "date", "no_date", "all")
MAX_RESULTS = 50
MAX_CANDIDATES = 10
MAX_CONTENT_CHARS = 500
MAX_DESCRIPTION_CHARS = 4000
MAX_RECURRENCE_CHARS = 100
MAX_QUERY_CHARS = 200
MAX_LABELS = 10
MAX_LABEL_CHARS = 60
MAX_DURATION_MINUTES = 1440
MAX_NAME_CHARS = 120
MAX_COMPLETED_RESULTS = 100
MAX_COMPLETED_RANGE = timedelta(days=92)  # Todoist liefert erledigte Aufgaben für höchstens 3 Monate
LISTING_TTL_SECONDS = 3600
DELETION_TTL_SECONDS = 300
CREATE_DEDUPE_SECONDS = 120

_T = TypeVar("_T")


class DeletionStatus(Enum):
    PENDING = "pending"
    DELETING = "deleting"
    DONE = "done"


class DeletionKind(Enum):
    TASK = "task"
    PROJECT = "project"
    LABEL = "label"


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    deletion_id: str
    kind: DeletionKind
    target_id: str
    name: str
    status: DeletionStatus
    expires_at: float  # Zeitpunkt der Uhr des Services (monoton)
    task: Task | None = None  # nur bei Aufgaben
    open_task_count: int | None = None  # Projekte: offene Aufgaben (inkl. Unterprojekte); Labels: Aufgaben mit Label
    subproject_count: int = 0  # nur bei Projekten


class DeletionError(TaskError):
    """Die Löschfreigabe ist nicht (mehr) gültig."""


class DeletionNotFoundError(DeletionError):
    pass


class DeletionExpiredError(DeletionError):
    pass


class DeletionAlreadyDoneError(DeletionError):
    pass


@dataclass(frozen=True, slots=True)
class TaskListing:
    tasks: list[Task]
    total: int  # Treffer vor Begrenzung auf ``limit``
    overdue_count: int  # nur bei scope="today" gefüllt: offene Aufgaben vor heute


@dataclass(frozen=True, slots=True)
class CreateResult:
    task: Task
    duplicate: bool = False  # True = identische Aufgabe wurde gerade erst angelegt, nichts Neues erstellt


@dataclass(frozen=True, slots=True)
class CompleteResult:
    task: Task
    next_due: TaskDue | None = None  # bei wiederkehrenden Aufgaben: nächster Termin


@dataclass(frozen=True, slots=True)
class MoveResult:
    task: Task
    project_name: str
    section_name: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedListing:
    tasks: list[Task]  # nach Erledigungszeitpunkt sortiert, auf ``limit`` begrenzt
    total: int  # Treffer vor der Begrenzung
    project_names: dict[str, str]  # Projekt-ID -> Name (nur aktive Projekte)


@dataclass
class _Listing:
    task_ids: list[str]
    expires_at: float


@dataclass
class _DeletionRecord:
    deletion: PendingDeletion
    needs_review: bool


@dataclass
class _RecentCreate:
    created_at: float
    task: Task | None  # None = läuft noch oder Ergebnis unklar (Zeitüberschreitung)
    uncertain: bool = False


class TaskService:
    def __init__(
        self,
        provider: TaskProvider,
        *,
        timezone: tzinfo,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._timezone = timezone
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone))
        self._listings: dict[int, _Listing] = {}
        self._deletions: dict[int, _DeletionRecord] = {}
        self._recent_creates: dict[tuple, _RecentCreate] = {}

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def timezone(self) -> tzinfo:
        return self._timezone

    def now(self) -> datetime:
        return self._now().astimezone(self._timezone)

    async def aclose(self) -> None:
        await self._provider.aclose()

    # --- Lesen ----------------------------------------------------------------

    async def list_tasks(
        self,
        chat_id: int,
        *,
        scope: str = "today",
        on: date | None = None,
        project: str | None = None,
        search: str | None = None,
        limit: int = 20,
    ) -> TaskListing:
        if scope not in SCOPES:
            raise TaskValidationError(f"Unbekannter Zeitraum {scope!r} (möglich: {', '.join(SCOPES)}).")
        if scope == "date" and on is None:
            raise TaskValidationError("Für scope='date' bitte ein Datum angeben.")
        search = _clean_query(search)
        project_id = (await self._find_project(project)).project_id if project else None

        now = self.now()
        tasks = await self._provider.list_tasks(project_id)
        matching = [t for t in tasks if _in_scope(t, scope, on, now)]
        if search:
            matching = [t for t in matching if _matches(t, search)]
        matching.sort(key=_sort_key)
        shown = matching[: max(1, min(MAX_RESULTS, limit))]
        overdue = 0
        if scope == "today":
            overdue = sum(1 for t in tasks if t.due is not None and t.due.date < now.date())
        self._remember_listing(chat_id, shown)
        return TaskListing(tasks=shown, total=len(matching), overdue_count=overdue)

    async def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        projects = await self._provider.list_projects()
        if include_archived:
            projects = projects + await self._provider.list_archived_projects()
        return projects

    async def list_open_tasks(self) -> list[Task]:
        """Alle offenen Aufgaben ohne Filter und ohne Übersichtsnummern (für Hintergrundjobs wie Erinnerungen)."""
        return await self._provider.list_tasks()

    # --- Anlegen --------------------------------------------------------------

    async def create_task(
        self,
        chat_id: int,
        *,
        content: str,
        description: str | None = None,
        due_date: date | None = None,
        due_time: dtime | None = None,
        recurrence: str | None = None,
        duration_minutes: int | None = None,
        priority: int | None = None,
        project: str | None = None,
        labels: Sequence[str] | None = None,
    ) -> CreateResult:
        content = _clean_content(content)
        due = self._due_input(due_date, due_time, recurrence)
        if duration_minutes is not None:
            _validate_duration(duration_minutes)
            if due is None or (due.at is None and due.recurrence is None):
                raise TaskValidationError("Eine Dauer ist nur zusammen mit einer Uhrzeit möglich.")
        project_id = (await self._find_project(project)).project_id if project else None
        new_task = NewTask(
            content=content,
            description=_clean_description(description),
            project_id=project_id,
            priority=_validate_priority(priority),
            due=due,
            duration_minutes=duration_minutes,
            labels=_clean_labels(labels),
        )

        key = (chat_id, " ".join(content.casefold().split()), due, project_id)
        self._prune_recent_creates()
        recent = self._recent_creates.get(key)
        if recent is not None:
            if recent.task is not None:
                logger.info("Aufgabe nicht erneut angelegt (Duplikat innerhalb von %ss).", CREATE_DEDUPE_SECONDS)
                return CreateResult(task=recent.task, duplicate=True)
            if recent.uncertain:
                raise TaskValidationError(
                    "Diese Aufgabe wurde gerade erst angelegt, Todoist hat aber nicht rechtzeitig geantwortet. "
                    "Bitte in Todoist prüfen, ob sie existiert, bevor sie erneut erstellt wird."
                )
            raise TaskValidationError("Diese Aufgabe wird gerade bereits angelegt.")

        # Vor dem ersten await eintragen: parallele identische Anfragen scheitern.
        record = _RecentCreate(created_at=self._clock(), task=None)
        self._recent_creates[key] = record
        try:
            task = await self._provider.create_task(new_task)
        except TaskTimeoutError:
            record.uncertain = True
            raise
        except BaseException:
            self._recent_creates.pop(key, None)
            raise
        record.task = task
        logger.info("Aufgabe angelegt (id=%s).", task.task_id)
        return CreateResult(task=task)

    # --- Ändern ---------------------------------------------------------------

    async def update_task(
        self,
        chat_id: int,
        *,
        number: int | None = None,
        query: str | None = None,
        content: str | None = None,
        description: str | None = None,
        due_date: date | None = None,
        due_time: dtime | None = None,
        recurrence: str | None = None,
        remove_due: bool = False,
        remove_recurrence: bool = False,
        duration_minutes: int | None = None,
        remove_duration: bool = False,
        priority: int | None = None,
        labels: Sequence[str] | None = None,
    ) -> Task:
        task = await self._resolve(chat_id, number, query)
        due = self._changed_due(task, due_date, due_time, recurrence, remove_due, remove_recurrence)

        if duration_minutes is not None:
            _validate_duration(duration_minutes)
            if remove_duration:
                raise TaskValidationError("Dauer setzen und entfernen gleichzeitig ist nicht möglich.")
            if due is not None:
                has_time = due.at is not None or due.recurrence is not None
            else:
                has_time = task.due is not None and task.due.time is not None
            if not has_time:
                raise TaskValidationError("Eine Dauer ist nur bei Aufgaben mit Uhrzeit möglich.")

        changes = TaskChanges(
            content=_clean_content(content) if content is not None else None,
            description=_clean_description(description) if description is not None else None,
            priority=_validate_priority(priority),
            due=due,
            duration_minutes=duration_minutes,
            clear_duration=remove_duration or bool(due and due.clear and task.duration_minutes),
            labels=_clean_labels(labels) if labels is not None else None,
        )
        if changes.is_empty:
            raise TaskValidationError("Es wurde keine Änderung angegeben.")
        updated = await self._provider.update_task(task.task_id, changes)
        logger.info("Aufgabe geändert (id=%s).", task.task_id)
        return updated

    # --- Erledigen ------------------------------------------------------------

    async def complete_task(self, chat_id: int, *, number: int | None = None, query: str | None = None) -> CompleteResult:
        task = await self._resolve(chat_id, number, query)
        await self._provider.complete_task(task.task_id)
        logger.info("Aufgabe erledigt (id=%s).", task.task_id)
        next_due = None
        if task.due is not None and task.due.is_recurring:
            try:
                next_due = (await self._provider.get_task(task.task_id)).due
            except TaskError:
                next_due = None
        return CompleteResult(task=task, next_due=next_due)

    async def list_completed_tasks(
        self,
        *,
        since: datetime,
        until: datetime,
        project: str | None = None,
        limit: int = 50,
    ) -> CompletedListing:
        """Erledigte Aufgaben mit Erledigungszeitpunkt in [since, until). Vergibt keine Aktionsnummern."""
        since, until = self._aware(since), self._aware(until)
        if until <= since:
            raise TaskValidationError("Das Ende des Zeitraums muss nach dem Anfang liegen.")
        if until - since > MAX_COMPLETED_RANGE:
            raise TaskValidationError("Der Zeitraum darf höchstens drei Monate umfassen.")
        projects = await self._provider.list_projects()
        project_id = _pick_by_name(projects, project, "Projekt", "Projekte").project_id if project else None
        tasks = await self._provider.list_completed_tasks(since, until, project_id)
        tasks = sorted(tasks, key=lambda t: t.completed_at or since)
        shown = tasks[: max(1, min(MAX_COMPLETED_RESULTS, limit))]
        return CompletedListing(
            tasks=shown,
            total=len(tasks),
            project_names={p.project_id: p.name for p in projects},
        )

    # --- Verschieben ----------------------------------------------------------

    async def move_task(
        self,
        chat_id: int,
        *,
        number: int | None = None,
        query: str | None = None,
        project: str | None = None,
        section: str | None = None,
    ) -> MoveResult:
        """Verschiebt eine Aufgabe in ein anderes Projekt und/oder einen Abschnitt (keine Terminverschiebung)."""
        if not (project and project.strip()) and not (section and section.strip()):
            raise TaskValidationError("Bitte ein Zielprojekt und/oder einen Zielabschnitt angeben.")
        task = await self._resolve(chat_id, number, query)
        projects = await self._provider.list_projects()
        names = {p.project_id: p.name for p in projects}
        target_project = _pick_by_name(projects, project, "Projekt", "Projekte") if project else None

        if section and section.strip():
            sections = await self._provider.list_sections(target_project.project_id if target_project else None)
            chosen = _pick_by_name(
                sections,
                section,
                "Abschnitt",
                "Abschnitte",
                display=lambda s: f"{s.name} (Projekt {names.get(s.project_id, '?')})",
            )
            if task.section_id == chosen.section_id:
                raise TaskValidationError(f"Die Aufgabe liegt bereits im Abschnitt „{chosen.name}“.")
            moved = await self._provider.move_task(task.task_id, section_id=chosen.section_id)
            logger.info("Aufgabe in Abschnitt verschoben (id=%s).", task.task_id)
            return MoveResult(task=moved, project_name=names.get(chosen.project_id, "?"), section_name=chosen.name)

        assert target_project is not None
        if task.project_id == target_project.project_id and task.section_id is None:
            raise TaskValidationError(f"Die Aufgabe liegt bereits im Projekt „{target_project.name}“.")
        moved = await self._provider.move_task(task.task_id, project_id=target_project.project_id)
        logger.info("Aufgabe in Projekt verschoben (id=%s).", task.task_id)
        return MoveResult(task=moved, project_name=target_project.name)

    # --- Projekte -------------------------------------------------------------

    async def create_project(self, name: str, parent: str | None = None) -> Project:
        name = _clean_name(name, "Projektname")
        projects = await self._provider.list_projects()
        parent_id = _pick_by_name(projects, parent, "Projekt", "Projekte").project_id if parent else None
        if any(_norm(p.name) == _norm(name) and p.parent_id == parent_id for p in projects):
            raise TaskValidationError(f"Ein Projekt „{name}“ gibt es an dieser Stelle schon.")
        project = await self._provider.create_project(name, parent_id)
        logger.info("Projekt angelegt (id=%s).", project.project_id)
        return project

    async def rename_project(self, project: str, new_name: str) -> tuple[Project, Project]:
        """Benennt ein Projekt um. Liefert (vorher, nachher)."""
        new_name = _clean_name(new_name, "Projektname")
        projects = await self._provider.list_projects()
        current = _pick_by_name(projects, project, "Projekt", "Projekte")
        _reject_inbox(current, "umbenannt")
        if any(
            p.project_id != current.project_id and _norm(p.name) == _norm(new_name) and p.parent_id == current.parent_id
            for p in projects
        ):
            raise TaskValidationError(f"Ein Projekt „{new_name}“ gibt es an dieser Stelle schon.")
        renamed = await self._provider.rename_project(current.project_id, new_name)
        logger.info("Projekt umbenannt (id=%s).", current.project_id)
        return current, renamed

    async def archive_project(self, project: str) -> Project:
        current = _pick_by_name(await self._provider.list_projects(), project, "Projekt", "Projekte")
        _reject_inbox(current, "archiviert")
        await self._provider.archive_project(current.project_id)
        logger.info("Projekt archiviert (id=%s).", current.project_id)
        return current

    async def unarchive_project(self, project: str) -> Project:
        archived = await self._provider.list_archived_projects()
        current = _pick_by_name(archived, project, "Archiviertes Projekt", "archivierte Projekte")
        await self._provider.unarchive_project(current.project_id)
        logger.info("Projekt wiederhergestellt (id=%s).", current.project_id)
        return current

    # --- Abschnitte -----------------------------------------------------------

    async def list_sections(self, project: str) -> tuple[Project, list[Section]]:
        found = _pick_by_name(await self._provider.list_projects(), project, "Projekt", "Projekte")
        return found, await self._provider.list_sections(found.project_id)

    async def create_section(self, project: str, name: str) -> tuple[Project, Section]:
        name = _clean_name(name, "Abschnittsname")
        found = _pick_by_name(await self._provider.list_projects(), project, "Projekt", "Projekte")
        existing = await self._provider.list_sections(found.project_id)
        if any(_norm(s.name) == _norm(name) for s in existing):
            raise TaskValidationError(f"Im Projekt „{found.name}“ gibt es den Abschnitt „{name}“ schon.")
        section = await self._provider.create_section(found.project_id, name)
        logger.info("Abschnitt angelegt (id=%s).", section.section_id)
        return found, section

    # --- Labels ---------------------------------------------------------------

    async def list_labels(self) -> list[Label]:
        return await self._provider.list_labels()

    async def create_label(self, name: str) -> Label:
        name = _clean_label_name(name)
        if any(_norm(label.name) == _norm(name) for label in await self._provider.list_labels()):
            raise TaskValidationError(f"Das Label „{name}“ gibt es schon.")
        label = await self._provider.create_label(name)
        logger.info("Label angelegt (id=%s).", label.label_id)
        return label

    async def rename_label(self, label: str, new_name: str) -> tuple[Label, Label]:
        """Benennt ein Label um. Liefert (vorher, nachher)."""
        new_name = _clean_label_name(new_name)
        labels = await self._provider.list_labels()
        current = _pick_by_name(labels, _strip_at(label), "Label", "Labels")
        if any(item.label_id != current.label_id and _norm(item.name) == _norm(new_name) for item in labels):
            raise TaskValidationError(f"Das Label „{new_name}“ gibt es schon.")
        renamed = await self._provider.rename_label(current.label_id, new_name)
        logger.info("Label umbenannt (id=%s).", current.label_id)
        return current, renamed

    # --- Löschen (nur mit Bestätigung) ---------------------------------------

    async def request_deletion(self, chat_id: int, *, number: int | None = None, query: str | None = None) -> PendingDeletion:
        """Bereitet das Löschen einer Aufgabe vor. Gelöscht wird erst nach ``confirm_deletion`` (Nutzer-Klick)."""
        task = await self._resolve(chat_id, number, query)
        return self._store_deletion(
            chat_id, kind=DeletionKind.TASK, target_id=task.task_id, name=task.content, task=task
        )

    async def request_project_deletion(self, chat_id: int, project: str) -> PendingDeletion:
        """Bereitet das Löschen eines Projekts samt Unterprojekten und Aufgaben vor (nur mit Bestätigung)."""
        projects = await self._provider.list_projects()
        current = _pick_by_name(projects, project, "Projekt", "Projekte")
        _reject_inbox(current, "gelöscht")
        tree = {current.project_id}
        grew = True
        while grew:  # Unterprojekte (beliebig tief) einsammeln
            grew = False
            for p in projects:
                if p.parent_id in tree and p.project_id not in tree:
                    tree.add(p.project_id)
                    grew = True
        tasks = await self._provider.list_tasks()
        open_count = sum(1 for t in tasks if t.project_id in tree)
        return self._store_deletion(
            chat_id,
            kind=DeletionKind.PROJECT,
            target_id=current.project_id,
            name=current.name,
            open_task_count=open_count,
            subproject_count=len(tree) - 1,
        )

    async def request_label_deletion(self, chat_id: int, label: str) -> PendingDeletion:
        """Bereitet das Löschen eines Labels vor (es wird von allen Aufgaben entfernt; nur mit Bestätigung)."""
        current = _pick_by_name(await self._provider.list_labels(), _strip_at(label), "Label", "Labels")
        tasks = await self._provider.list_tasks()
        used = sum(1 for t in tasks if any(_norm(name) == _norm(current.name) for name in t.labels))
        return self._store_deletion(
            chat_id,
            kind=DeletionKind.LABEL,
            target_id=current.label_id,
            name=current.name,
            open_task_count=used,
        )

    def _store_deletion(self, chat_id: int, **fields) -> PendingDeletion:
        current = self._deletions.get(chat_id)
        if current is not None and current.deletion.status is DeletionStatus.DELETING:
            raise TaskValidationError("Es wird gerade etwas gelöscht. Bitte kurz warten.")
        deletion = PendingDeletion(
            deletion_id=secrets.token_hex(6),
            status=DeletionStatus.PENDING,
            expires_at=self._clock() + DELETION_TTL_SECONDS,
            **fields,
        )
        self._deletions[chat_id] = _DeletionRecord(deletion=deletion, needs_review=True)
        return deletion

    def take_deletion_for_review(self, chat_id: int) -> PendingDeletion | None:
        """Liefert eine Löschanfrage, die dem Nutzer jetzt zur Bestätigung angezeigt werden muss."""
        record = self._deletions.get(chat_id)
        if record is None or not record.needs_review or record.deletion.status is not DeletionStatus.PENDING:
            return None
        record.needs_review = False
        return record.deletion

    async def confirm_deletion(self, chat_id: int, deletion_id: str) -> PendingDeletion:
        """Löscht – nur nach ausdrücklicher Nutzeraktion (Telegram-Button), niemals durch das LLM."""
        record = self._deletions.get(chat_id)
        if record is None or record.deletion.deletion_id != deletion_id:
            raise DeletionNotFoundError("Diese Löschanfrage ist nicht mehr gültig.")
        deletion = record.deletion
        this = _THIS[deletion.kind]
        if deletion.status is DeletionStatus.DONE:
            raise DeletionAlreadyDoneError(f"{this} wurde bereits gelöscht.")
        if deletion.status is DeletionStatus.DELETING:
            raise DeletionAlreadyDoneError(f"{this} wird gerade gelöscht.")
        if self._clock() > deletion.expires_at:
            del self._deletions[chat_id]
            raise DeletionExpiredError("Die Löschanfrage ist abgelaufen. Bitte erneut anfordern.")

        # Status vor dem ersten await setzen: Doppelklicks löschen nicht zweimal.
        record.deletion = replace(deletion, status=DeletionStatus.DELETING)
        try:
            if deletion.kind is DeletionKind.TASK:
                await self._provider.delete_task(deletion.target_id)
            elif deletion.kind is DeletionKind.PROJECT:
                await self._provider.delete_project(deletion.target_id)
            else:
                await self._provider.delete_label(deletion.target_id)
        except BaseException:
            self._deletions.pop(chat_id, None)
            raise
        record.deletion = replace(deletion, status=DeletionStatus.DONE)
        logger.info("Gelöscht: %s (id=%s).", deletion.kind.value, deletion.target_id)
        return deletion

    def cancel_deletion(self, chat_id: int, deletion_id: str) -> PendingDeletion | None:
        """Verwirft eine offene Löschanfrage. Liefert sie zurück, oder ``None`` wenn keine offene existiert."""
        record = self._deletions.get(chat_id)
        if record is None or record.deletion.deletion_id != deletion_id:
            return None
        if record.deletion.status is not DeletionStatus.PENDING:
            return None
        del self._deletions[chat_id]
        return record.deletion

    # --- Intern ---------------------------------------------------------------

    def _due_input(self, due_date: date | None, due_time: dtime | None, recurrence: str | None) -> DueInput | None:
        if recurrence is not None and recurrence.strip():
            if due_date is not None or due_time is not None:
                raise TaskValidationError(
                    "Bei wiederkehrenden Aufgaben Datum/Uhrzeit bitte in 'recurrence' angeben "
                    "(z. B. 'jeden Sonntag um 10 Uhr')."
                )
            return DueInput(recurrence=_clean_recurrence(recurrence))
        if due_time is not None:
            if due_date is None:
                raise TaskValidationError("Zur Uhrzeit fehlt das Datum.")
            return DueInput(at=self._at(due_date, due_time))
        if due_date is not None:
            self._reject_past(due_date)
            return DueInput(on=due_date)
        return None

    def _changed_due(
        self,
        task: Task,
        due_date: date | None,
        due_time: dtime | None,
        recurrence: str | None,
        remove_due: bool,
        remove_recurrence: bool,
    ) -> DueInput | None:
        recurring = task.due is not None and task.due.is_recurring
        if remove_due:
            if due_date or due_time or recurrence:
                raise TaskValidationError("Fälligkeit entfernen und gleichzeitig neu setzen ist nicht möglich.")
            return DueInput(clear=True)
        if recurrence is not None and recurrence.strip():
            return self._due_input(due_date, due_time, recurrence)
        if due_date is None and due_time is None:
            if remove_recurrence and recurring:
                # Wiederholung beenden, nächsten Termin als festes Datum behalten.
                if task.due.time is not None:
                    return DueInput(at=task.due.time)
                return DueInput(on=task.due.date)
            return None
        if recurring and not remove_recurrence:
            raise TaskValidationError(
                f"„{task.content}“ wiederholt sich ({task.due.text or 'wiederkehrend'}). Ein festes Datum würde "
                "die Wiederholung beenden. Bitte nachfragen: Wiederholung beenden (remove_recurrence=true) "
                "oder neuen Rhythmus angeben (recurrence)?"
            )
        target_date = due_date or (task.due.date if task.due else None)
        if target_date is None:
            raise TaskValidationError("Die Aufgabe hat kein Datum. Bitte ein Datum angeben.")
        # Ohne neue Uhrzeit bleibt eine vorhandene Uhrzeit erhalten (wie beim Verschieben in Todoist).
        target_time = due_time or (task.due.time.timetz().replace(tzinfo=None) if task.due and task.due.time else None)
        if target_time is not None:
            return DueInput(at=self._at(target_date, target_time))
        self._reject_past(target_date)
        return DueInput(on=target_date)

    def _at(self, day: date, at: dtime) -> datetime:
        local = datetime.combine(day, at.replace(tzinfo=None, second=0, microsecond=0)).replace(tzinfo=self._timezone)
        # Nicht existierende Uhrzeiten (Zeitumstellung) per Hin-und-Rück-Umrechnung erkennen.
        roundtrip = local.astimezone(UTC).astimezone(self._timezone)
        if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
            raise TaskValidationError(
                f"{at.strftime('%H:%M')} Uhr existiert am {day.strftime('%d.%m.%Y')} wegen der Zeitumstellung nicht."
            )
        if local < self.now() - timedelta(minutes=1):
            raise TaskValidationError(
                f"{local.strftime('%d.%m.%Y %H:%M')} liegt in der Vergangenheit. Bitte Datum/Uhrzeit prüfen."
            )
        return local

    def _reject_past(self, day: date) -> None:
        if day < self.now().date():
            raise TaskValidationError(f"Der {day.strftime('%d.%m.%Y')} liegt in der Vergangenheit. Bitte Datum prüfen.")

    def _aware(self, value: datetime) -> datetime:
        return value.replace(tzinfo=self._timezone) if value.tzinfo is None else value.astimezone(self._timezone)

    async def _find_project(self, name: str) -> Project:
        return _pick_by_name(await self._provider.list_projects(), name, "Projekt", "Projekte")

    async def _resolve(self, chat_id: int, number: int | None, query: str | None) -> Task:
        if number is not None:
            listing = self._listings.get(chat_id)
            if listing is None or self._clock() > listing.expires_at:
                raise TaskValidationError("Bitte zuerst Aufgaben auflisten oder die Aufgabe beim Namen nennen.")
            if not 1 <= number <= len(listing.task_ids):
                raise TaskValidationError(
                    f"Nummer {number} gibt es in der letzten Übersicht nicht (1 bis {len(listing.task_ids)})."
                )
            return await self._provider.get_task(listing.task_ids[number - 1])

        query = _clean_query(query)
        if not query:
            raise TaskValidationError("Bitte Nummer oder Namen der Aufgabe angeben.")
        tasks = await self._provider.list_tasks()
        matches = sorted((t for t in tasks if _matches(t, query, content_only=True)), key=_sort_key)
        if len(matches) > 1:
            wanted = " ".join(query.casefold().split())
            exact = [t for t in matches if " ".join(t.content.casefold().split()) == wanted]
            if len(exact) == 1:
                return exact[0]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise TaskNotFoundError(f"Keine offene Aufgabe passt zu „{query}“.")
        candidates = matches[:MAX_CANDIDATES]
        self._remember_listing(chat_id, candidates)
        raise TaskAmbiguousError(f"{len(matches)} offene Aufgaben passen zu „{query}“.", candidates)

    def _remember_listing(self, chat_id: int, tasks: list[Task]) -> None:
        self._listings[chat_id] = _Listing(
            task_ids=[t.task_id for t in tasks],
            expires_at=self._clock() + LISTING_TTL_SECONDS,
        )

    def _prune_recent_creates(self) -> None:
        cutoff = self._clock() - CREATE_DEDUPE_SECONDS
        for key in [k for k, v in self._recent_creates.items() if v.created_at < cutoff]:
            del self._recent_creates[key]


# --- Hilfsfunktionen ------------------------------------------------------------


def _in_scope(task: Task, scope: str, on: date | None, now: datetime) -> bool:
    due = task.due
    today = now.date()
    if scope == "all":
        return True
    if scope == "no_date":
        return due is None
    if due is None:
        return False
    if scope == "today":
        return due.date == today
    if scope == "tomorrow":
        return due.date == today + timedelta(days=1)
    if scope == "overdue":
        return due.date < today or (due.time is not None and due.time < now)
    if scope == "week":
        return today <= due.date <= today + timedelta(days=6)
    return due.date == on  # scope == "date"


def _sort_key(task: Task) -> tuple:
    due = task.due
    if due is None:
        return (1, date.max, 24 * 60, task.priority, task.content.casefold())
    minutes = due.time.hour * 60 + due.time.minute if due.time is not None else 24 * 60
    return (0, due.date, minutes, task.priority, task.content.casefold())


def _matches(task: Task, query: str, *, content_only: bool = False) -> bool:
    haystack = task.content.casefold()
    if not content_only:
        haystack += " " + task.description.casefold()
    return all(token in haystack for token in query.casefold().split())


def _clean_query(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    if len(value) > MAX_QUERY_CHARS:
        raise TaskValidationError("Der Suchbegriff ist zu lang.")
    return value or None


def _clean_content(value: str) -> str:
    value = " ".join((value or "").split())
    if not value:
        raise TaskValidationError("Der Aufgabentitel ist leer.")
    if len(value) > MAX_CONTENT_CHARS:
        raise TaskValidationError(f"Der Aufgabentitel ist zu lang (höchstens {MAX_CONTENT_CHARS} Zeichen).")
    return value


def _clean_description(value: str | None) -> str:
    value = (value or "").strip()
    if len(value) > MAX_DESCRIPTION_CHARS:
        raise TaskValidationError(f"Die Beschreibung ist zu lang (höchstens {MAX_DESCRIPTION_CHARS} Zeichen).")
    return value


def _clean_recurrence(value: str) -> str:
    value = " ".join(value.split())
    if len(value) > MAX_RECURRENCE_CHARS or not value.isprintable():
        raise TaskValidationError("Die Wiederholungsangabe ist ungültig oder zu lang.")
    return value


def _clean_labels(values: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values or ():
        label = " ".join(raw.split()).lstrip("@")
        if not label:
            continue
        if len(label) > MAX_LABEL_CHARS or not label.isprintable():
            raise TaskValidationError(f"Ungültiges Label: {label[:MAX_LABEL_CHARS]!r}.")
        if label.casefold() not in (existing.casefold() for existing in result):
            result.append(label)
    if len(result) > MAX_LABELS:
        raise TaskValidationError(f"Höchstens {MAX_LABELS} Labels sind erlaubt.")
    return tuple(result)


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _strip_at(value: str) -> str:
    return value.strip().lstrip("@")


def _clean_name(value: str | None, what: str) -> str:
    value = " ".join((value or "").split())
    if not value:
        raise TaskValidationError(f"Der {what} ist leer.")
    if len(value) > MAX_NAME_CHARS or not value.isprintable():
        raise TaskValidationError(f"Der {what} ist ungültig oder zu lang (höchstens {MAX_NAME_CHARS} Zeichen).")
    return value


def _clean_label_name(value: str | None) -> str:
    name = _clean_name(_strip_at(value or ""), "Labelname")
    if len(name) > MAX_LABEL_CHARS:
        raise TaskValidationError(f"Der Labelname ist zu lang (höchstens {MAX_LABEL_CHARS} Zeichen).")
    return name


def _reject_inbox(project: Project, verb: str) -> None:
    if project.is_inbox:
        raise TaskValidationError(f"Der Eingang kann nicht {verb} werden.")


def _pick_by_name(
    items: Sequence[_T],
    name: str | None,
    singular: str,
    plural: str,
    *,
    display: Callable[[_T], str] | None = None,
) -> _T:
    """Findet genau einen Eintrag per Name (exakt, sonst Teiltreffer); bei Mehrdeutigkeit wird nachgefragt."""
    wanted = _norm(name or "")
    if not wanted:
        raise TaskValidationError(f"Die Angabe zum Eintrag ({singular}) ist leer.")
    show = display or (lambda item: item.name)  # type: ignore[attr-defined]
    exact = [i for i in items if _norm(i.name) == wanted]  # type: ignore[attr-defined]
    found = exact or [i for i in items if wanted in i.name.casefold()]  # type: ignore[attr-defined]
    if len(found) == 1:
        return found[0]
    names = ", ".join(show(i) for i in (found or items)[:MAX_CANDIDATES])
    if found:
        raise TaskValidationError(f"Mehrere {plural} passen zu „{name}“: {names}. Bitte genauer angeben.")
    raise TaskNotFoundError(f"{singular} „{name}“ nicht gefunden. Vorhanden: {names or 'keine'}.")


_THIS = {
    DeletionKind.TASK: "Diese Aufgabe",
    DeletionKind.PROJECT: "Dieses Projekt",
    DeletionKind.LABEL: "Dieses Label",
}


def _validate_priority(value: int | None) -> int | None:
    if value is not None and not 1 <= value <= 4:
        raise TaskValidationError("Die Priorität muss zwischen 1 (höchste) und 4 (normal) liegen.")
    return value


def _validate_duration(minutes: int) -> None:
    if not 1 <= minutes <= MAX_DURATION_MINUTES:
        raise TaskValidationError(f"Die Dauer muss zwischen 1 und {MAX_DURATION_MINUTES} Minuten liegen.")
