"""TaskService: providerunabhängige Aufgabenlogik.

- Filter nach Zeitraum (heute, morgen, überfällig, 7 Tage, Datum, ohne Datum) in
  der konfigurierten Zeitzone.
- Nummerierung der zuletzt gezeigten Aufgaben pro Chat ("erledige Nummer 2") und
  Suche nach Namen mit Mehrdeutigkeitsprüfung: Passen mehrere Aufgaben, wird
  nachgefragt statt geraten.
- Schutz vor Duplikaten beim Anlegen (gleiche Aufgabe kurz hintereinander).
- Schutz wiederkehrender Aufgaben: Ein festes Datum beendet die Wiederholung
  nur, wenn das ausdrücklich gewünscht ist.
- Löschen nur nach Bestätigung per Telegram-Button (``confirm_deletion``);
  das Sprachmodell kann Löschungen lediglich vorbereiten.

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

from .models import (
    DueInput,
    NewTask,
    Project,
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
LISTING_TTL_SECONDS = 3600
DELETION_TTL_SECONDS = 300
CREATE_DEDUPE_SECONDS = 120


class DeletionStatus(Enum):
    PENDING = "pending"
    DELETING = "deleting"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    deletion_id: str
    task: Task
    status: DeletionStatus
    expires_at: float  # Zeitpunkt der Uhr des Services (monoton)


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

    async def list_projects(self) -> list[Project]:
        return await self._provider.list_projects()

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

    # --- Löschen (nur mit Bestätigung) ---------------------------------------

    async def request_deletion(self, chat_id: int, *, number: int | None = None, query: str | None = None) -> PendingDeletion:
        """Bereitet das Löschen vor. Gelöscht wird erst nach ``confirm_deletion`` (Nutzer-Klick)."""
        task = await self._resolve(chat_id, number, query)
        current = self._deletions.get(chat_id)
        if current is not None and current.deletion.status is DeletionStatus.DELETING:
            raise TaskValidationError("Es wird gerade eine andere Aufgabe gelöscht. Bitte kurz warten.")
        deletion = PendingDeletion(
            deletion_id=secrets.token_hex(6),
            task=task,
            status=DeletionStatus.PENDING,
            expires_at=self._clock() + DELETION_TTL_SECONDS,
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

    async def confirm_deletion(self, chat_id: int, deletion_id: str) -> Task:
        """Löscht – nur nach ausdrücklicher Nutzeraktion (Telegram-Button), niemals durch das LLM."""
        record = self._deletions.get(chat_id)
        if record is None or record.deletion.deletion_id != deletion_id:
            raise DeletionNotFoundError("Diese Löschanfrage ist nicht mehr gültig.")
        deletion = record.deletion
        if deletion.status is DeletionStatus.DONE:
            raise DeletionAlreadyDoneError("Diese Aufgabe wurde bereits gelöscht.")
        if deletion.status is DeletionStatus.DELETING:
            raise DeletionAlreadyDoneError("Diese Aufgabe wird gerade gelöscht.")
        if self._clock() > deletion.expires_at:
            del self._deletions[chat_id]
            raise DeletionExpiredError("Die Löschanfrage ist abgelaufen. Bitte erneut anfordern.")

        # Status vor dem ersten await setzen: Doppelklicks löschen nicht zweimal.
        record.deletion = replace(deletion, status=DeletionStatus.DELETING)
        try:
            await self._provider.delete_task(deletion.task.task_id)
        except BaseException:
            self._deletions.pop(chat_id, None)
            raise
        record.deletion = replace(deletion, status=DeletionStatus.DONE)
        logger.info("Aufgabe gelöscht (id=%s).", deletion.task.task_id)
        return deletion.task

    def cancel_deletion(self, chat_id: int, deletion_id: str) -> bool:
        record = self._deletions.get(chat_id)
        if record is None or record.deletion.deletion_id != deletion_id:
            return False
        if record.deletion.status is not DeletionStatus.PENDING:
            return False
        del self._deletions[chat_id]
        return True

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

    async def _find_project(self, name: str) -> Project:
        wanted = " ".join(name.casefold().split())
        if not wanted:
            raise TaskValidationError("Der Projektname ist leer.")
        projects = await self._provider.list_projects()
        exact = [p for p in projects if " ".join(p.name.casefold().split()) == wanted]
        partial = exact or [p for p in projects if wanted in p.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(p.name for p in (partial or projects)[:MAX_CANDIDATES])
        if partial:
            raise TaskValidationError(f"Mehrere Projekte passen zu „{name}“: {names}. Bitte genauer angeben.")
        raise TaskNotFoundError(f"Projekt „{name}“ nicht gefunden. Vorhanden: {names or 'keine'}.")

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


def _validate_priority(value: int | None) -> int | None:
    if value is not None and not 1 <= value <= 4:
        raise TaskValidationError("Die Priorität muss zwischen 1 (höchste) und 4 (normal) liegen.")
    return value


def _validate_duration(minutes: int) -> None:
    if not 1 <= minutes <= MAX_DURATION_MINUTES:
        raise TaskValidationError(f"Die Dauer muss zwischen 1 und {MAX_DURATION_MINUTES} Minuten liegen.")
