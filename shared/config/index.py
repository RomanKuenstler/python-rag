from __future__ import annotations

import os
from pathlib import Path

COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "knowledge_base")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
CONTENT_PATH = os.getenv("CONTENT_PATH", "./data")
EMBEDDABLE_EXTENSIONS = [".md", ".txt", ".html", ".htm", ".pdf", ".epub", ".wav", ".mp3", ".m4a", ".webm"]
DEFAULT_FILE_TAG = str(os.getenv("DEFAULT_FILE_TAG", "default")).strip().lower() or "default"

HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "10"))
MAX_SIMILARITIES = int(os.getenv("MAX_SIMILARITIES", "4"))
MIN_SIMILARITIES = int(os.getenv("MIN_SIMILARITIES", "1"))
COSINE_LIMIT = float(os.getenv("COSINE_LIMIT", "0.45"))

INDEX_STATE_FILE = os.getenv("INDEX_STATE_FILE", str(Path("./.index-state.json").resolve()))
EMBEDDING_STATUS_FILE = os.getenv("EMBEDDING_STATUS_FILE", str(Path("./.embedding-status.json").resolve()))
CHAT_HISTORY_DIR = os.getenv("CHAT_HISTORY_DIR", str(Path("./chat-history").resolve()))

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "rag")
POSTGRES_USER = os.getenv("POSTGRES_USER", "rag")
AUTH_PASSWORD_SALT = os.getenv("AUTH_PASSWORD_SALT", "xzy132")

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "400"))
INDEX_SCHEMA_VERSION = os.getenv("INDEX_SCHEMA_VERSION", "1")
MAX_EMBEDDING_CHARS = int(os.getenv("MAX_EMBEDDING_CHARS", "400"))
PDF_MIN_EXTRACTED_CHARS = int(os.getenv("PDF_MIN_EXTRACTED_CHARS", "80"))

APP_NAME = "local RAG"
APP_VERSION = "v1.0.2"


def validate_retrieval_config() -> None:
    if MAX_SIMILARITIES < 1:
        raise ValueError(f"Invalid MAX_SIMILARITIES: {MAX_SIMILARITIES}. It must be an integer >= 1.")
    if MIN_SIMILARITIES < 0:
        raise ValueError(f"Invalid MIN_SIMILARITIES: {MIN_SIMILARITIES}. It must be an integer >= 0.")
    if MIN_SIMILARITIES > MAX_SIMILARITIES:
        raise ValueError(
            f"Invalid retrieval config: MIN_SIMILARITIES ({MIN_SIMILARITIES}) must be <= MAX_SIMILARITIES ({MAX_SIMILARITIES})."
        )
    if CHUNK_SIZE < 100:
        raise ValueError(f"Invalid CHUNK_SIZE: {CHUNK_SIZE}. It must be an integer >= 100.")
    if CHUNK_OVERLAP < 0:
        raise ValueError(f"Invalid CHUNK_OVERLAP: {CHUNK_OVERLAP}. It must be an integer >= 0.")
    if CHUNK_OVERLAP >= CHUNK_SIZE:
        raise ValueError(
            f"Invalid chunk config: CHUNK_OVERLAP ({CHUNK_OVERLAP}) must be smaller than CHUNK_SIZE ({CHUNK_SIZE})."
        )
    if MAX_EMBEDDING_CHARS < 200:
        raise ValueError(
            f"Invalid MAX_EMBEDDING_CHARS: {MAX_EMBEDDING_CHARS}. It must be an integer >= 200."
        )
    if PDF_MIN_EXTRACTED_CHARS < 0:
        raise ValueError(
            f"Invalid PDF_MIN_EXTRACTED_CHARS: {PDF_MIN_EXTRACTED_CHARS}. It must be an integer >= 0."
        )
