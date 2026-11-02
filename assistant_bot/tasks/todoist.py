"""TodoistTaskProvider: Zugriff auf Todoist über die offizielle REST-API v1 (httpx).

Dokumentation: https://developer.todoist.com/api/v1/

- Authentifizierung per persönlichem API-Token (``Authorization: Bearer ...``).
- Fälligkeiten mit Uhrzeit werden als exakter UTC-Zeitpunkt gesendet
  (``due_datetime``); so stimmt die Uhrzeit unabhängig von der in Todoist
  eingestellten Zeitzone. Wiederkehrende Termine gehen als deutscher
  ``due_string`` (z. B. "jeden Sonntag") an den Todoist-Parser.
- Lesende Anfragen werden bei Netzwerk-/Serverfehlern einmal wiederholt.
  Schreibende Anfragen nur, wenn sicher ist, dass Todoist sie nicht verarbeitet
  hat (Verbindungsaufbau gescheitert, HTTP 429) – sonst drohen Duplikate.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .models import (
    DueInput,
    Label,
    NewTask,
    Project,
    Section,
    Task,
    TaskAuthenticationError,
    TaskChanges,
    TaskConnectionError,
    TaskDue,
    TaskError,
    TaskNotFoundError,
    TaskRateLimitError,
    TaskTimeoutError,
    TaskValidationError,
)

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.todoist.com/api/v1"
TASK_URL_TEMPLATE = "https://app.todoist.com/app/task/{task_id}"
PAGE_LIMIT = 200  # Maximum laut API
MAX_PAGES = 10  # Schutz vor Endlosschleifen: höchstens 2000 Aufgaben/Projekte
MAX_RETRY_AFTER_SECONDS = 5  # länger wird im Chat nicht gewartet
DUE_LANG = "de"

# Todoist-IDs sind opake alphanumerische Strings. Strikte Prüfung verhindert Pfadmanipulation.
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TodoistTaskProvider:
    name = "todoist"

    def __init__(
        self,
        api_token: str,
        *,
        timezone: tzinfo,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._timezone = timezone
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"Authorization": f"Bearer {api_token}", "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
        )

    # --- Aufgaben -------------------------------------------------------------

    async def list_tasks(self, project_id: str | None = None) -> list[Task]:
        params: dict[str, Any] = {}
        if project_id is not None:
            params["project_id"] = _safe_id(project_id)
        return [self._parse_task(item) for item in await self._get_all("/tasks", params)]

    async def get_task(self, task_id: str) -> Task:
        return self._parse_task(await self._request("GET", f"/tasks/{_safe_id(task_id)}", retry_safe=True))

    async def create_task(self, task: NewTask) -> Task:
        body: dict[str, Any] = {"content": task.content}
        if task.description:
            body["description"] = task.description
        if task.project_id is not None:
            body["project_id"] = _safe_id(task.project_id)
        if task.priority is not None:
            body["priority"] = _to_api_priority(task.priority)
        if task.labels:
            body["labels"] = list(task.labels)
        body.update(_due_fields(task.due))
        if task.duration_minutes is not None:
            body.update(duration=task.duration_minutes, duration_unit="minute")
        return self._parse_task(await self._request("POST", "/tasks", json=body, retry_safe=False))

    async def update_task(self, task_id: str, changes: TaskChanges) -> Task:
        body: dict[str, Any] = {}
        if changes.content is not None:
            body["content"] = changes.content
        if changes.description is not None:
            body["description"] = changes.description
        if changes.priority is not None:
            body["priority"] = _to_api_priority(changes.priority)
        if changes.labels is not None:
            body["labels"] = list(changes.labels)
        body.update(_due_fields(changes.due))
        if changes.clear_duration:
            body.update(duration=None, duration_unit=None)
        elif changes.duration_minutes is not None:
            body.update(duration=changes.duration_minutes, duration_unit="minute")
        data = await self._request("POST", f"/tasks/{_safe_id(task_id)}", json=body, retry_safe=False)
        return self._parse_task(data)

    async def complete_task(self, task_id: str) -> None:
        # Nicht wiederholen: Bei wiederkehrenden Aufgaben würde ein doppeltes "close"
        # die Aufgabe um zwei Termine weiterschieben.
        await self._request("POST", f"/tasks/{_safe_id(task_id)}/close", retry_safe=False)

    async def delete_task(self, task_id: str) -> None:
        await self._request("DELETE", f"/tasks/{_safe_id(task_id)}", retry_safe=False)

    async def move_task(self, task_id: str, *, project_id: str | None = None, section_id: str | None = None) -> Task:
        if (project_id is None) == (section_id is None):
            raise TaskValidationError("Zum Verschieben genau ein Ziel angeben: Projekt oder Abschnitt.")
        if project_id is not None:
            body = {"project_id": _safe_id(project_id)}
        else:
            body = {"section_id": _safe_id(section_id or "")}
        data = await self._request("POST", f"/tasks/{_safe_id(task_id)}/move", json=body, retry_safe=False)
        return self._parse_task(data)

    async def list_completed_tasks(self, since: datetime, until: datetime, project_id: str | None = None) -> list[Task]:
        params: dict[str, Any] = {"since": _utc_text(since), "until": _utc_text(until)}
        if project_id is not None:
            params["project_id"] = _safe_id(project_id)
        items = await self._get_all("/tasks/completed/by_completion_date", params, key="items")
        return [self._parse_task(item) for item in items]

    # --- Projekte -------------------------------------------------------------

    async def list_projects(self) -> list[Project]:
        return [
            _parse_project(item)
            for item in await self._get_all("/projects", {})
            if not item.get("is_archived") and not item.get("is_deleted")
        ]

    async def list_archived_projects(self) -> list[Project]:
        return [
            _parse_project(item, archived=True)
            for item in await self._get_all("/projects/archived", {})
            if not item.get("is_deleted")
        ]

    async def create_project(self, name: str, parent_id: str | None = None) -> Project:
        body: dict[str, Any] = {"name": name}
        if parent_id is not None:
            body["parent_id"] = _safe_id(parent_id)
        return _parse_project(await self._request("POST", "/projects", json=body, retry_safe=False))

    async def rename_project(self, project_id: str, name: str) -> Project:
        data = await self._request("POST", f"/projects/{_safe_id(project_id)}", json={"name": name}, retry_safe=False)
        return _parse_project(data)

    async def archive_project(self, project_id: str) -> None:
        await self._request("POST", f"/projects/{_safe_id(project_id)}/archive", retry_safe=False)

    async def unarchive_project(self, project_id: str) -> None:
        await self._request("POST", f"/projects/{_safe_id(project_id)}/unarchive", retry_safe=False)

    async def delete_project(self, project_id: str) -> None:
        await self._request("DELETE", f"/projects/{_safe_id(project_id)}", retry_safe=False)

    # --- Abschnitte -----------------------------------------------------------

    async def list_sections(self, project_id: str | None = None) -> list[Section]:
        params: dict[str, Any] = {}
        if project_id is not None:
            params["project_id"] = _safe_id(project_id)
        return [
            _parse_section(item)
            for item in await self._get_all("/sections", params)
            if not item.get("is_archived") and not item.get("is_deleted")
        ]

    async def create_section(self, project_id: str, name: str) -> Section:
        body = {"name": name, "project_id": _safe_id(project_id)}
        return _parse_section(await self._request("POST", "/sections", json=body, retry_safe=False))

    # --- Labels ---------------------------------------------------------------

    async def list_labels(self) -> list[Label]:
        return [_parse_label(item) for item in await self._get_all("/labels", {})]

    async def create_label(self, name: str) -> Label:
        return _parse_label(await self._request("POST", "/labels", json={"name": name}, retry_safe=False))

    async def rename_label(self, label_id: str, name: str) -> Label:
        data = await self._request("POST", f"/labels/{_safe_id(label_id)}", json={"name": name}, retry_safe=False)
        return _parse_label(data)

    async def delete_label(self, label_id: str) -> None:
        await self._request("DELETE", f"/labels/{_safe_id(label_id)}", retry_safe=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- HTTP -----------------------------------------------------------------

    async def _get_all(self, path: str, params: dict[str, Any], key: str = "results") -> list[dict[str, Any]]:
        """Liest alle Seiten eines paginierten Endpunkts (Liste unter ``key`` + ``next_cursor``)."""
        results: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page_params = {**params, "limit": PAGE_LIMIT}
            if cursor:
                page_params["cursor"] = cursor
            data = await self._request("GET", path, params=page_params, retry_safe=True)
            if not isinstance(data, dict) or not isinstance(data.get(key), list):
                raise TaskConnectionError("Todoist hat unerwartet geantwortet.")
            for item in data[key]:
                # Laut API können Einträge bei gleichzeitigen Änderungen doppelt erscheinen.
                # Wiederkehrende Aufgaben tauchen als erledigt mehrfach mit gleicher ID auf (je Erledigung).
                if isinstance(item, dict) and "id" in item:
                    results[f"{item['id']}|{item.get('completed_at') or ''}"] = item
            cursor = data.get("next_cursor")
            if not cursor:
                break
        else:
            logger.warning("Todoist: Seitenlimit erreicht (%s), Ergebnis ist unvollständig.", path)
        return list(results.values())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_safe: bool,
    ) -> Any:
        for attempt in (1, 2):
            last_attempt = attempt == 2
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException as exc:
                if retry_safe and not last_attempt:
                    continue
                logger.warning("Todoist: Zeitüberschreitung (%s %s).", method, path)
                raise TaskTimeoutError(
                    "Todoist hat nicht rechtzeitig geantwortet."
                    + ("" if retry_safe else " Die Änderung wurde möglicherweise trotzdem übernommen.")
                ) from exc
            except httpx.TransportError as exc:
                # ConnectError: Verbindung kam nie zustande -> auch Schreibzugriffe sicher wiederholbar.
                if not last_attempt and (retry_safe or isinstance(exc, httpx.ConnectError)):
                    continue
                logger.warning("Todoist nicht erreichbar: %s", type(exc).__name__)
                raise TaskConnectionError("Todoist ist gerade nicht erreichbar.") from exc

            status = response.status_code
            if status == 429:
                retry_after = _retry_after(response)
                if not last_attempt and retry_after is not None and retry_after <= MAX_RETRY_AFTER_SECONDS:
                    await self._sleep(retry_after)
                    continue
                logger.warning("Todoist Rate Limit erreicht (retry_after=%s).", retry_after)
                wait = f" Bitte in etwa {retry_after} Sekunden erneut versuchen." if retry_after else ""
                raise TaskRateLimitError(f"Das Todoist-Anfragelimit ist erreicht.{wait}", retry_after)
            if status >= 500 and retry_safe and not last_attempt:
                continue
            return self._handle_response(method, path, response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _handle_response(self, method: str, path: str, response: httpx.Response) -> Any:
        status = response.status_code
        if status < 300:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise TaskConnectionError("Todoist hat unerwartet geantwortet.") from exc

        logger.warning("Todoist-Anfrage abgelehnt: %s %s -> HTTP %s", method, path, status)
        if status == 401:
            raise TaskAuthenticationError("Todoist hat den Zugriff abgelehnt. Bitte TODOIST_API_TOKEN prüfen.")
        if status == 403:
            raise TaskError("Todoist erlaubt diese Aktion nicht (fehlende Berechtigung oder Limit des Tarifs erreicht).")
        if status == 404:
            raise TaskNotFoundError("Der Eintrag wurde in Todoist nicht gefunden (evtl. erledigt oder gelöscht).")
        if status >= 500:
            raise TaskConnectionError("Todoist ist gerade nicht erreichbar (Serverfehler).")
        if status in (400, 422):
            detail = _error_detail(response)
            raise TaskValidationError("Todoist hat die Angaben abgelehnt" + (f": {detail}" if detail else "."))
        raise TaskError(f"Die Todoist-Anfrage ist fehlgeschlagen (HTTP {status}).")

    # --- Abbildung Todoist -> Modell -----------------------------------------

    def _parse_task(self, item: Any) -> Task:
        try:
            task_id = str(item["id"])
            duration = item.get("duration") or None
            return Task(
                task_id=task_id,
                content=str(item.get("content") or ""),
                description=str(item.get("description") or ""),
                project_id=str(item["project_id"]) if item.get("project_id") else None,
                priority=_from_api_priority(item.get("priority")),
                due=self._parse_due(item.get("due")),
                duration_minutes=_parse_duration(duration),
                labels=tuple(str(label) for label in item.get("labels") or ()),
                url=TASK_URL_TEMPLATE.format(task_id=task_id),
                created_at=_parse_timestamp(item.get("added_at")),
                section_id=str(item["section_id"]) if item.get("section_id") else None,
                completed_at=self._local(_parse_timestamp(item.get("completed_at"))),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise TaskConnectionError("Todoist hat unerwartete Aufgabendaten geliefert.") from exc

    def _local(self, value: datetime | None) -> datetime | None:
        return value.astimezone(self._timezone) if value is not None else None

    def _parse_due(self, due: Any) -> TaskDue | None:
        if not due or not due.get("date"):
            return None
        raw = str(due["date"])
        recurring = bool(due.get("is_recurring"))
        text = str(due.get("string") or "")
        if len(raw) == 10:  # YYYY-MM-DD: ganztägig
            return TaskDue(date=date.fromisoformat(raw), is_recurring=recurring, text=text)
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            # "Floating": Uhrzeit gilt in der Zeitzone des Nutzers.
            value = value.replace(tzinfo=_zone(due.get("timezone")) or self._timezone)
        local = value.astimezone(self._timezone)
        return TaskDue(date=local.date(), time=local, is_recurring=recurring, text=text)


def _due_fields(due: DueInput | None) -> dict[str, Any]:
    if due is None:
        return {}
    if due.clear:
        return {"due_string": "no date", "due_lang": "en"}
    if due.recurrence:
        return {"due_string": due.recurrence, "due_lang": DUE_LANG}
    if due.at is not None:
        return {"due_datetime": due.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if due.on is not None:
        return {"due_date": due.on.isoformat()}
    return {}


def _parse_project(item: Any, *, archived: bool = False) -> Project:
    try:
        return Project(
            project_id=str(item["id"]),
            name=str(item.get("name") or ""),
            is_inbox=bool(item.get("inbox_project")),
            is_archived=archived or bool(item.get("is_archived")),
            parent_id=str(item["parent_id"]) if item.get("parent_id") else None,
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise TaskConnectionError("Todoist hat unerwartete Projektdaten geliefert.") from exc


def _parse_section(item: Any) -> Section:
    try:
        return Section(section_id=str(item["id"]), project_id=str(item["project_id"]), name=str(item.get("name") or ""))
    except (KeyError, TypeError, AttributeError) as exc:
        raise TaskConnectionError("Todoist hat unerwartete Abschnittsdaten geliefert.") from exc


def _parse_label(item: Any) -> Label:
    try:
        return Label(label_id=str(item["id"]), name=str(item.get("name") or ""))
    except (KeyError, TypeError, AttributeError) as exc:
        raise TaskConnectionError("Todoist hat unerwartete Label-Daten geliefert.") from exc


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_api_priority(priority: int) -> int:
    # Todoist-API: 4 = sehr dringend (in der App "P1"), 1 = normal ("P4").
    return 5 - max(1, min(4, priority))


def _from_api_priority(value: Any) -> int:
    try:
        return 5 - max(1, min(4, int(value)))
    except (TypeError, ValueError):
        return 4


def _parse_timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_duration(duration: Any) -> int | None:
    if not isinstance(duration, dict):
        return None
    amount = int(duration.get("amount") or 0)
    if amount <= 0:
        return None
    return amount * 1440 if duration.get("unit") == "day" else amount


def _zone(name: Any) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _retry_after(response: httpx.Response) -> int | None:
    candidates: list[Any] = [response.headers.get("Retry-After")]
    try:
        payload = response.json()
        candidates.insert(0, (payload.get("error_extra") or {}).get("retry_after"))
    except (ValueError, AttributeError):
        pass
    for value in candidates:
        try:
            if value is not None:
                return max(0, int(float(value)))
        except (TypeError, ValueError):
            continue
    return None


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        detail = (payload.get("error_extra") or {}).get("explanation") or payload.get("error") or ""
    except (ValueError, AttributeError):
        return ""
    return "".join(ch for ch in str(detail) if ch.isprintable())[:200]


def _safe_id(value: str) -> str:
    if not _ID_PATTERN.fullmatch(value or ""):
        raise TaskValidationError("Ungültige Todoist-ID.")
    return value

