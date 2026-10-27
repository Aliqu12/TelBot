"""Zugriffskontrolle: Nur der konfigurierte Besitzer darf den Assistenten nutzen."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessPolicy:
    allowed_user_id: int | None

    @property
    def is_configured(self) -> bool:
        """False = Einrichtungsmodus: nur /whoami ist nutzbar, keine KI-Anfragen."""
        return self.allowed_user_id is not None

    def is_authorized(self, user_id: int | None) -> bool:
        return user_id is not None and self.is_configured and user_id == self.allowed_user_id

    def may_use_whoami(self, user_id: int | None) -> bool:
        """Im Einrichtungsmodus darf jeder seine ID abfragen, danach nur noch der Besitzer."""
        return user_id is not None and (not self.is_configured or self.is_authorized(user_id))
