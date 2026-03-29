from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .auth_utils import get_global_password_salt, hash_password_with_global_salt, hash_password_with_salt
from .db import db_query, ensure_database_ready, ping_database
from .library_service import (
    delete_managed_library_file_for_user,
    list_managed_library_files,
    save_managed_library_file,
    toggle_managed_library_file,
)
from .user_bootstrap import sync_users_from_config_file

MAX_LIBRARY_UPLOAD_FILES_PER_REQUEST = 5
SESSION_INITIAL_TTL_MS = 2 * 60 * 60 * 1000
SESSION_REFRESH_THRESHOLD_MS = SESSION_INITIAL_TTL_MS / 2
SESSION_MAX_LIFETIME_MS = 24 * 60 * 60 * 1000

PORT = int(os.getenv("BACKEND_API_PORT", "3100"))
HOST = os.getenv("BACKEND_API_HOST", "0.0.0.0")
RETRIEVER_BASE_URL = os.getenv("RETRIEVER_BASE_URL", "http://retriever:3000")
EMBEDDER_BASE_URL = os.getenv("EMBEDDER_BASE_URL", "http://embedder:3200")
OCR_SCANNER_BASE_URL = os.getenv("OCR_SCANNER_BASE_URL", "http://ocr-scanner:3300")
AUDIO_TRANSCRIPTION_BASE_URL = os.getenv("AUDIO_TRANSCRIPTION_BASE_URL", "http://audio-transcription:3400")
MAX_API_BODY_BYTES = int(os.getenv("MAX_API_BODY_BYTES", str(25 * 1024 * 1024)))
ADMIN_EDIT_PROTECTED_USERNAMES = {"default", "defaultadm"}

CHAT_INPUT_AUDIO_EXTENSIONS = {"wav", "mp3", "m4a", "webm"}
CHAT_INPUT_TRANSCRIPTION_MODES = {"translate", "transcribe"}
PROMPT_AUDIO_ATTACHMENT_EXTENSIONS = {".wav", ".mp3", ".m4a", ".webm"}

app = FastAPI()


def json_response(status: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=payload,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Session-Token",
        },
    )


@app.middleware("http")
async def payload_limit_middleware(request: Request, call_next):
    body = await request.body()
    if len(body) > MAX_API_BODY_BYTES:
        return json_response(413, {"error": "Payload too large"})
    request.state.cached_body = body
    return await call_next(request)


def parse_json_body(request: Request) -> dict[str, Any]:
    raw = getattr(request.state, "cached_body", b"")
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def hash_session_token(token: str) -> str:
    return hashlib.sha256(f"{get_global_password_salt()}:{str(token or '')}".encode("utf-8")).hexdigest()


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_session_window(now: datetime | None = None) -> dict[str, datetime]:
    created_at = now or datetime.now(timezone.utc)
    max_expires_at = created_at + timedelta(milliseconds=SESSION_MAX_LIFETIME_MS)
    expires_at = min(created_at + timedelta(milliseconds=SESSION_INITIAL_TTL_MS), max_expires_at)
    return {"createdAt": created_at, "expiresAt": expires_at, "maxExpiresAt": max_expires_at}


def get_session_id_from_request(request: Request, body: dict[str, Any] | None = None) -> str:
    from_query = str(request.query_params.get("sessionId") or "").strip()
    if from_query:
        return from_query
    return str((body or {}).get("sessionId") or "").strip()


def create_or_replace_session(user_id: int, session_id: str) -> dict[str, Any]:
    window = build_session_window()
    session_token = str(uuid.uuid4())
    session_token_hash = hash_session_token(session_token)
    db_query(
        """
        INSERT INTO sessions (user_id, session_identifier, session_token_hash, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (session_identifier) DO UPDATE
          SET user_id = EXCLUDED.user_id,
              session_token_hash = EXCLUDED.session_token_hash,
              created_at = EXCLUDED.created_at,
              expires_at = EXCLUDED.expires_at
        """,
        [user_id, session_id, session_token_hash, to_iso(window["createdAt"]), to_iso(window["expiresAt"])],
    )
    return {
        "sessionId": session_id,
        "sessionToken": session_token,
        "createdAt": to_iso(window["createdAt"]),
        "expiresAt": to_iso(window["expiresAt"]),
        "maxExpiresAt": to_iso(window["maxExpiresAt"]),
    }


def _parse_iso(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_and_refresh_session(request: Request, body: dict[str, Any] | None = None, refresh: bool = True) -> dict[str, Any]:
    requested_session_id = get_session_id_from_request(request, body)
    session_token = str(request.headers.get("x-session-token") or "").strip()
    if not session_token:
        return {"ok": False, "statusCode": 401, "error": "Missing session token."}
    expected_token_hash = hash_session_token(session_token)

    if requested_session_id:
        rows = db_query(
            """
            SELECT s.user_id, s.session_identifier, s.session_token_hash, s.created_at, s.expires_at, u.username, u.display_name, u.role
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_identifier = %s
            LIMIT 1
            """,
            [requested_session_id],
        )
    else:
        rows = db_query(
            """
            SELECT s.user_id, s.session_identifier, s.session_token_hash, s.created_at, s.expires_at, u.username, u.display_name, u.role
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token_hash = %s
            LIMIT 1
            """,
            [expected_token_hash],
        )

    session = rows[0] if rows else None
    if not session or not session.get("session_token_hash"):
        return {"ok": False, "statusCode": 401, "error": "Session not found."}

    resolved_session_id = str(session.get("session_identifier") or "").strip()
    if expected_token_hash != session.get("session_token_hash"):
        return {"ok": False, "statusCode": 401, "error": "Session token is invalid."}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    created_ms = int(_parse_iso(session.get("created_at")).timestamp() * 1000)
    expires_at = session.get("expires_at")
    expires_ms = int(_parse_iso(expires_at).timestamp() * 1000) if expires_at else 0
    max_expires_ms = created_ms + int(SESSION_MAX_LIFETIME_MS)

    if now_ms >= max_expires_ms:
        db_query("DELETE FROM sessions WHERE session_identifier = %s", [resolved_session_id])
        return {"ok": False, "statusCode": 401, "error": "Session reached its maximum lifetime. Please log in again."}
    if not expires_ms or now_ms >= expires_ms:
        db_query("DELETE FROM sessions WHERE session_identifier = %s", [resolved_session_id])
        return {"ok": False, "statusCode": 401, "error": "Session expired. Please log in again."}

    next_expires = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
    if refresh and (expires_ms - now_ms) <= SESSION_REFRESH_THRESHOLD_MS:
        refreshed_ms = min(now_ms + int(SESSION_INITIAL_TTL_MS), max_expires_ms)
        if refreshed_ms > expires_ms:
            next_expires = datetime.fromtimestamp(refreshed_ms / 1000, tz=timezone.utc)
            db_query("UPDATE sessions SET expires_at = %s WHERE session_identifier = %s", [to_iso(next_expires), resolved_session_id])

    return {
        "ok": True,
        "session": {
            "userId": int(session.get("user_id")),
            "sessionId": resolved_session_id,
            "username": session.get("username"),
            "displayName": session.get("display_name"),
            "role": session.get("role"),
            "createdAt": to_iso(datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)),
            "expiresAt": to_iso(next_expires),
            "maxExpiresAt": to_iso(datetime.fromtimestamp(max_expires_ms / 1000, tz=timezone.utc)),
        },
    }


async def fetch_json(url: str) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(url)
            text = response.text
            return json.loads(text) if text else None
        except Exception:
            return None


def is_admin_session(session: dict[str, Any]) -> bool:
    return str(session.get("role") or "").strip().lower() == "admin"


def normalize_user_role(input_role: Any) -> str:
    return "admin" if str(input_role or "").strip().lower() == "admin" else "users"


def cors_response(status: int, content: bytes, content_type: str) -> Response:
    return Response(
        status_code=status,
        content=content,
        media_type=content_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Session-Token",
        },
    )


async def proxy_retriever(request: Request, target_path: str, session_id: str | None) -> Response:
    method = request.method
    target_url = f"{RETRIEVER_BASE_URL}{target_path}"
    query_items = list(request.query_params.multi_items())
    if session_id:
        query_items.append(("sessionId", session_id))
    if query_items:
        target_url += ("&" if "?" in target_url else "?") + urlencode(query_items)

    content_type = request.headers.get("content-type", "application/json")
    body = None
    if method in {"POST", "PATCH", "DELETE"}:
        raw = getattr(request.state, "cached_body", b"")
        raw_text = raw.decode("utf-8") if raw else ""
        if session_id and raw_text and "application/json" in content_type.lower():
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    parsed["sessionId"] = session_id
                    body = json.dumps(parsed).encode("utf-8")
                else:
                    body = raw
            except Exception:
                body = raw
        elif session_id and not raw_text and "application/json" in content_type.lower():
            body = json.dumps({"sessionId": session_id}).encode("utf-8")
        else:
            body = raw

    async with httpx.AsyncClient(timeout=120) as client:
        upstream = await client.request(method, target_url, headers={"content-type": content_type}, content=body)
    return cors_response(upstream.status_code, upstream.content, upstream.headers.get("content-type", "application/json"))


@app.options("/{path:path}")
async def options_handler(path: str):
    return json_response(200, {"ok": True})


@app.post("/api/auth/login")
async def auth_login(request: Request):
    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    session_id = get_session_id_from_request(request, body)
    if not username or not password:
        return json_response(400, {"ok": False, "error": "Username and password are required."})
    if not session_id:
        return json_response(400, {"ok": False, "error": "Session id is required."})

    rows = db_query(
        """
        SELECT id, username, display_name, password_hash, password_salt, role, is_active, require_changepw
        FROM users WHERE username = %s LIMIT 1
        """,
        [username],
    )
    user = rows[0] if rows else None
    if not user:
        return json_response(404, {"ok": False, "error": "User does not exist."})
    if not user.get("is_active"):
        return json_response(403, {"ok": False, "error": "User account is inactive."})

    if hash_password_with_salt(password, user.get("password_salt")) != user.get("password_hash"):
        return json_response(401, {"ok": False, "error": "Invalid password."})

    if user.get("require_changepw"):
        return json_response(200, {"ok": True, "requirePasswordChange": True, "user": {"id": user["id"], "username": user["username"], "displayName": user["display_name"], "role": user["role"]}})

    session = create_or_replace_session(user["id"], session_id)
    return json_response(200, {"ok": True, "requirePasswordChange": False, "user": {"id": user["id"], "username": user["username"], "displayName": user["display_name"], "role": user["role"]}, "session": session})


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    username = str(body.get("username") or "").strip()
    old_password = str(body.get("oldPassword") or "")
    new_password = str(body.get("newPassword") or "")
    confirm_new_password = str(body.get("confirmNewPassword") or "")
    session_id = get_session_id_from_request(request, body)
    if not username or not old_password or not new_password or not confirm_new_password:
        return json_response(400, {"ok": False, "error": "All fields are required."})
    if not session_id:
        return json_response(400, {"ok": False, "error": "Session id is required."})
    if new_password != confirm_new_password:
        return json_response(400, {"ok": False, "error": "New password and confirmation do not match."})

    rows = db_query("SELECT id, username, display_name, password_hash, password_salt, role, is_active FROM users WHERE username = %s LIMIT 1", [username])
    user = rows[0] if rows else None
    if not user:
        return json_response(404, {"ok": False, "error": "User does not exist."})
    if not user.get("is_active"):
        return json_response(403, {"ok": False, "error": "User account is inactive."})
    if hash_password_with_salt(old_password, user.get("password_salt")) != user.get("password_hash"):
        return json_response(401, {"ok": False, "error": "Old password is invalid."})

    db_query("UPDATE users SET password_hash = %s, password_salt = %s, require_changepw = FALSE, updated_at = NOW() WHERE id = %s", [hash_password_with_global_salt(new_password), get_global_password_salt(), user["id"]])
    session = create_or_replace_session(user["id"], session_id)
    return json_response(200, {"ok": True, "requirePasswordChange": False, "user": {"id": user["id"], "username": user["username"], "displayName": user["display_name"], "role": user["role"]}, "session": session})


@app.get("/api/auth/session")
async def auth_session(request: Request):
    validation = validate_and_refresh_session(request, refresh=True)
    if not validation.get("ok"):
        return json_response(validation.get("statusCode", 401), {"ok": False, "error": validation.get("error")})
    session = validation["session"]
    return json_response(200, {"ok": True, "user": {"username": session["username"], "displayName": session["displayName"], "role": session["role"]}, "session": {"sessionId": session["sessionId"], "createdAt": session["createdAt"], "expiresAt": session["expiresAt"], "maxExpiresAt": session["maxExpiresAt"]}})


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})
    validation = validate_and_refresh_session(request, body=body, refresh=False)
    if not validation.get("ok"):
        return json_response(validation.get("statusCode", 401), {"ok": False, "error": validation.get("error")})
    db_query("DELETE FROM sessions WHERE session_identifier = %s", [validation["session"]["sessionId"]])
    return json_response(200, {"ok": True, "loggedOut": True})


def require_session(request: Request, body: dict[str, Any] | None = None, refresh: bool = True):
    validated = validate_and_refresh_session(request, body=body, refresh=refresh)
    if not validated.get("ok"):
        return None, json_response(validated.get("statusCode", 401), {"ok": False, "error": validated.get("error")})
    return validated["session"], None


@app.get("/healthz")
async def healthz():
    try:
        ping_database()
        db = {"ok": True}
    except Exception as exc:
        db = {"ok": False, "error": str(exc)}
    return json_response(200 if db["ok"] else 503, {"ok": db["ok"], "service": "backend-api", "retrieverBaseUrl": RETRIEVER_BASE_URL, "embedderBaseUrl": EMBEDDER_BASE_URL, "postgres": db})


@app.get("/api/status")
async def api_status(request: Request):
    session, error = require_session(request)
    if error:
        return error
    sid = session["sessionId"]
    retriever_status = await fetch_json(f"{RETRIEVER_BASE_URL}/internal/retriever/status?sessionId={sid}")
    embedder_status = await fetch_json(f"{EMBEDDER_BASE_URL}/internal/embedder/status")
    ocr_status = await fetch_json(f"{OCR_SCANNER_BASE_URL}/healthz")
    audio_status = await fetch_json(f"{AUDIO_TRANSCRIPTION_BASE_URL}/healthz")
    payload = {
        **(retriever_status or {}),
        "orchestration": {"entrypoint": "backend-api", "version": "v1"},
        "services": {
            "backend": {"role": "backend-api", "baseUrl": f"http://{HOST}:{PORT}"},
            "retriever": {"role": ((retriever_status or {}).get("app") or {}).get("role", "retriever-api"), "baseUrl": RETRIEVER_BASE_URL},
            "embedder": {"role": (embedder_status or {}).get("service", "embedder"), "baseUrl": EMBEDDER_BASE_URL, "status": embedder_status},
            "ocrScanner": {"role": (ocr_status or {}).get("service", "ocr-scanner"), "baseUrl": OCR_SCANNER_BASE_URL, "status": (ocr_status or {}).get("status") or ("active" if ocr_status else "disconnected")},
            "audioTranscription": {"role": (audio_status or {}).get("service", "audio-transcription"), "baseUrl": AUDIO_TRANSCRIPTION_BASE_URL, "status": (audio_status or {}).get("status") or ("active" if audio_status else "disconnected")},
        },
    }
    if (embedder_status or {}).get("embeddingStatus"):
        payload.setdefault("embedding", {})["workerStatus"] = embedder_status.get("embeddingStatus")
    return json_response(200, payload)


@app.api_route("/api/admin/users", methods=["GET", "POST"])
async def admin_users(request: Request):
    session, error = require_session(request)
    if error:
        return error
    if not is_admin_session(session):
        return json_response(403, {"ok": False, "error": "Admin access required."})

    if request.method == "GET":
        users = db_query("SELECT username, is_active, require_changepw FROM users ORDER BY username ASC")
        return json_response(200, {"ok": True, "users": [{"username": u["username"], "isActive": bool(u["is_active"]), "requireChangePw": bool(u["require_changepw"])} for u in users]})

    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    username = str(body.get("username") or "").strip()
    display_name = str(body.get("displayName") or "").strip()
    role = normalize_user_role(body.get("role"))
    if len(username) < 4 or len(display_name) < 2:
        return json_response(400, {"ok": False, "error": "Username or display name is too short."})
    if username in ADMIN_EDIT_PROTECTED_USERNAMES:
        return json_response(400, {"ok": False, "error": "This username is reserved."})

    try:
        rows = db_query(
            """
            INSERT INTO users (username, display_name, password_hash, password_salt, role, is_active, require_changepw, updated_at)
            VALUES (%s, %s, %s, %s, %s, TRUE, TRUE, NOW())
            RETURNING username, display_name, role, is_active, require_changepw
            """,
            [username, display_name, hash_password_with_global_salt(os.getenv("AUTH_INITIAL_PASSWORD", "Passw0rd!")), get_global_password_salt(), role],
        )
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            return json_response(409, {"ok": False, "error": "Username already exists."})
        raise
    created = rows[0]
    return json_response(201, {"ok": True, "user": {"username": created["username"], "displayName": created["display_name"], "role": created["role"], "isActive": bool(created["is_active"]), "requireChangePw": bool(created["require_changepw"])}})


@app.api_route("/api/admin/users/{username}", methods=["PATCH", "DELETE"])
async def admin_user_update_delete(username: str, request: Request):
    session, error = require_session(request)
    if error:
        return error
    if not is_admin_session(session):
        return json_response(403, {"ok": False, "error": "Admin access required."})
    username = username.strip()
    if not username:
        return json_response(400, {"ok": False, "error": "Username is required."})

    if request.method == "DELETE":
        if username == session["username"]:
            return json_response(400, {"ok": False, "error": "You cannot delete your own account."})
        if username in ADMIN_EDIT_PROTECTED_USERNAMES:
            return json_response(400, {"ok": False, "error": "This user cannot be deleted."})
        rows = db_query("DELETE FROM users WHERE username = %s RETURNING id, username", [username])
        if not rows:
            return json_response(404, {"ok": False, "error": "User does not exist."})
        db_query("DELETE FROM sessions WHERE user_id = %s", [rows[0]["id"]])
        return json_response(200, {"ok": True, "deleted": True, "username": rows[0]["username"]})

    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    has_is_active = isinstance(body.get("isActive"), bool)
    has_require_change = isinstance(body.get("requireChangePw"), bool)
    if not has_is_active and not has_require_change:
        return json_response(400, {"ok": False, "error": "At least one update flag is required."})
    if username == session["username"] and has_is_active and body.get("isActive") is False:
        return json_response(400, {"ok": False, "error": "You cannot deactivate your own account."})
    if username in ADMIN_EDIT_PROTECTED_USERNAMES:
        return json_response(400, {"ok": False, "error": "This user cannot be modified."})

    updates, values = [], []
    if has_is_active:
        values.append(body.get("isActive"))
        updates.append(f"is_active = %s")
    if has_require_change:
        values.append(body.get("requireChangePw"))
        updates.append("require_changepw = %s")
    values.append(username)

    rows = db_query(f"UPDATE users SET {', '.join(updates)}, updated_at = NOW() WHERE username = %s RETURNING username, is_active, require_changepw", values)
    if not rows:
        return json_response(404, {"ok": False, "error": "User does not exist."})
    updated = rows[0]
    return json_response(200, {"ok": True, "user": {"username": updated["username"], "isActive": bool(updated["is_active"]), "requireChangePw": bool(updated["require_changepw"])}})


@app.api_route("/api/library/files", methods=["GET", "POST", "DELETE", "PATCH"])
async def library_files(request: Request):
    session, error = require_session(request)
    if error:
        return error

    if request.method == "GET":
        files = list_managed_library_files(user_id=session["userId"], is_admin=is_admin_session(session))
        return json_response(200, {"ok": True, "files": files, "total": len(files), "ready": len([f for f in files if f.get('uploadStatus') == 'ready']), "embedding": len([f for f in files if f.get('uploadStatus') == 'embedding']), "error": len([f for f in files if f.get('uploadStatus') == 'error'])})

    if request.method == "DELETE":
        file_path = str(request.query_params.get("path") or "")
        if not file_path:
            return json_response(400, {"ok": False, "error": "Missing 'path' query parameter."})
        result = delete_managed_library_file_for_user(file_path, user_id=session["userId"], is_admin=is_admin_session(session))
        if not result.get("deleted"):
            if result.get("reason") == "not_owner":
                return json_response(403, {"ok": False, "error": "You can only delete files that you uploaded."})
            return json_response(404, {"ok": False, "error": "Managed file not found."})
        return json_response(200, {"ok": True, "path": result.get("path"), "removedVectors": result.get("removedVectors")})

    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    if request.method == "PATCH":
        file_path = str(body.get("path") or "")
        action = str(body.get("action") or "").lower()
        if not file_path:
            return json_response(400, {"ok": False, "error": "Missing 'path' in request body."})
        if action not in {"disable", "activate"}:
            return json_response(400, {"ok": False, "error": "Action must be either 'disable' or 'activate'."})
        result = toggle_managed_library_file(file_path, action == "activate", user_id=session["userId"])
        if not result.get("updated"):
            return json_response(404, {"ok": False, "error": "Managed file not found for this user."})
        return json_response(200, {"ok": True, "file": result})

    raw_files = body.get("files") if isinstance(body.get("files"), list) else ([{"name": body.get("name"), "contentBase64": body.get("contentBase64"), "overwrite": body.get("overwrite"), "tags": body.get("tags")}] if (body.get("name") or body.get("contentBase64")) else [])
    if not raw_files:
        return json_response(400, {"ok": False, "error": "No files provided."})
    if len(raw_files) > MAX_LIBRARY_UPLOAD_FILES_PER_REQUEST:
        return json_response(400, {"ok": False, "error": f"Please upload up to {MAX_LIBRARY_UPLOAD_FILES_PER_REQUEST} files per request."})

    results = []
    for entry in raw_files:
        try:
            file = save_managed_library_file(file_name=entry.get("name"), content_base64=entry.get("contentBase64"), overwrite=bool(entry.get("overwrite")), tags=entry.get("tags"), uploaded_by_user_id=session["userId"], save_to_root=is_admin_session(session))
            results.append({"ok": True, "fileName": entry.get("name"), "file": file})
        except Exception as exc:
            results.append({"ok": False, "fileName": entry.get("name"), "error": str(exc)})

    all_ok = all(x.get("ok") for x in results)
    status_code = 201 if all_ok else 207 if any(x.get("ok") for x in results) else 400
    return json_response(status_code, {"ok": all_ok, "files": results})


async def transcribe_prompt_audio_attachment(file: dict[str, Any]) -> dict[str, Any]:
    raw_name = str(file.get("name") or "").strip()
    extension = f".{raw_name.lower().split('.')[-1]}" if "." in raw_name else ""
    if extension not in PROMPT_AUDIO_ATTACHMENT_EXTENSIONS:
        return {"ok": True, "file": file}
    audio_base64 = str(file.get("contentBase64") or "").strip()
    if not audio_base64:
        return {"ok": False, "error": f"Missing audio payload for attachment {raw_name or 'unknown'}."}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{AUDIO_TRANSCRIPTION_BASE_URL}/audio/transcribe", json={"request_type": "user_attach", "audio_base64": audio_base64, "audio_extension": extension.replace('.', ''), "transcription_mode": "translate"})
    payload = response.json() if response.text else {}
    if response.status_code >= 400 or payload.get("ok") is not True:
        return {"ok": False, "error": payload.get("error") or f"Audio transcription failed ({response.status_code})"}
    text = str(((payload.get("transcription") or {}).get("text")) or "").strip()
    if not text:
        return {"ok": False, "error": f"No text detected in audio attachment {raw_name or 'unknown'}."}
    return {"ok": True, "file": {"name": f"{raw_name}.transcription.txt", "content": text}}


@app.post("/api/prompt")
async def api_prompt(request: Request):
    session, error = require_session(request)
    if error:
        return error
    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    uploaded_files = body.get("uploadedFiles") if isinstance(body.get("uploadedFiles"), list) else []
    normalized = []
    for file in uploaded_files:
        converted = await transcribe_prompt_audio_attachment(file)
        if not converted.get("ok"):
            return json_response(400, {"ok": False, "error": converted.get("error") or "Audio attachment transcription failed."})
        normalized.append(converted["file"])

    async with httpx.AsyncClient(timeout=300) as client:
        upstream = await client.post(f"{RETRIEVER_BASE_URL}/internal/retriever/prompt", headers={"content-type": "application/json"}, json={**body, "uploadedFiles": normalized, "sessionId": session["sessionId"]})
    return cors_response(upstream.status_code, upstream.content, upstream.headers.get("content-type", "application/json"))


@app.post("/api/transcription/chat-input")
async def transcription_chat_input(request: Request):
    session, error = require_session(request)
    if error:
        return error
    try:
        body = parse_json_body(request)
    except Exception:
        return json_response(400, {"ok": False, "error": "Invalid JSON payload"})

    audio_base64 = str(body.get("audioBase64") or "").strip()
    requested_extension = str(body.get("audioExtension") or "").strip().lower().lstrip(".")
    requested_mode = str(body.get("transcriptionMode") or "").strip().lower()
    audio_ext = requested_extension if requested_extension in CHAT_INPUT_AUDIO_EXTENSIONS else "wav"
    mode = requested_mode if requested_mode in CHAT_INPUT_TRANSCRIPTION_MODES else "translate"
    if not audio_base64:
        return json_response(400, {"ok": False, "error": "Missing audioBase64 payload."})

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{AUDIO_TRANSCRIPTION_BASE_URL}/audio/transcribe", json={"request_type": "chat_input", "audio_base64": audio_base64, "audio_extension": audio_ext, "transcription_mode": mode})
    payload = response.json() if response.text else {}
    if response.status_code >= 400 or payload.get("ok") is not True:
        return json_response(response.status_code or 502, {"ok": False, "error": payload.get("error") or f"Audio transcription failed ({response.status_code})", "errorCode": payload.get("error_code") or "transcription_failed"})

    transcription = payload.get("transcription") or {}
    return json_response(200, {"ok": True, "transcription": {"text": str(transcription.get("text") or "").strip(), "detectedLanguage": transcription.get("detected_language") or "unknown", "durationSeconds": transcription.get("duration_seconds"), "mode": transcription.get("mode") or mode}})


PROXY_MAPPINGS = {
    ("GET", "/api/files"): ("/internal/retriever/files", True),
    ("PATCH", "/api/files/tags"): ("/internal/retriever/files/tags", False),
    ("GET", "/api/files/tag-filters"): ("/internal/retriever/files/tag-filters", True),
    ("PATCH", "/api/files/tag-filters"): ("/internal/retriever/files/tag-filters", True),
    ("GET", "/api/messages"): ("/internal/retriever/messages", True),
    ("GET", "/api/personalization"): ("/internal/retriever/personalization", True),
    ("PATCH", "/api/personalization"): ("/internal/retriever/personalization", True),
    ("GET", "/api/chats"): ("/internal/retriever/chats", True),
    ("POST", "/api/chats"): ("/internal/retriever/chats", True),
}


@app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def catch_all(path: str, request: Request):
    pathname = "/" + path

    session, error = require_session(request)
    if error:
        return error

    key = (request.method, pathname)
    if key in PROXY_MAPPINGS:
        target, _ = PROXY_MAPPINGS[key]
        return await proxy_retriever(request, target, session["sessionId"])

    if request.method == "GET" and pathname.startswith("/api/chats/") and pathname.endswith("/download"):
        suffix = pathname.replace("/api/chats/", "/internal/retriever/chats/", 1)
        return await proxy_retriever(request, suffix, session["sessionId"])

    if request.method in {"PATCH", "DELETE"} and pathname.startswith("/api/chats/"):
        suffix = pathname.replace("/api/chats/", "/internal/retriever/chats/", 1)
        return await proxy_retriever(request, suffix, session["sessionId"])

    return json_response(404, {"error": "Not found"})


def main():
    ensure_database_ready()
    synced = sync_users_from_config_file()
    print(f"[backend] synced users from {synced['filePath']} (configured: {synced['configured']})")
    print(f"Backend API listening on http://{HOST}:{PORT}")
    uvicorn.run("apps.backend.api:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
