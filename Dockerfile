# syntax=docker/dockerfile:1

# Offizielles, schlankes Python-Image (Multi-Arch: amd64 und arm64).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Abhängigkeiten zuerst: Dieser Layer bleibt bei reinen Codeänderungen im Cache.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Unprivilegierter Benutzer und Datenverzeichnis für die SQLite-Datenbank.
RUN groupadd --system --gid 10001 assistant \
    && useradd --system --uid 10001 --gid assistant --no-create-home --shell /usr/sbin/nologin assistant \
    && mkdir -p /data \
    && chown assistant:assistant /data

COPY assistant_bot/ ./assistant_bot/
# Neutrale Standard-Persönlichkeit und die mitgelieferten Persönlichkeiten (z. B. custom_personalities/firzana.md).
# Ausgewählt wird per PERSONALITY_FILE.
COPY personality.md ./personality.md
COPY custom_personalities/ ./custom_personalities/

# Vorlagen zum Extrahieren ohne Git (siehe README): docker run --rm <image> cat /app/.env.example
COPY .env.example compose.yaml ./
ENV DATABASE_PATH=/data/assistant.db

USER assistant

# Exec-Form: Python läuft als PID 1 und erhält SIGTERM direkt (sauberes Beenden).
CMD ["python", "-m", "assistant_bot"]
