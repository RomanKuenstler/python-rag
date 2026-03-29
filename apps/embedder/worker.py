from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

EMBED_INTERVAL_SECONDS = int(os.getenv("EMBED_INTERVAL_SECONDS", "15"))
EMBEDDER_HEALTH_PORT = int(os.getenv("EMBEDDER_HEALTH_PORT", "3200"))
EMBEDDER_HEALTH_HOST = os.getenv("EMBEDDER_HEALTH_HOST", "0.0.0.0")

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "rag")
POSTGRES_USER = os.getenv("POSTGRES_USER", "rag")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "rag")

app = FastAPI()


def get_conn():
    return psycopg.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DB,
        autocommit=True,
    )


def read_embedding_status():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, started_at, finished_at, summary, error_message, last_job_id, updated_at
                    FROM embedding_status
                    WHERE singleton = TRUE
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "status": row[0],
                    "startedAt": row[1].isoformat().replace("+00:00", "Z") if row[1] else None,
                    "finishedAt": row[2].isoformat().replace("+00:00", "Z") if row[2] else None,
                    "summary": row[3],
                    "errorMessage": row[4],
                    "lastJobId": row[5],
                    "updatedAt": row[6].isoformat().replace("+00:00", "Z") if row[6] else None,
                }
    except Exception:
        return None


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "embedder"}


@app.get("/internal/embedder/status")
def embedder_status():
    return {
        "ok": True,
        "service": "embedder",
        "intervalSeconds": EMBED_INTERVAL_SECONDS,
        "embeddingStatus": read_embedding_status(),
    }


def start_health_server():
    config = uvicorn.Config(app, host=EMBEDDER_HEALTH_HOST, port=EMBEDDER_HEALTH_PORT, log_level="info")
    server = uvicorn.Server(config)
    server.run()


def run_index_once() -> tuple[bool, dict | None]:
    proc = subprocess.run(
        ["node", "apps/embedder/indexer-once.mjs"],
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="")

    summary = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("__SUMMARY_JSON__"):
            try:
                summary = json.loads(line.replace("__SUMMARY_JSON__", "", 1))
            except json.JSONDecodeError:
                summary = None
    return proc.returncode == 0, summary


async def run_loop():
    print(f"[embedder] started (interval={EMBED_INTERVAL_SECONDS}s)")
    while True:
        ok, summary = run_index_once()
        if not ok:
            print("[embedder] indexing loop failed")
        elif summary is not None:
            print(f"[embedder] summary at {datetime.now(timezone.utc).isoformat()}: {summary.get('status', 'completed')}")
        await asyncio.sleep(EMBED_INTERVAL_SECONDS)


def main():
    thread = threading.Thread(target=start_health_server, daemon=True)
    thread.start()
    asyncio.run(run_loop())


if __name__ == "__main__":
    main()
