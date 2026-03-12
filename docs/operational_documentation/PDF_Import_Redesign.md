# PDF Import Redesign — ToC-First, Async LLM Processing

## Status: PLANNED — not yet implemented

---

## Problem with the current flow

The existing `/import/pdf` pipeline:

1. Uploads a PDF
2. Immediately chunks the entire document (character-count based)
3. Runs the LLM over all chunks in a single synchronous pass
4. User waits the entire time, with no visibility into what's being processed

This breaks down for large structured documents (multi-hundred page manuals, operator guides) because:
- The user has no say in which sections to import
- LLM calls on irrelevant sections (appendices, glossaries, indexes) waste time and tokens
- Synchronous processing blocks the UI for potentially minutes
- Timeouts mid-batch produce confusing partial results

---

## Proposed flow

```
1. Upload PDF
       ↓
2. Scanned-PDF check (instant — if no extractable text, reject with clear message)
       ↓
3. Parse ToC + chunk by structure (no AI, instant)
   Store chunks in DB with status = "pending", selected = false
       ↓
4. Redirect to section selection page
   User sees: section title | page range | word count | text snippet
   User ticks the sections they want processed
       ↓
5. User submits selection
   Response returns immediately → redirect to job status page
   BackgroundTask fires: LLM processes selected chunks one by one,
   updating per-chunk status as it goes
       ↓
6. Status page auto-polls — shows overall progress + per-section result state
   [ Email hook: fire notification when job reaches "complete" ]
       ↓
7. User reviews accepted candidates, accepts/rejects individual tasks
       ↓
8. Commit: write accepted tasks to DB as draft
```

---

## Scanned PDF handling

Scanned PDFs (image-only, no embedded text) cannot be processed without OCR. OCR is out of scope — expensive, slow, and error-prone for the quality standard this system requires.

**Detection:** After extracting all pages, sum total characters across all pages. If the average is below **50 characters per page**, treat the document as unreadable and return an error immediately. Don't store anything, don't queue anything.

**User message:**
> "This PDF does not contain extractable text. It may be a scanned document. Please supply a text-based PDF or copy the content into a manual import."

---

## LLM prompt per chunk

Each selected chunk is sent to the LLM using the prompt defined in `docs/ai_import_prompt.md`, with the chunk's section title prepended as context:

```
SECTION: {section_title}

SOURCE DOCUMENT:
{chunk_text}
```

The LLM returns JSON matching the schema in `ai_import_prompt.md`. The response is validated and stored as `llm_result_json` on the chunk. Failed or invalid responses are marked as `error` and can be retried.

---

## Database changes

### Migrations to `ingestion_chunks`

Two new columns:

```sql
ALTER TABLE ingestion_chunks ADD COLUMN selected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ingestion_chunks ADD COLUMN chunk_status TEXT NOT NULL DEFAULT 'pending';
```

**`chunk_status` values:**
| Value | Meaning |
|---|---|
| `pending` | Created, not yet selected or processed |
| `queued` | User selected this chunk, awaiting LLM |
| `processing` | LLM call in flight |
| `done` | LLM returned valid JSON |
| `error` | LLM returned invalid/unparseable response |
| `timeout` | LLM request timed out |
| `skipped` | User deselected before processing |

### Migration to `ingestions`

One new column to track async job state:

```sql
ALTER TABLE ingestions ADD COLUMN job_status TEXT NOT NULL DEFAULT 'pending';
```

**`job_status` values:** `pending` → `running` → `complete` | `failed`

All migrations follow the existing `PRAGMA table_info()` pattern in `database.py`.

---

## Files to create / modify

### New files

| File | Purpose |
|---|---|
| `app/templates/import_pdf_sections.html` | Section selection checklist page |
| `app/templates/import_pdf_status.html` | Async job progress page (auto-polling) |
| `app/notifications.py` | Email notification stub (no-op until mail server configured) |

### Modified files

| File | Changes |
|---|---|
| `app/database.py` | Migrations for `selected`, `chunk_status`, `job_status` columns |
| `app/routes/imports.py` | Rewrite prepare/run/commit routes; add section-select and status routes; wire BackgroundTasks |
| `app/ingestion.py` | Add scanned-PDF detection helper; no structural changes needed |
| `app/templates/import_pdf.html` | Minor: update messaging to match new flow |

---

## Route map

| Method | Path | Description |
|---|---|---|
| `GET` | `/import/pdf` | Landing page — LLM status badge, previous ingestions, upload form |
| `POST` | `/import/pdf/prepare` | Upload + scanned check + chunk → redirect to section selection |
| `GET` | `/import/pdf/sections/{ingestion_id}` | Section selection checklist |
| `POST` | `/import/pdf/queue/{ingestion_id}` | Accept selected chunk IDs → queue BackgroundTask → redirect to status |
| `GET` | `/import/pdf/status/{ingestion_id}` | Job progress page (renders once; JS polls below) |
| `GET` | `/import/pdf/status/{ingestion_id}/json` | Polling endpoint — returns `{job_status, done, total, chunks:[...]}` |
| `GET` | `/import/pdf/review/{ingestion_id}` | Review accepted candidates (existing flow, kept as-is) |
| `POST` | `/import/pdf/commit` | Commit accepted tasks to DB as draft (existing flow, kept as-is) |

The old `/import/pdf/run` route is removed — its logic moves into the background task function.

---

## Section selection page (`import_pdf_sections.html`)

Each row in the checklist shows:

```
[✓] Chapter 3 — Valve Isolation Procedure    pages 42–51    ~1,200 words
    "To isolate a valve, first confirm the system is depressurised..."
```

Controls:
- **Select all / Deselect all** toggle
- Per-section checkbox
- Sections with very little text (< 200 chars) are shown dimmed with a note: "Sparse — may not yield useful tasks"
- Submit button: "Process X selected sections →"

For PDFs with no ToC/bookmarks: falls back to character-count chunks. The "title" shown is the first line of text in the chunk (truncated to 80 chars). A banner explains: "This document has no table of contents. Chunks are split by character count."

---

## Status page (`import_pdf_status.html`)

Auto-polls `/import/pdf/status/{id}/json` every 3 seconds.

Shows:
- Overall progress bar: `7 / 12 sections processed`
- Per-section status row with a coloured pill: `queued` / `processing` / `done` / `error` / `timeout`
- When job reaches `complete`: progress bar turns green, "Review results →" button appears
- On `error` job status: shows how many chunks failed with a retry option

Email hook fires at job completion (no-op currently — see `notifications.py`).

---

## Background task

```python
def _run_ingestion_background(ingestion_id: str, db_path: str) -> None:
    """Processes all queued chunks for an ingestion job.
    Runs as a FastAPI BackgroundTask — fires after response is sent.
    """
    # 1. Open DB connection using db_path directly (not the request-scoped context var)
    # 2. Set job_status = "running"
    # 3. For each chunk where selected=1 and chunk_status="queued":
    #    a. Set chunk_status = "processing"
    #    b. Call _llm_chat() with the ai_import_prompt + section text
    #    c. Parse JSON response
    #    d. Set chunk_status = "done" and llm_result_json = response
    #       OR chunk_status = "error"/"timeout" on failure
    # 4. Set job_status = "complete" (or "failed" if all chunks errored)
    # 5. Call _notify_ingestion_complete(ingestion_id, ...)  ← no-op stub
```

The background task receives the `db_path` directly (not the request context var) because it runs outside the request lifecycle. This is the same pattern used elsewhere in the app for startup tasks.

---

## `notifications.py` stub

```python
def _notify_ingestion_complete(ingestion_id: str, username: str, db_path: str) -> None:
    """Send notification when an ingestion job completes.

    Currently a no-op. Wire up when an outgoing mail server is configured.
    Expected implementation: send email to the user who queued the job
    with a link to /import/pdf/review/{ingestion_id}.
    """
    pass  # TODO: implement when mail server is available
```

---

## What is NOT changing

- The commit step (`/import/pdf/commit`) — keeps existing accept/reject review flow
- The `ai_import_prompt.md` — used verbatim as the LLM system prompt
- The deduplication / fingerprinting logic in `ingestion.py`
- JSON import (`/import/json`) — unaffected

---

## Out of scope

- OCR for scanned PDFs
- Parallel/concurrent chunk processing (sequential is fine; queue systems can be added later)
- Retry UI for individual failed chunks (can be added in a follow-up)
- Email delivery (stub is in place; implementation deferred to mail server work)
