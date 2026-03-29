from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from shared.config.index import POSTGRES_DB, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER

_pool: psycopg.Connection | None = None
_init_done = False


def _get_pool() -> psycopg.Connection:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg.connect(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=os.getenv("POSTGRES_PASSWORD", "rag"),
            dbname=POSTGRES_DB,
            row_factory=dict_row,
            autocommit=True,
        )
    return _pool


def _resolve_migrations_dir() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        (here / "../migrations").resolve(),
        (Path.cwd() / "migrations").resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Migrations directory not found. Checked: {', '.join(str(c) for c in candidates)}")


def _should_retry_connection(error: Exception) -> bool:
    code = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    if code in {"57P03"}:
        return True
    message = str(error).lower()
    return any(fragment in message for fragment in ["refused", "timedout", "timeout", "reset", "connection"])


def ensure_database_ready() -> None:
    global _init_done
    if _init_done:
        return

    retries = int(os.getenv("POSTGRES_CONNECT_RETRIES", "30"))
    delay_ms = int(os.getenv("POSTGRES_CONNECT_DELAY_MS", "1000"))

    for attempt in range(1, retries + 1):
        try:
            _run_migrations()
            _init_done = True
            return
        except Exception as error:
            if attempt >= retries or not _should_retry_connection(error):
                raise
            print(f"[db] connection attempt {attempt}/{retries} failed. Retrying in {delay_ms}ms...")
            time.sleep(delay_ms / 1000)


def db_query(text: str, params: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    conn = _get_pool()
    with conn.cursor() as cur:
        cur.execute(text, params or [])
        if cur.description:
            return list(cur.fetchall())
        return []


def ping_database() -> bool:
    db_query("SELECT 1")
    return True


def _run_migrations() -> None:
    conn = _get_pool()
    db_query(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    migration_dir = _resolve_migrations_dir()
    files = sorted([f for f in migration_dir.iterdir() if f.suffix == ".sql"])

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (937112,))

    try:
        for file_path in files:
            version = file_path.stem
            if db_query("SELECT 1 FROM schema_migrations WHERE version = %s", [version]):
                continue

            sql = file_path.read_text(encoding="utf-8")
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
