from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "rag")
POSTGRES_USER = os.getenv("POSTGRES_USER", "rag")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "rag")

_pool: psycopg.Connection | None = None


def get_conn() -> psycopg.Connection:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            dbname=POSTGRES_DB,
            row_factory=dict_row,
            autocommit=True,
        )
    return _pool


def db_query(text: str, params: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(text, params or [])
        if cur.description:
            return list(cur.fetchall())
        return []


def ping_database() -> bool:
    db_query("SELECT 1")
    return True


def _resolve_migrations_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "migrations",
        Path.cwd() / "migrations",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Migrations directory not found. Checked: {', '.join(str(c) for c in candidates)}")


def _should_retry(error: Exception) -> bool:
    code = getattr(error, "sqlstate", "") or getattr(error, "pgcode", "") or ""
    message = str(error).lower()
    return code in {"57P03"} or "connection" in message or "timeout" in message or "refused" in message


def ensure_database_ready() -> None:
    retries = int(os.getenv("POSTGRES_CONNECT_RETRIES", "30"))
    delay_ms = int(os.getenv("POSTGRES_CONNECT_DELAY_MS", "1000"))

    for attempt in range(1, retries + 1):
        try:
            _run_migrations()
            return
        except Exception as error:
            if attempt >= retries or not _should_retry(error):
                raise
            print(
                f"[db] connection attempt {attempt}/{retries} failed. Retrying in {delay_ms}ms..."
            )
            time.sleep(delay_ms / 1000)


def _run_migrations() -> None:
    conn = get_conn()
    db_query(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    migrations_dir = _resolve_migrations_dir()
    migration_files = sorted([p for p in migrations_dir.iterdir() if p.suffix == ".sql"])

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (937112,))
    try:
        for migration in migration_files:
            version = migration.stem
            exists = db_query("SELECT 1 FROM schema_migrations WHERE version = %s", [version])
            if exists:
                continue
            sql = migration.read_text(encoding="utf-8")
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (937112,))
