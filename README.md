# Local RAG AI System

![Version](https://img.shields.io/badge/version-1.9.0-blue)
![Docker](https://img.shields.io/badge/docker-required-blue)
![Node](https://img.shields.io/badge/node.js-20+-green)
![License](https://img.shields.io/badge/license-MIT-green)
![Beginner Friendly](https://img.shields.io/badge/beginner-friendly-success)

Local, containerized Retrieval-Augmented Generation (RAG) for indexing your own files and chatting with grounded, citation-ready answers.

## What this project includes

This stack runs with Docker Compose and ships as focused services:

- `retriever` – Python-supervised API stack (frontend-facing auth/admin/library API + retriever orchestration + embedded OCR/audio workers).
- `embedder` – Python-supervised background indexing worker with co-located OCR/audio workers.
- `qdrant` – vector database for similarity search.
- `postgres` – persistence for users, sessions, chats, messages, settings, tags, and runtime metadata.
- `webui` – Vite-bundled React browser UI (Node build -> nginx runtime) with retriever API proxying.

## Quick start

```bash
docker compose up -d --build
```

### Default endpoints

- Web UI: `http://localhost:5173`
- Backend API: `http://localhost:3100`
- Retriever internal API: `http://retriever:3000`
- Embedder health endpoint (internal): `http://embedder:3200/internal/embedder/status`

### Health checks

- Retriever stack (public API): `GET /healthz`
- Embedder: `GET /internal/embedder/status`

## Current feature set

- **Session-based auth** (`/api/auth/*`) with token refresh and max-lifetime enforcement.
- **Admin user APIs** for user CRUD under `/api/admin/users`.
- **Chat lifecycle APIs** (`/api/chats`, rename/delete/download).
- **Prompt execution API** (`/api/prompt`) with RAG + assistant-mode orchestration.
- **Personalization APIs** (`/api/personalization`) for per-user behavior shaping.
- **File management APIs**:
  - Retriever-backed index view and tagging (`/api/files`, `/api/files/tags`, `/api/files/tag-filters`).
  - Managed library upload/toggle/delete/list (`/api/library/files`).
- **Prompt file attachments** (`.md`, `.txt`, `.html`, `.htm`, `.pdf`, `.csv`, OCR image formats, and audio files `.wav`, `.mp3`, `.m4a`, `.webm`).
- **OCR integration path** for both indexing-time and prompt-time extraction.
- **Library audio ingestion** for `.wav`, `.mp3`, `.m4a`, `.webm` via audio transcription prep flow.

## OCR scanner behavior

`POST /ocr/scan` supports:

- `library_pdf` (from `data/`)
- `prompt_pdf` (from `upload/` or inline base64)
- `library_image` (from `data/`)
- `prompt_image` (from `upload/` or inline base64)

Extraction behavior:

- PDF flow attempts layout-aware extraction first and falls back to OCR when quality is weak.
- Image flow runs OCR directly.
- Responses include status metadata (`status`, `extraction_details`, and `error_code` when relevant).

## Audio transcription behavior

`POST /audio/transcribe` supports:

- `chat_input` (from `upload/` using `chat_audio_relative_path` or inline `audio_base64`)
- `audio_embedding` (from `data/` using `audio_relative_path`)

Current behavior:

- Accepts `.wav`, `.mp3`, `.m4a`, and `.webm` files.
- Validates request type + payload shape and returns normalized response metadata.
- Detects spoken language and returns it as metadata (`detected_language`).
- Runs Whisper Small (`openai/whisper-small`) in translate mode to return English text for downstream embedding.

## Repository layout

```text
.
├── apps/
│   ├── backend/          # shared backend API module (run inside retriever container)
│   ├── retriever/        # retrieval stack supervisor + chat orchestration + assistant behavior
│   ├── embedder/         # embedding stack supervisor + background index worker
│   ├── ocr-scanner/      # Python OCR worker (embedded by retriever/embedder)
│   ├── audio-transcription/ # Python audio worker (embedded by retriever/embedder)
│   └── webui/            # React browser client + Vite build + nginx runtime config
├── shared/
│   ├── src/              # reusable runtime modules
│   ├── config/           # env/runtime constants
│   ├── db/               # postgres helpers
│   └── prompts/          # guardrails + assistant/persona prompt templates
├── docs/                 # project docs
├── data/                 # indexable knowledge base files
├── upload/               # temporary prompt-upload files
├── migrations/           # postgres migrations
├── compose.yml           # local orchestration
└── Dockerfile            # shared Node image used by backend/retriever/embedder
```

## Documentation map

- `docs/DOCUMENTATION.md` – architecture, runtime flow, data flow, and API surface summary.
- `docs/DEVELOPERS.md` – contributor guide: boundaries, key modules, and safe change strategy.
- `docs/CHANGELOG.md` – change history.
- `docs/PROMPTS.md` – high-level prompt design principles.
- `docs/PROMPTBUILDING.md` – implementation-level prompt assembly internals.

## Typical development checks

```bash
npm run lint
node --check apps/webui/app.js apps/webui/panel-content.js apps/webui/utils.js apps/webui/chat-export.js apps/webui/api-client.js apps/webui/app-shared.js
node --check apps/retriever/api.js apps/retriever/cli.js apps/retriever/ui.js apps/retriever/ui-helpers.js apps/embedder/worker.js apps/embedder/health-server.js
```
