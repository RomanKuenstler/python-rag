from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from .db import db_query

COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "knowledge_base")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
CONTENT_PATH = os.getenv("CONTENT_PATH", "./data")
DEFAULT_FILE_TAG = (os.getenv("DEFAULT_FILE_TAG", "default").strip().lower() or "default")
EMBEDDABLE_EXTENSIONS = {e.lower() for e in [".md", ".txt", ".html", ".htm", ".pdf", ".epub", ".wav", ".mp3", ".m4a", ".webm"]}
LIBRARY_UPLOAD_SUBDIR = os.getenv("LIBRARY_UPLOAD_SUBDIR", "_library").strip()
MAX_LIBRARY_UPLOAD_BYTES = int(os.getenv("MAX_LIBRARY_UPLOAD_BYTES", str(15 * 1024 * 1024)))
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-:.]{0,63}$")


def _normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, list):
        items = tags
    elif isinstance(tags, str):
        items = tags.split(",")
    else:
        items = []
    normalized = []
    for tag in items:
        t = str(tag or "").strip().lower()
        if t and TAG_PATTERN.match(t):
            normalized.append(t)
    return list(dict.fromkeys(normalized))


def _normalize_filename(name: str) -> str:
    safe = Path(str(name or "").strip()).name
    return re.sub(r"[^\w.\-() ]+", "_", safe)


def is_allowed_library_file_extension(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in EMBEDDABLE_EXTENSIONS


def _ensure_path_inside_content_root(relative_path: str) -> Path:
    root = Path(CONTENT_PATH).resolve()
    target = (root / relative_path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("Resolved path escapes content root.")
    return target


def _set_file_tags_for_path(file_path: str, tags: list[str]) -> None:
    normalized_path = str(file_path or "").strip()
    if not normalized_path:
        return
    next_tags = tags if tags else [DEFAULT_FILE_TAG]
    db_query("DELETE FROM file_tags WHERE file_path = %s", [normalized_path])
    for tag in next_tags:
        db_query(
            """
            INSERT INTO file_tags (file_path, tag, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (file_path, tag) DO UPDATE SET updated_at = NOW()
            """,
            [normalized_path, tag],
        )


def _upsert_managed_library_file(file_path: str, original_name: str, size_bytes: int, status: str, uploaded_by_user_id: int | None) -> None:
    db_query(
        """
        INSERT INTO library_managed_files (
          file_path, original_name, source, upload_status, size_bytes, uploaded_by_user_id, uploaded_at, embedded_at, last_error, updated_at
        ) VALUES (%s, %s, 'webui', %s, %s, %s, NOW(), NULL, NULL, NOW())
        ON CONFLICT (file_path) DO UPDATE SET
          original_name = EXCLUDED.original_name,
          source = EXCLUDED.source,
          upload_status = EXCLUDED.upload_status,
          size_bytes = EXCLUDED.size_bytes,
          uploaded_by_user_id = EXCLUDED.uploaded_by_user_id,
          uploaded_at = NOW(),
          embedded_at = NULL,
          last_error = NULL,
          updated_at = NOW()
        """,
        [file_path, original_name, status, size_bytes, uploaded_by_user_id],
    )


def save_managed_library_file(*, file_name: str, content_base64: str, overwrite: bool = False, tags: Any = None, uploaded_by_user_id: int | None = None, save_to_root: bool = False) -> dict[str, Any]:
    normalized_name = _normalize_filename(file_name)
    if not normalized_name:
        raise ValueError("Missing file name.")
    if not is_allowed_library_file_extension(normalized_name):
        raise ValueError(f"Unsupported file extension for '{normalized_name}'.")
    if not isinstance(content_base64, str) or not content_base64:
        raise ValueError("Missing file content.")

    try:
        file_buffer = base64.b64decode(content_base64)
    except Exception as exc:
        raise ValueError("Invalid base64 file payload.") from exc

    if len(file_buffer) == 0:
        raise ValueError("File is empty.")
    if len(file_buffer) > MAX_LIBRARY_UPLOAD_BYTES:
        raise ValueError(f"File too large. Max allowed size is {MAX_LIBRARY_UPLOAD_BYTES} bytes.")

    relative_path = normalized_name if save_to_root else f"{LIBRARY_UPLOAD_SUBDIR}/{normalized_name}"
    absolute_path = _ensure_path_inside_content_root(relative_path)
    absolute_path.parent.mkdir(parents=True, exist_ok=True)

    if not overwrite and absolute_path.exists():
        raise ValueError("File already exists.")

    absolute_path.write_bytes(file_buffer)

    uid = int(uploaded_by_user_id) if str(uploaded_by_user_id or "").isdigit() else None
    _upsert_managed_library_file(relative_path, normalized_name, len(file_buffer), "uploaded", uid)
    normalized_tags = _normalize_tags(tags)
    _set_file_tags_for_path(relative_path, normalized_tags if normalized_tags else [DEFAULT_FILE_TAG])

    return {
        "path": relative_path,
        "name": normalized_name,
        "sizeBytes": len(file_buffer),
        "status": "uploaded",
        "tags": normalized_tags if normalized_tags else [DEFAULT_FILE_TAG],
    }


def _create_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, check_compatibility=False)


def _delete_points_by_source(relative_path: str) -> dict[str, Any]:
    client = _create_qdrant_client()
    source_filter = {"must": [{"key": "source", "match": {"value": relative_path}}]}

    before_count = None
    try:
        before = client.count(COLLECTION_NAME, count_filter=source_filter, exact=True)
        before_count = int(before.count)
    except Exception:
        before_count = None

    client.delete(collection_name=COLLECTION_NAME, points_selector=source_filter, wait=True)
    for _ in range(12):
        remaining = client.count(COLLECTION_NAME, count_filter=source_filter, exact=True)
        if int(remaining.count or 0) <= 0:
            return {"removedVectors": before_count}
        time.sleep(0.15)
    raise RuntimeError(f"Vector deletion verification failed for '{relative_path}'.")


def _get_managed_library_file(file_path: str) -> dict[str, Any] | None:
    rows = db_query(
        """
        SELECT file_path, original_name, source, upload_status, size_bytes, uploaded_by_user_id, uploaded_at, embedded_at, last_error, last_job_id, updated_at
        FROM library_managed_files
        WHERE file_path = %s
        """,
        [file_path],
    )
    return rows[0] if rows else None


def _hard_delete_managed_library_file(file_path: str) -> bool:
    normalized_path = str(file_path or "").strip()
    if not normalized_path:
        return False
    db_query("DELETE FROM file_tags WHERE file_path = %s", [normalized_path])
    db_query("DELETE FROM file_metadata WHERE file_path = %s", [normalized_path])
    rows = db_query("DELETE FROM library_managed_files WHERE file_path = %s RETURNING file_path", [normalized_path])
    return bool(rows)


def delete_managed_library_file_for_user(file_path: str, *, user_id: int, is_admin: bool = False) -> dict[str, Any]:
    requested_path = str(file_path or "").strip()
    if not requested_path:
        return {"deleted": False, "reason": "not_found"}

    file = _get_managed_library_file(requested_path)
    effective_path = file["file_path"] if file else requested_path

    if not is_admin:
        if not file:
            return {"deleted": False, "reason": "not_found"}
        if not str(file.get("file_path", "")).startswith(f"{LIBRARY_UPLOAD_SUBDIR}/"):
            return {"deleted": False, "reason": "not_user_managed"}
        if not user_id or int(file.get("uploaded_by_user_id") or 0) != int(user_id):
            return {"deleted": False, "reason": "not_owner"}

    known = db_query("SELECT 1 FROM file_metadata WHERE file_path = %s LIMIT 1", [effective_path])
    if not file and not known:
        return {"deleted": False, "reason": "not_found"}

    abs_path = _ensure_path_inside_content_root(effective_path)
    if abs_path.exists():
        abs_path.unlink(missing_ok=True)
    vector = _delete_points_by_source(effective_path)
    _hard_delete_managed_library_file(effective_path)
    return {"deleted": True, "path": effective_path, "removedVectors": vector.get("removedVectors")}


def list_managed_library_files(*, user_id: int, is_admin: bool = False) -> list[dict[str, Any]]:
    rows = db_query(
        """
        SELECT
          m.file_path,
          m.original_name,
          m.source,
          m.upload_status,
          m.size_bytes,
          m.uploaded_by_user_id,
          m.uploaded_at,
          m.embedded_at,
          m.last_error,
          m.last_job_id,
          m.updated_at,
          f.extension,
          f.last_modified,
          f.file_hash,
          f.chunk_count,
          f.detected_language,
          f.embedded,
          COALESCE(p.enabled, TRUE) AS enabled
        FROM library_managed_files m
        LEFT JOIN file_metadata f ON f.file_path = m.file_path
        LEFT JOIN user_library_file_preferences p
          ON p.file_path = m.file_path
         AND p.user_id = %s
        WHERE m.upload_status <> 'deleted'
        ORDER BY m.updated_at DESC, m.file_path ASC
        """,
        [int(user_id or 0)],
    )
    out = []
    for row in rows:
        path = row.get("file_path")
        out.append({
            "path": path,
            "originalName": row.get("original_name"),
            "source": row.get("source"),
            "uploadStatus": row.get("upload_status"),
            "sizeBytes": int(row.get("size_bytes") or 0),
            "uploadedAt": row.get("uploaded_at"),
            "embeddedAt": row.get("embedded_at"),
            "lastError": row.get("last_error"),
            "lastJobId": row.get("last_job_id"),
            "extension": row.get("extension"),
            "detectedLanguage": row.get("detected_language") or None,
            "lastModified": row.get("last_modified"),
            "hash": row.get("file_hash"),
            "chunkCount": row.get("chunk_count"),
            "embedded": row.get("embedded"),
            "enabled": row.get("enabled") is not False,
            "canToggle": str(path or "").startswith(f"{LIBRARY_UPLOAD_SUBDIR}/"),
            "canDelete": is_admin or int(row.get("uploaded_by_user_id") or 0) == int(user_id or 0),
            "updatedAt": row.get("updated_at"),
        })
    return out


def toggle_managed_library_file(file_path: str, enabled: bool, *, user_id: int) -> dict[str, Any]:
    file = _get_managed_library_file(file_path)
    if not file or file.get("upload_status") == "deleted":
        return {"updated": False, "reason": "not_found"}
    if not str(file.get("file_path") or "").startswith(f"{LIBRARY_UPLOAD_SUBDIR}/"):
        return {"updated": False, "reason": "not_user_toggleable"}

    rows = db_query(
        """
        INSERT INTO user_library_file_preferences (user_id, file_path, enabled, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id, file_path) DO UPDATE
          SET enabled = EXCLUDED.enabled,
              updated_at = NOW()
        RETURNING user_id, file_path, enabled, updated_at
        """,
        [int(user_id), file.get("file_path"), enabled is not False],
    )
    if not rows:
        return {"updated": False, "reason": "invalid_user_or_path"}
    updated = rows[0]
    return {"updated": True, "path": file.get("file_path"), "enabled": updated.get("enabled") is not False}
