FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY package*.json /app/
RUN npm install

COPY apps/embedder/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY apps /app/apps
COPY shared /app/shared
COPY migrations /app/migrations

RUN groupadd --gid 1001 nodejs && useradd --uid 1001 --gid nodejs --shell /bin/bash --create-home ai
RUN mkdir -p /app/state && chown -R ai:nodejs /app
USER ai

CMD ["python", "-m", "apps.embedder.worker"]
