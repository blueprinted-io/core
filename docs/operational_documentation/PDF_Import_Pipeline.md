# PDF Import Pipeline

## Overview

The PDF import pipeline converts technical documentation into governed draft Tasks without requiring manual copy-paste. It is designed to work with any OpenAI-compatible LLM endpoint (local or cloud) and handles large documents gracefully by giving users explicit control over which sections are processed.

---

## Design Principles

- **No surprise AI costs.** The user explicitly selects which sections to send to the LLM. Nothing is processed automatically.
- **Non-blocking uploads.** Large PDFs are parsed in the background. The user is redirected immediately and sees a live status page.
- **Scanned documents rejected early.** If a PDF contains no extractable text, the user is told immediately — no wasted time or LLM calls.
- **Structure-aware chunking.** If the PDF has a table of contents (bookmarks), sections follow chapter boundaries. If not, the document is split by character count and the user sees a warning.
- **Graceful LLM failures.** Timeouts and errors on individual chunks are recorded and skipped — the batch continues. The user can see which sections succeeded.
- **Email-ready.** The completion hook (`notifications.py`) is wired but inert until a mail server is configured.

---

## Flow

```
1. Upload PDF
        ↓
2. Save file, compute SHA-256 hash
   (duplicate detection: same file + same user → redirect to existing job)
        ↓
3. Create ingestion record (job_status = 'chunking')
   Fire background task: parse PDF, scanned check, chunk
   Redirect immediately → /import/pdf/sections/{id}
        ↓
4. Section selection page
   - If chunking still in progress → spinner + auto-refresh every 3s
   - If failed (scanned or parse error) → error message
   - If ready → checklist of sections with title, page range, word count, preview
   User ticks the sections they want. Select all / deselect all available.
        ↓
5. User submits selection → POST /import/pdf/queue/{id}
   Selected chunks marked: selected=1, chunk_status='queued'
   Background task fires: LLM processes queued chunks one by one
   Redirect → /import/pdf/status/{id}
        ↓
6. Status page (live polling every 3s)
   - Per-section status: queued / processing / done / error / timeout
   - Progress bar
   - When complete → "Review results" button appears
   [ Email notification hook fires here — no-op until mail server configured ]
        ↓
7. Review page
   - All draft Tasks extracted by the LLM, grouped by section
   - User accepts or discards individual tasks
   - Accepted tasks committed to DB as draft Tasks, ready for submission
```

---

## Database Tables

### `ingestions`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | UUID, PK |
| `source_type` | TEXT | `'pdf'` or `'json'` |
| `source_sha256` | TEXT | For duplicate detection |
| `filename` | TEXT | Original uploaded filename |
| `created_by` | TEXT | Username |
| `created_at` | TEXT | ISO timestamp |
| `status` | TEXT | `draft` → `in_progress` → `done` |
| `job_status` | TEXT | `chunking` → `pending` → `running` → `complete` / `failed` |
| `cursor_chunk` | INTEGER | Legacy — not used in new async flow |
| `note` | TEXT | User note, or error message on failure |

### `ingestion_chunks`

| Column | Type | Notes |
|---|---|---|
| `ingestion_id` | TEXT | FK → ingestions.id |
| `chunk_index` | INTEGER | 0-based order |
| `section_title` | TEXT | From PDF bookmark, or NULL for char-count chunks |
| `pages_json` | TEXT | JSON array of page numbers in this chunk |
| `text` | TEXT | Extracted plain text |
| `selected` | INTEGER | 1 = user selected this chunk for processing |
| `chunk_status` | TEXT | `pending` / `queued` / `processing` / `done` / `error` / `timeout` / `skipped` |
| `llm_result_json` | TEXT | Raw JSON returned by LLM, or `{"error": "..."}` on failure |
| `created_at` | TEXT | ISO timestamp |

---

## Key Code Locations

| Component | File |
|---|---|
| Background chunking task | `lcs_mvp/app/routes/imports.py` → `_run_chunking_background()` |
| Background LLM task | `lcs_mvp/app/routes/imports.py` → `_run_ingestion_background()` |
| Scanned PDF detection | `lcs_mvp/app/ingestion.py` → `_pdf_is_scanned()` |
| ToC extraction | `lcs_mvp/app/ingestion.py` → `_pdf_extract_outline()` |
| Structure-aware chunking | `lcs_mvp/app/ingestion.py` → `_chunk_by_structure()` |
| LLM config (admin) | `lcs_mvp/app/routes/admin.py` → `/admin/llm` |
| Email notification stub | `lcs_mvp/app/notifications.py` → `_notify_ingestion_complete()` |
| Section selection UI | `lcs_mvp/app/templates/import_pdf_sections.html` |
| Status/progress UI | `lcs_mvp/app/templates/import_pdf_status.html` |
| Review/commit UI | `lcs_mvp/app/templates/import_pdf_review.html` |

---

## LLM Prompt

The system uses the schema defined in `docs/ai_import_prompt.md`. At runtime the prompt is injected with:

- `{per_chunk}` — max tasks per chunk (admin-configured, default 5)
- `{section_header}` — `SECTION: <title>\n\n` if a ToC title is available, otherwise empty
- `{source}` — the extracted plain text of the chunk

The system-role preamble is merged into the user message for compatibility with local LLM servers (e.g. LM Studio) that only accept `user` and `assistant` roles.

---

## LLM Configuration

All LLM settings are admin-only, stored in the `system_settings` DB table, and editable at `/admin/llm`.

| Setting | Default | Description |
|---|---|---|
| `llm_base_url` | — | Base URL of any OpenAI-compatible endpoint |
| `llm_api_key` | — | API key (leave blank for local endpoints) |
| `llm_model` | — | Model name / ID |
| `llm_timeout_seconds` | 120 | Per-chunk LLM timeout |
| `llm_max_tasks_per_chunk` | 5 | Max tasks the LLM should extract per section |
| `llm_max_chunks_per_run` | 8 | (legacy batch runner — unused in async flow) |

---

## Scanned PDF Handling

If the average extracted text per page falls below a minimum threshold, the document is classified as scanned. The ingestion record is marked `job_status='failed'` with an explanatory note, and the user sees a clear error on the sections page. No LLM calls are made.

Scanned PDFs are not supported. OCR is intentionally out of scope.

---

## Limitations & Known Edge Cases

- **Very large ToCs** (100+ chapters) produce long checklists — no pagination yet
- **PDFs with no text layer** (fully image-based) are rejected at the scanned check
- **Hybrid PDFs** (some text, some scanned pages) pass the scanned check but may have sparse chunks flagged with a "Sparse" warning in the section list
- **Character-count chunks** (no ToC) use the first line of text as the section title — quality varies
- **Re-uploads of the same file** by the same user are detected via SHA-256 and redirect to the existing job

---

## Future Work

- Email notification when background processing completes (`notifications.py`)
- Re-process individual failed/timed-out sections without re-running the whole job
- Pagination for very large section lists
- OCR support (explicitly deferred — too expensive and complex for MVP)
