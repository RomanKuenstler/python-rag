from __future__ import annotations

import json
import os
from pathlib import Path

from .auth_utils import get_global_password_salt, hash_password_with_global_salt
from .db import db_query, get_conn

DEFAULT_USERNAME = "default"
DEFAULT_DISPLAY_NAME = "Default User"
DEFAULT_PASSWORD = "default"
DEFAULT_ROLE = "users"
DEFAULT_ADMIN_USERNAME = "defaultadm"
DEFAULT_ADMIN_DISPLAY_NAME = "Default Admin"
DEFAULT_ADMIN_PASSWORD = "defaultadm"
ADMIN_ROLE = "admin"
USERS_CONFIG_PATH = str(os.getenv("AUTH_USERS_FILE", "")).strip()
INITIAL_USER_PASSWORD = str(os.getenv("AUTH_INITIAL_PASSWORD", "Passw0rd!"))


def _normalize_role(value: str) -> str:
    return ADMIN_ROLE if str(value or "").strip().lower() == ADMIN_ROLE else DEFAULT_ROLE


def _normalize_configured_users(raw_config: dict) -> list[dict]:
    lst = raw_config if isinstance(raw_config, list) else raw_config.get("users", []) if isinstance(raw_config, dict) else []
    out = []
    for entry in lst:
        username = str((entry or {}).get("username", "")).strip()
        display_name = str((entry or {}).get("display_name", "")).strip()
        if not username or not display_name:
            continue
        if username in {DEFAULT_USERNAME, DEFAULT_ADMIN_USERNAME}:
            continue
        out.append({"username": username, "displayName": display_name, "role": _normalize_role((entry or {}).get("role"))})
    return out


def _ensure_default_users() -> list[int]:
    global_salt = get_global_password_salt()
    defaults = [
        {"username": DEFAULT_USERNAME, "displayName": DEFAULT_DISPLAY_NAME, "password": DEFAULT_PASSWORD, "role": DEFAULT_ROLE},
        {"username": DEFAULT_ADMIN_USERNAME, "displayName": DEFAULT_ADMIN_DISPLAY_NAME, "password": DEFAULT_ADMIN_PASSWORD, "role": ADMIN_ROLE},
    ]
    ids = []
    for entry in defaults:
        hashed_password = hash_password_with_global_salt(entry["password"])
        rows = db_query(
            """
            INSERT INTO users (username, display_name, password_hash, password_salt, role, is_active, require_changepw, updated_at)
            VALUES (%s, %s, %s, %s, %s, TRUE, FALSE, NOW())
            ON CONFLICT (username) DO UPDATE
              SET display_name = EXCLUDED.display_name,
                  password_hash = EXCLUDED.password_hash,
                  password_salt = EXCLUDED.password_salt,
                  role = EXCLUDED.role,
                  is_active = TRUE,
                  require_changepw = FALSE,
                  updated_at = NOW()
            RETURNING id
            """,
            [entry["username"], entry["displayName"], hashed_password, global_salt, entry["role"]],
        )
        if rows and rows[0].get("id"):
            ids.append(rows[0]["id"])
    return ids


def _resolve_users_config_path() -> Path:
    if USERS_CONFIG_PATH:
        return Path(USERS_CONFIG_PATH).resolve()
    return (Path.cwd() / "users.json").resolve()


def sync_users_from_config_file(file_path: Path | None = None) -> dict:
    path = file_path or _resolve_users_config_path()
    default_user_ids = _ensure_default_users()

    parsed = {"users": []}
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw) if raw.strip() else {"users": []}
    except FileNotFoundError:
        pass

    configured_users = _normalize_configured_users(parsed)

    conn = get_conn()
    conn.autocommit = False
    try:
        for user in configured_users:
            pw_hash = hash_password_with_global_salt(INITIAL_USER_PASSWORD)
            global_salt = get_global_password_salt()
            db_query(
                """
                INSERT INTO users (username, display_name, password_hash, password_salt, role, is_active, require_changepw, updated_at)
                VALUES (%s, %s, %s, %s, %s, TRUE, TRUE, NOW())
                ON CONFLICT (username) DO UPDATE
                  SET display_name = EXCLUDED.display_name,
                      role = EXCLUDED.role,
                      is_active = TRUE,
                      updated_at = NOW()
                """,
                [user["username"], user["displayName"], pw_hash, global_salt, user["role"]],
            )

        protected_usernames = [DEFAULT_USERNAME, DEFAULT_ADMIN_USERNAME]
        configured_usernames = [x["username"] for x in configured_users]
        if configured_usernames:
            db_query(
                """
                UPDATE users
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE username <> ALL(%s::text[])
                  AND username <> ALL(%s::text[])
                  AND is_active = TRUE
                """,
                [protected_usernames, configured_usernames],
            )
        else:
            db_query(
                """
                UPDATE users
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE username <> ALL(%s::text[])
                  AND is_active = TRUE
                """,
                [protected_usernames],
            )

        db_query(
            """
            UPDATE users
            SET is_active = TRUE,
                updated_at = NOW()
            WHERE id = ANY(%s::bigint[])
            """,
            [default_user_ids],
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True

    return {"filePath": str(path), "configured": len(configured_users)}
