"""Tests für die Persönlichkeitsdatei (PERSONALITY_FILE) und den daraus gebauten Systemprompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant_bot.config import (
    DEFAULT_PERSONALITY_FILE,
    MAX_PERSONALITY_CHARS,
    ConfigError,
    load_personality,
    load_settings,
)
from assistant_bot.prompts import SHARED_PROMPT_RULES, build_system_prompt

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_default_personality_file_is_personality_md(valid_env: dict[str, str]) -> None:
    assert load_settings(valid_env).personality_file == Path(DEFAULT_PERSONALITY_FILE) == Path("personality.md")


def test_personality_file_can_be_configured(valid_env: dict[str, str]) -> None:
    settings = load_settings({**valid_env, "PERSONALITY_FILE": "/config/arabisch.md"})

    assert settings.personality_file == Path("/config/arabisch.md")


def test_default_personality_is_neutral() -> None:
    personality = load_personality(PROJECT_ROOT / DEFAULT_PERSONALITY_FILE)

    assert "{owner_name}" in personality and "BEGRÜSSUNG UND ABSCHIED" in personality
    assert not any(word in personality for word in ("Firzana", "Assalam", "Hafiz", "Urdu", "Islamabad"))


def test_shipped_firzana_personality_has_greeting_and_farewell() -> None:
    personality = load_personality(PROJECT_ROOT / "custom_personalities" / "firzana.md")

    assert "Firzana" in personality and "{owner_name}" in personality
    assert "Assalam Alaykum" in personality and "Khuda Hafiz" in personality and "Allah Hafiz" in personality
    assert "NOT an AI" in personality and "Aap" in personality
    assert "Every message you write is in Urdu" in personality


def test_shipped_firzana_personality_has_no_contradicting_language_rules() -> None:
    personality = load_personality(PROJECT_ROOT / "custom_personalities" / "firzana.md")

    # Eine einzige Sprachregel: Urdu-Schrift, Wechsel nur auf ausdrückliche Bitte.
    assert "Simply writing in another language is NOT such a request" in personality
    assert "Antworte standardmäßig" not in personality and "Schreibt er auf Urdu" not in personality


def test_shipped_hamid_personality_is_informal_and_prefers_urdu() -> None:
    personality = load_personality(PROJECT_ROOT / "custom_personalities" / "hamid.md")

    assert "Hamid" in personality and "{owner_name}" in personality
    assert "Assalam Alaykum" in personality and "Khuda Hafiz" in personality and "Allah Hafiz" in personality
    assert "keine KI" in personality and "Tum" in personality
    assert "Antworte standardmäßig auf Urdu" in personality


def test_load_personality_reads_utf8_and_strips_bom_and_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "personality.md"
    path.write_bytes("\ufeff\n  Salam Alaykum, ich bin Layla.  \n\n".encode())

    assert load_personality(path) == "Salam Alaykum, ich bin Layla."


def test_missing_personality_file_gives_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nicht gefunden"):
        load_personality(tmp_path / "gibt-es-nicht.md")


@pytest.mark.parametrize("content", ["", "   \n\n"], ids=["empty", "blank"])
def test_empty_personality_file_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "personality.md"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match="leer"):
        load_personality(path)


def test_too_long_personality_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "personality.md"
    path.write_text("x" * (MAX_PERSONALITY_CHARS + 1), encoding="utf-8")

    with pytest.raises(ConfigError, match="zu lang"):
        load_personality(path)


def test_invalid_encoding_and_directories_are_config_errors(tmp_path: Path) -> None:
    broken = tmp_path / "broken.md"
    broken.write_bytes(b"\xff\xfe\x00\xd8 kaputt")

    with pytest.raises(ConfigError, match="gelesen"):
        load_personality(broken)
    with pytest.raises(ConfigError, match="gelesen"):
        load_personality(tmp_path)


def test_system_prompt_starts_with_personality_and_replaces_owner_name() -> None:
    prompt = build_system_prompt("Ich bin Layla. Ich arbeite für {owner_name}.", "Omar")

    assert prompt.startswith("Ich bin Layla. Ich arbeite für Omar.")
    assert "{owner_name}" not in prompt
    assert "Omar" in prompt.split("AUFGABEN", 1)[1]


def test_system_prompt_tolerates_other_braces_in_the_personality_file() -> None:
    prompt = build_system_prompt('Schreibe Zahlen wie {1,2} oder {"a": 1}.', "Omar")

    assert prompt.startswith('Schreibe Zahlen wie {1,2} oder {"a": 1}.')


def test_system_prompt_always_contains_the_shared_rules() -> None:
    prompt = build_system_prompt("Persona", "Omar")

    assert "Erfinde keine Fakten" in prompt and "nur nach erforderlicher Freigabe" in prompt
    assert SHARED_PROMPT_RULES.format(owner_name="Omar") in prompt
