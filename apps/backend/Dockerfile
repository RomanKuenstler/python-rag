FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY apps/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY apps /app/apps
COPY shared /app/shared
COPY migrations /app/migrations
COPY users.json /app/users.json

RUN groupadd --gid 1001 nodejs && useradd --uid 1001 --gid nodejs --shell /bin/bash --create-home ai
RUN mkdir -p /app/state && chown -R ai:nodejs /app

USER ai

ENV APP_ROLE=backend
CMD ["python", "-m", "apps.backend.api"]
