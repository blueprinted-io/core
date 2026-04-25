from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from typing import Any

logger = logging.getLogger("blueprinted.imports")

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ..config import templates, UPLOADS_DIR, DB_PATH_CTX
from ..database import db, utc_now_iso, _workflow_domains, enforce_workflow_ref_rules, _get_llm_config, _get_system_setting, _user_domains, _active_domains, _user_id
from ..linting import _normalize_steps, _validate_steps_required
from ..audit import audit
from ..auth import require
from ..ingestion import (
    _llm_probe, _llm_chat,
    _sha256_bytes, _task_fingerprint, _near_duplicate_score,
    _pdf_extract_pages, _pdf_is_scanned, _chunk_text, _pdf_extract_outline, _chunk_by_structure,
    _llm_triage_chunk, _llm_extract_task_chunk, _extract_and_match_images,
)
from ..notifications import _notify_ingestion_complete
from ..utils import _json_dump, _json_load

router = APIRouter()


def _import_initial_status(conn) -> str:
    """Return the status new import records should receive.

    Reads the auto_submit_on_import system setting. When true, imported
    records arrive as 'submitted' (ready for review). When false (default),
    they arrive as 'draft'. 'confirmed' is never a valid import status.
    """
    val = _get_system_setting(conn, "auto_submit_on_import", "false") or "false"
    return "submitted" if val == "true" else "draft"


@router.get("/_llm/status")
def llm_status(request: Request):
    require(request.state.role, "import:pdf")
    with db() as conn:
        cfg = _get_llm_config(conn)
    probe = _llm_probe(cfg["llm_base_url"], cfg["llm_api_key"])
    model = cfg.get("llm_model") or ""
    return {"ok": bool(probe.get("ok")), "detail": str(probe.get("detail")), "model": model}


# ---------------------------------------------------------------------------
# PDF import — landing page
# ---------------------------------------------------------------------------

@router.get("/import/pdf", response_class=HTMLResponse)
def import_pdf_form(request: Request):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        rows = conn.execute(
            "SELECT id, filename, created_at, status, job_status FROM ingestions "
            "WHERE source_type='pdf' AND created_by=? ORDER BY created_at DESC LIMIT 50",
            (actor,),
        ).fetchall()

        ingestions = []
        done_statuses = ("'done'", "'error'", "'timeout'", "'skipped'", "'merged'")
        for r in rows:
            ing = dict(r)
            counts = conn.execute(
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN chunk_status IN ({','.join(done_statuses)}) THEN 1 ELSE 0 END) AS done "
                f"FROM ingestion_chunks WHERE ingestion_id=?",
                (r["id"],),
            ).fetchone()
            ing["total_chunks"] = counts["total"] or 0
            ing["done_chunks"] = counts["done"] or 0
            ingestions.append(ing)

    return templates.TemplateResponse(
        request,
        "import_pdf.html",
        {"ingestions": ingestions},
    )


# ---------------------------------------------------------------------------
# PDF import — step 1: upload, hash, create record, fire chunking background task
# ---------------------------------------------------------------------------

def _run_chunking_background(ingestion_id: str, out_path: str, db_path: str) -> None:
    """Background task: parse PDF, scanned check, chunk, store results."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        pages = _pdf_extract_pages(out_path)

        if _pdf_is_scanned(pages):
            conn.execute(
                "UPDATE ingestions SET job_status='failed', note=? WHERE id=?",
                ("This PDF does not contain extractable text — it may be a scanned document. Please supply a text-based PDF.", ingestion_id),
            )
            conn.commit()
            return

        outline = _pdf_extract_outline(out_path)
        chunks = _chunk_by_structure(pages, outline) if outline else _chunk_text(pages, max_chars=12000)

        now = utc_now_iso()
        for idx, ch in enumerate(chunks):
            conn.execute(
                "INSERT OR REPLACE INTO ingestion_chunks"
                "(ingestion_id, chunk_index, pages_json, text, llm_result_json, created_at, section_title, selected, chunk_status, section_level) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ingestion_id, idx,
                    json.dumps(ch.get("pages", [])),
                    ch.get("text", ""),
                    None, now,
                    ch.get("section_title") or None,
                    0, "pending",
                    int(ch.get("section_level", 0)),
                ),
            )
            conn.commit()  # commit per-chunk so write lock isn't held across the whole parse
        conn.execute(
            "UPDATE ingestions SET job_status='pending' WHERE id=?",
            (ingestion_id,),
        )
        conn.commit()
    except Exception as e:
        try:
            conn.execute(
                "UPDATE ingestions SET job_status='failed', note=? WHERE id=?",
                (f"Document parsing failed: {e}", ingestion_id),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


@router.post("/import/pdf/prepare")
def import_pdf_prepare(
    request: Request,
    background_tasks: BackgroundTasks,
    pdf: UploadFile = File(...),
    actor_note: str = Form("Imported from PDF"),
):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    # Save upload and compute hash — this is the only synchronous work
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", pdf.filename or "upload.pdf")
    file_id = str(uuid.uuid4())
    out_path = os.path.join(UPLOADS_DIR, f"{file_id}__{safe_name}")
    file_bytes = pdf.file.read()
    with open(out_path, "wb") as f:
        f.write(file_bytes)
    sha = _sha256_bytes(file_bytes)

    db_path = DB_PATH_CTX.get()

    with db() as conn:
        existing = conn.execute(
            "SELECT id, job_status FROM ingestions WHERE source_type='pdf' AND source_sha256=? AND created_by=? ORDER BY created_at DESC LIMIT 1",
            (sha, actor),
        ).fetchone()

        if existing:
            ingestion_id = str(existing["id"])
            job_status = existing["job_status"]
            if job_status in ("complete", "partial"):
                return RedirectResponse(url=f"/import/pdf/review/{ingestion_id}", status_code=303)
            if job_status == "running":
                return RedirectResponse(url=f"/import/pdf/status/{ingestion_id}", status_code=303)
            # Already chunked (pending/chunking) — go to sections
            has_chunks = conn.execute(
                "SELECT 1 FROM ingestion_chunks WHERE ingestion_id=? LIMIT 1", (ingestion_id,)
            ).fetchone()
            if has_chunks:
                return RedirectResponse(url=f"/import/pdf/sections/{ingestion_id}", status_code=303)
            # Otherwise fall through and re-fire chunking
        else:
            ingestion_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO ingestions(id, source_type, source_sha256, filename, file_path, created_by, created_at, status, cursor_chunk, max_tasks_per_run, note, job_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ingestion_id, "pdf", sha, safe_name, out_path, actor, utc_now_iso(), "draft", 0, 5, actor_note.strip() or "Imported from PDF", "chunking"),
            )

        conn.execute(
            "UPDATE ingestions SET job_status='chunking' WHERE id=?", (ingestion_id,)
        )

    background_tasks.add_task(_run_chunking_background, ingestion_id, out_path, db_path)
    return RedirectResponse(url=f"/import/pdf/sections/{ingestion_id}", status_code=303)


# ---------------------------------------------------------------------------
# PDF import — step 2: section selection checklist
# ---------------------------------------------------------------------------

@router.get("/import/pdf/sections/{ingestion_id}", response_class=HTMLResponse)
def import_pdf_sections(request: Request, ingestion_id: str, mode: str = Query("")):
    require(request.state.role, "import:pdf")
    actor = request.state.user
    is_resume = mode == "resume"

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        # If already queued/running/complete, redirect to the appropriate page
        job_status = ing["job_status"]
        if job_status in ("triaging", "triaged"):
            return RedirectResponse(url=f"/import/pdf/triage/{ingestion_id}", status_code=303)
        if job_status == "running":
            return RedirectResponse(url=f"/import/pdf/status/{ingestion_id}", status_code=303)
        if job_status in ("complete", "partial") and not is_resume:
            return RedirectResponse(url=f"/import/pdf/review/{ingestion_id}", status_code=303)

        chunks = conn.execute(
            "SELECT chunk_index, pages_json, text, section_title, selected, chunk_status, section_level "
            "FROM ingestion_chunks WHERE ingestion_id=? ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

        from ..database import _get_app_settings
        show_select_all = _get_app_settings(conn).get("import_select_all", False)

    has_toc = any((r["section_title"] or "").strip() for r in chunks)

    sections = []
    for r in chunks:
        text = (r["text"] or "").strip()
        word_count = len(text.split())
        pages = _json_load(r["pages_json"]) or []
        page_label = f"p.{pages[0]}" if len(pages) == 1 else (f"pp.{pages[0]}–{pages[-1]}" if pages else "")
        title = (r["section_title"] or "").strip()
        if not title:
            # Use first non-empty line as a fallback label
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            title = (first_line[:80] + "…") if len(first_line) > 80 else first_line or f"Chunk {r['chunk_index'] + 1}"
        preview = text[:200].replace("\n", " ").strip()
        if len(text) > 200:
            preview += "…"
        chunk_status = r["chunk_status"] or "pending"
        if is_resume:
            # In resume mode: retry errors by default, leave done/pending unchecked
            default_selected = chunk_status in ("error", "timeout")
        else:
            default_selected = bool(r["selected"])
        sections.append({
            "chunk_index": r["chunk_index"],
            "title": title,
            "page_label": page_label,
            "word_count": word_count,
            "preview": preview,
            "sparse": word_count < 40,
            "selected": default_selected,
            "level": int(r["section_level"] or 0),
            "chunk_status": chunk_status,
        })

    # Mark which rows are groups (have children in the TOC hierarchy)
    for i, s in enumerate(sections):
        next_level = sections[i + 1]["level"] if i + 1 < len(sections) else -1
        s["is_group"] = next_level > s["level"]

    # Max depth — tells template how deep the tree goes
    max_level = max((s["level"] for s in sections), default=0)

    return templates.TemplateResponse(
        request,
        "import_pdf_sections.html",
        {
            "ing": dict(ing),
            "sections": sections,
            "has_toc": has_toc,
            "max_level": max_level,
            "ingestion_id": ingestion_id,
            "job_status": job_status,
            "is_resume": is_resume,
            "show_select_all": show_select_all,
        },
    )


# ---------------------------------------------------------------------------
# PDF import — step 3: triage background task + queue/extraction
# ---------------------------------------------------------------------------

def _load_llm_cfg_from_conn(conn) -> dict[str, Any]:
    cfg_rows = conn.execute("SELECT key, value FROM system_settings WHERE key LIKE 'llm_%'").fetchall()
    raw_cfg = {r["key"]: r["value"] for r in cfg_rows}
    return {
        "llm_base_url": raw_cfg.get("llm_base_url", ""),
        "llm_api_key": raw_cfg.get("llm_api_key", ""),
        "llm_model": raw_cfg.get("llm_model", ""),
        "llm_max_tokens": int(raw_cfg.get("llm_max_tokens", 2000)),
        "llm_temperature": float(raw_cfg.get("llm_temperature", 0.2)),
        "llm_timeout_seconds": float(raw_cfg.get("llm_timeout_seconds", 120)),
        "llm_max_tasks_per_chunk": int(raw_cfg.get("llm_max_tasks_per_chunk", 5)),
    }


def _run_triage_background(ingestion_id: str, db_path: str, username: str) -> None:
    """Background task: LLM-classifies each selected chunk as task/workflow/ignore."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        cfg = _load_llm_cfg_from_conn(conn)
        conn.execute("UPDATE ingestions SET job_status='triaging' WHERE id=?", (ingestion_id,))
        conn.commit()

        chunks = conn.execute(
            "SELECT chunk_index, text, section_title FROM ingestion_chunks "
            "WHERE ingestion_id=? AND selected=1 ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

        logger.info("Triage started ingestion=%s chunks=%d user=%s", ingestion_id[:8], len(chunks), username)
        for cr in chunks:
            result = _llm_triage_chunk(
                cr["text"] or "",
                (cr["section_title"] or "").strip(),
                cfg,
            )
            conn.execute(
                "UPDATE ingestion_chunks SET chunk_type=?, triage_confidence=?, triage_reason=? "
                "WHERE ingestion_id=? AND chunk_index=?",
                (result["type"], result["confidence"], result["reason"], ingestion_id, int(cr["chunk_index"])),
            )
            conn.commit()

        conn.execute("UPDATE ingestions SET job_status='triaged' WHERE id=?", (ingestion_id,))
        conn.commit()
        logger.info("Triage complete ingestion=%s", ingestion_id[:8])
    except Exception as e:
        logger.exception("Triage failed ingestion=%s", ingestion_id[:8])
        try:
            conn.execute(
                "UPDATE ingestions SET job_status='failed', note=? WHERE id=?",
                (f"Triage failed: {e}", ingestion_id),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _group_chunks_by_hierarchy(chunks: list, split_groups: set[int] | None = None) -> list[tuple]:
    """Group consecutive chunks into (lead, [children]) pairs based on section_level.

    A chunk is a child of the immediately preceding chunk at a lower section_level.
    All descendants are absorbed: children of children are included in the same group.
    Chunks at the same level as the lead start a new group.

    If a lead's chunk_index is in split_groups, it is not merged with its children —
    the lead and each child are each returned as independent (chunk, []) groups.
    """
    split_groups = split_groups or set()
    groups: list[tuple] = []
    i = 0
    while i < len(chunks):
        lead = chunks[i]
        lead_level = int(lead["section_level"] or 0)
        lead_idx = int(lead["chunk_index"])
        children = []
        j = i + 1
        while j < len(chunks):
            child_level = int(chunks[j]["section_level"] or 0)
            if child_level > lead_level:
                children.append(chunks[j])
                j += 1
            else:
                break
        if lead_idx in split_groups and children:
            # User chose to split: each section becomes its own task
            groups.append((lead, []))
            for child in children:
                groups.append((child, []))
        else:
            groups.append((lead, children))
        i = j  # skip past any children
    return groups


def _merge_chunk_texts(lead, children: list) -> str:
    """Concatenate lead + child texts with section headers for LLM context."""
    parts = []
    lead_title = (lead["section_title"] or "").strip()
    lead_text = (lead["text"] or "").strip()
    parts.append(f"# {lead_title}\n\n{lead_text}" if lead_title else lead_text)
    for child in children:
        child_title = (child["section_title"] or "").strip()
        child_text = (child["text"] or "").strip()
        parts.append(f"## {child_title}\n\n{child_text}" if child_title else child_text)
    return "\n\n---\n\n".join(p for p in parts if p)


def _run_ingestion_background(ingestion_id: str, db_path: str, username: str, split_groups: set[int] | None = None) -> None:
    """Background task: LLM-processes all queued chunks using type-aware schema 1.0 prompts."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")

    try:
        cfg = _load_llm_cfg_from_conn(conn)

        conn.execute(
            "UPDATE ingestions SET job_status='running', status='in_progress' WHERE id=?",
            (ingestion_id,),
        )
        conn.commit()

        chunks = conn.execute(
            "SELECT chunk_index, pages_json, text, section_title, chunk_type, section_level FROM ingestion_chunks "
            "WHERE ingestion_id=? AND selected=1 AND chunk_status='queued' ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

        groups = _group_chunks_by_hierarchy(list(chunks), split_groups=split_groups)
        logger.info(
            "Extraction started ingestion=%s chunks=%d groups=%d user=%s",
            ingestion_id[:8], len(chunks), len(groups), username,
        )
        done = errors = 0
        for lead, children in groups:
            chunk_index = int(lead["chunk_index"])
            section_title = (lead["section_title"] or "").strip()

            # Mark lead as processing
            conn.execute(
                "UPDATE ingestion_chunks SET chunk_status='processing' WHERE ingestion_id=? AND chunk_index=?",
                (ingestion_id, chunk_index),
            )
            conn.commit()

            # Build merged text and page list when children are present
            if children:
                extract_text = _merge_chunk_texts(lead, children)
                merged_pages = sorted(set(
                    p for row in [lead] + children
                    for p in (_json_load(row["pages_json"]) or [])
                    if isinstance(p, int)
                ))
                child_indices = [int(c["chunk_index"]) for c in children]
                logger.info(
                    "Merging %d child chunk(s) into chunk %d (ingestion=%s)",
                    len(children), chunk_index, ingestion_id[:8],
                )
            else:
                extract_text = lead["text"] or ""
                merged_pages = None
                child_indices = []

            try:
                data = _llm_extract_task_chunk(extract_text, section_title, cfg)

                # Attach section page ranges so image extraction at commit time can
                # use page number → section title → step matching instead of OCR heuristics
                if children:
                    data["_section_ranges"] = [
                        {"title": (lead["section_title"] or "").strip(), "pages": _json_load(lead["pages_json"]) or []},
                        *[{"title": (c["section_title"] or "").strip(), "pages": _json_load(c["pages_json"]) or []} for c in children],
                    ]

                update_pages = json.dumps(merged_pages) if merged_pages is not None else lead["pages_json"]
                conn.execute(
                    "UPDATE ingestion_chunks SET chunk_status='done', llm_result_json=?, pages_json=? "
                    "WHERE ingestion_id=? AND chunk_index=?",
                    (json.dumps(data), update_pages, ingestion_id, chunk_index),
                )
                # Mark children as merged so they don't appear as separate candidates
                for ci in child_indices:
                    conn.execute(
                        "UPDATE ingestion_chunks SET chunk_status='merged' "
                        "WHERE ingestion_id=? AND chunk_index=?",
                        (ingestion_id, ci),
                    )
                done += 1
            except HTTPException as e:
                status = "timeout" if e.status_code == 504 else "error"
                logger.error("Chunk %d extraction %s ingestion=%s: %s", chunk_index, status, ingestion_id[:8], e.detail)
                conn.execute(
                    "UPDATE ingestion_chunks SET chunk_status=?, llm_result_json=? "
                    "WHERE ingestion_id=? AND chunk_index=?",
                    (status, json.dumps({"error": str(e.detail)}), ingestion_id, chunk_index),
                )
                errors += 1
            except Exception as e:
                logger.exception("Chunk %d extraction error ingestion=%s", chunk_index, ingestion_id[:8])
                conn.execute(
                    "UPDATE ingestion_chunks SET chunk_status='error', llm_result_json=? "
                    "WHERE ingestion_id=? AND chunk_index=?",
                    (json.dumps({"error": str(e)}), ingestion_id, chunk_index),
                )
                errors += 1
            conn.commit()

        unprocessed = conn.execute(
            "SELECT COUNT(*) AS n FROM ingestion_chunks "
            "WHERE ingestion_id=? AND selected=1 "
            "AND chunk_status NOT IN ('done','error','timeout','skipped','merged')",
            (ingestion_id,),
        ).fetchone()["n"]
        final_status = "complete" if unprocessed == 0 else "partial"
        conn.execute(
            "UPDATE ingestions SET job_status=?, status='done' WHERE id=?",
            (final_status, ingestion_id),
        )
        conn.commit()
        logger.info("Extraction %s ingestion=%s done=%d errors=%d", final_status, ingestion_id[:8], done, errors)
        _notify_ingestion_complete(ingestion_id, username, db_path)

    except Exception:
        logger.exception("Extraction job failed ingestion=%s", ingestion_id[:8])
        try:
            conn.execute(
                "UPDATE ingestions SET job_status='failed' WHERE id=?",
                (ingestion_id,),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


@router.post("/import/pdf/triage/{ingestion_id}")
def import_pdf_triage_queue(
    request: Request,
    ingestion_id: str,
    background_tasks: BackgroundTasks,
    chunk_index: list[int] = Form([]),
):
    """Accept selected sections, fire triage background task."""
    require(request.state.role, "import:pdf")
    actor = request.state.user

    if not chunk_index:
        return RedirectResponse(url=f"/import/pdf/sections/{ingestion_id}?error=none_selected", status_code=303)

    db_path = DB_PATH_CTX.get()

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        conn.execute(
            "UPDATE ingestion_chunks SET selected=0, chunk_status='pending' WHERE ingestion_id=?",
            (ingestion_id,),
        )
        for idx in chunk_index:
            conn.execute(
                "UPDATE ingestion_chunks SET selected=1, chunk_status='queued' WHERE ingestion_id=? AND chunk_index=?",
                (ingestion_id, idx),
            )
        conn.execute("UPDATE ingestions SET job_status='triaging' WHERE id=?", (ingestion_id,))

    background_tasks.add_task(_run_triage_background, ingestion_id, db_path, actor)
    return RedirectResponse(url=f"/import/pdf/triage/{ingestion_id}", status_code=303)


@router.get("/import/pdf/triage/{ingestion_id}", response_class=HTMLResponse)
def import_pdf_triage_review(request: Request, ingestion_id: str):
    """Show triage results (spinner while running, review table when done)."""
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        job_status = ing["job_status"]

        if job_status == "running":
            return RedirectResponse(url=f"/import/pdf/status/{ingestion_id}", status_code=303)
        if job_status in ("complete", "partial"):
            return RedirectResponse(url=f"/import/pdf/review/{ingestion_id}", status_code=303)

        chunks = conn.execute(
            "SELECT chunk_index, pages_json, text, section_title, section_level, "
            "chunk_type, triage_confidence, triage_reason "
            "FROM ingestion_chunks WHERE ingestion_id=? AND selected=1 ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

        # Fetch user's domain entitlements for the domain picker
        uid = _user_id(conn, actor)
        if request.state.role == "admin":
            domains = _active_domains(conn)
        else:
            domains = _user_domains(conn, actor)

    # Build section list and annotate each child with will_merge=True
    raw_sections = []
    for r in chunks:
        text = (r["text"] or "").strip()
        word_count = len(text.split())
        pages = _json_load(r["pages_json"]) or []
        page_label = f"p.{pages[0]}" if len(pages) == 1 else (f"pp.{pages[0]}–{pages[-1]}" if pages else "")
        title = (r["section_title"] or f"Chunk {r['chunk_index'] + 1}").strip()
        conf = r["triage_confidence"]
        raw_sections.append({
            "chunk_index": r["chunk_index"],
            "title": title,
            "page_label": page_label,
            "word_count": word_count,
            "chunk_type": r["chunk_type"] or None,
            "confidence": round(float(conf) * 100) if conf is not None else None,
            "reason": r["triage_reason"] or "",
            "level": int(r["section_level"] or 0),
            "will_merge": False,
        })

    # Mark child sections that will be merged into their parent at extraction time
    groups = _group_chunks_by_hierarchy(list(chunks))
    merged_indices = {int(c["chunk_index"]) for _, children in groups for c in children}
    parent_of: dict[int, str] = {}
    parent_idx_of: dict[int, int] = {}
    parent_child_count: dict[int, int] = {}
    for lead, children in groups:
        if children:
            parent_child_count[int(lead["chunk_index"])] = len(children)
        for c in children:
            parent_of[int(c["chunk_index"])] = (lead["section_title"] or "").strip()
            parent_idx_of[int(c["chunk_index"])] = int(lead["chunk_index"])
    sections = []
    for s in raw_sections:
        s["will_merge"] = s["chunk_index"] in merged_indices
        s["merge_into"] = parent_of.get(s["chunk_index"], "")
        s["merge_into_idx"] = parent_idx_of.get(s["chunk_index"], -1)
        s["is_merge_parent"] = s["chunk_index"] in parent_child_count
        s["child_count"] = parent_child_count.get(s["chunk_index"], 0)
        sections.append(s)

    return templates.TemplateResponse(
        request,
        "import_pdf_triage.html",
        {
            "ing": dict(ing),
            "ingestion_id": ingestion_id,
            "job_status": job_status,
            "is_loading": job_status == "triaging",
            "sections": sections,
            "domains": domains,
        },
    )


@router.post("/import/pdf/queue/{ingestion_id}")
def import_pdf_queue(
    request: Request,
    ingestion_id: str,
    background_tasks: BackgroundTasks,
    chunk_index: list[int] = Form([]),
    chunk_type: list[str] = Form([]),
    domain: str = Form(""),
    split_group: list[int] = Form([]),
):
    """Accept triage overrides + domain, fire extraction background task."""
    require(request.state.role, "import:pdf")
    actor = request.state.user

    if not chunk_index:
        return RedirectResponse(url=f"/import/pdf/triage/{ingestion_id}?error=none_selected", status_code=303)

    db_path = DB_PATH_CTX.get()

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        # Deselect all; then apply submitted selections with their (possibly overridden) types
        conn.execute(
            "UPDATE ingestion_chunks SET selected=0, chunk_status='pending' WHERE ingestion_id=?",
            (ingestion_id,),
        )
        type_map = dict(zip(chunk_index, chunk_type))
        for idx in chunk_index:
            ct = (type_map.get(idx) or "task").strip().lower()
            if ct not in ("task", "workflow"):
                ct = "task"
            conn.execute(
                "UPDATE ingestion_chunks SET selected=1, chunk_status='queued', chunk_type=? "
                "WHERE ingestion_id=? AND chunk_index=?",
                (ct, ingestion_id, idx),
            )
        conn.execute(
            "UPDATE ingestions SET job_status='pending', domain=? WHERE id=?",
            ((domain or "").strip(), ingestion_id),
        )

    background_tasks.add_task(_run_ingestion_background, ingestion_id, db_path, actor, set(split_group))
    return RedirectResponse(url=f"/import/pdf/status/{ingestion_id}", status_code=303)


# ---------------------------------------------------------------------------
# PDF import — step 4: status / progress page + polling endpoint
# ---------------------------------------------------------------------------

@router.get("/import/pdf/status/{ingestion_id}", response_class=HTMLResponse)
def import_pdf_status_page(request: Request, ingestion_id: str):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

    return templates.TemplateResponse(
        request,
        "import_pdf_status.html",
        {"ing": dict(ing), "ingestion_id": ingestion_id},
    )


@router.get("/import/pdf/status/{ingestion_id}/json")
def import_pdf_status_json(request: Request, ingestion_id: str):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        chunks = conn.execute(
            "SELECT chunk_index, section_title, chunk_status, pages_json, llm_result_json "
            "FROM ingestion_chunks WHERE ingestion_id=? AND selected=1 ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

    total = len(chunks)
    done_statuses = {"done", "error", "timeout", "skipped", "merged"}
    done = sum(1 for c in chunks if c["chunk_status"] in done_statuses)

    def _chunk_error(c) -> str | None:
        """Extract human-readable error string from llm_result_json, if any."""
        raw = c["llm_result_json"]
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return str(parsed.get("error")) if "error" in parsed else None
        except Exception:
            return None

    return JSONResponse({
        "job_status": ing["job_status"],
        "filename": ing["filename"],
        "total": total,
        "done": done,
        "chunks": [
            {
                "chunk_index": c["chunk_index"],
                "title": (c["section_title"] or f"Chunk {c['chunk_index'] + 1}").strip(),
                "status": c["chunk_status"],
                "pages": _json_load(c["pages_json"]) or [],
                "error": _chunk_error(c),
            }
            for c in chunks
        ],
    })


# ---------------------------------------------------------------------------
# PDF import — delete ingestion + uploaded file
# ---------------------------------------------------------------------------

@router.post("/import/pdf/delete/{ingestion_id}")
def import_pdf_delete(request: Request, ingestion_id: str):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        ing = conn.execute(
            "SELECT file_path, job_status FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)
        if ing["job_status"] in ("running", "chunking", "triaging"):
            raise HTTPException(status_code=409, detail="Cannot delete while processing is in progress.")

        file_path = ing["file_path"] or ""
        # chunks are deleted via ON DELETE CASCADE from ingestion_chunks FK
        conn.execute("DELETE FROM ingestions WHERE id=?", (ingestion_id,))

    if file_path and os.path.isfile(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass  # best-effort; record is already gone

    return RedirectResponse(url="/import/pdf", status_code=303)


@router.get("/import/pdf/{ingestion_id}/download")
def import_pdf_download(request: Request, ingestion_id: str):
    require(request.state.role, "import:pdf")
    actor = request.state.user
    with db() as conn:
        ing = conn.execute(
            "SELECT file_path, filename FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
    if not ing:
        raise HTTPException(404)
    file_path = ing["file_path"] or ""
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File no longer available.")
    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=ing["filename"] or "document.pdf",
    )


# ---------------------------------------------------------------------------
# PDF import — debug: raw chunk data (admin only)
# ---------------------------------------------------------------------------

@router.get("/import/pdf/{ingestion_id}/debug")
def import_pdf_debug(request: Request, ingestion_id: str):
    from fastapi.responses import JSONResponse
    if request.state.role != "admin":
        raise HTTPException(403)
    actor = request.state.user
    with db() as conn:
        ing = conn.execute("SELECT * FROM ingestions WHERE id=?", (ingestion_id,)).fetchone()
        if not ing:
            raise HTTPException(404)
        rows = conn.execute(
            "SELECT chunk_index, section_title, chunk_status, chunk_type, selected, "
            "triage_confidence, triage_reason, llm_result_json "
            "FROM ingestion_chunks WHERE ingestion_id=? ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()
    chunks = []
    for r in rows:
        raw = r["llm_result_json"]
        parsed = None
        parse_error = None
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception as e:
                parse_error = str(e)
        chunks.append({
            "chunk_index": r["chunk_index"],
            "section_title": r["section_title"],
            "chunk_status": r["chunk_status"],
            "chunk_type": r["chunk_type"],
            "selected": r["selected"],
            "triage_confidence": r["triage_confidence"],
            "triage_reason": r["triage_reason"],
            "llm_result_raw_len": len(raw) if raw else 0,
            "llm_result_parsed": parsed,
            "parse_error": parse_error,
        })
    return JSONResponse({"ingestion_id": ingestion_id, "job_status": ing["job_status"], "chunks": chunks})


# ---------------------------------------------------------------------------
# PDF import — step 5: review candidates and commit
# ---------------------------------------------------------------------------

@router.get("/import/pdf/review/{ingestion_id}", response_class=HTMLResponse)
def import_pdf_review(request: Request, ingestion_id: str):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        chunk_rows = conn.execute(
            "SELECT chunk_index, pages_json, text, llm_result_json, section_title, chunk_status, chunk_type "
            "FROM ingestion_chunks WHERE ingestion_id=? AND selected=1 AND chunk_status='done' ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

        errored = conn.execute(
            "SELECT COUNT(*) AS n FROM ingestion_chunks WHERE ingestion_id=? AND selected=1 AND chunk_status IN ('error','timeout')",
            (ingestion_id,),
        ).fetchone()["n"]

        # Build existing task signatures for dedupe
        latest_rows = conn.execute(
            "SELECT record_id, MAX(version) AS v FROM tasks GROUP BY record_id"
        ).fetchall()
        existing_tasks: list[dict[str, Any]] = []
        for r in latest_rows:
            row = conn.execute(
                "SELECT title, outcome, steps_json FROM tasks WHERE record_id=? AND version=?",
                (r["record_id"], int(r["v"])),
            ).fetchone()
            if row:
                existing_tasks.append({
                    "record_id": r["record_id"],
                    "title": row["title"],
                    "outcome": row["outcome"],
                    "steps": _json_load(row["steps_json"]) or [],
                })

    candidates: list[dict[str, Any]] = []

    for cr in chunk_rows:
        if not cr["llm_result_json"]:
            continue
        try:
            data = json.loads(cr["llm_result_json"])
        except Exception:
            continue
        tasks = data.get("tasks") if isinstance(data, dict) else []
        if not isinstance(tasks, list):
            tasks = []

        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title", "")).strip()
            if not title:
                continue
            candidates.append({"chunk_index": int(cr["chunk_index"]), "pages": _json_load(cr["pages_json"]) or [], "task": t})

    # Dedupe within candidates by fingerprint
    out: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    for c in candidates:
        fp = _task_fingerprint(c["task"])
        if fp not in seen_fp:
            seen_fp.add(fp)
            out.append(c)

    # Attach dup flags
    flagged: list[dict[str, Any]] = []
    for c in out:
        t = c["task"]
        fp = _task_fingerprint(t)
        near_matches: list[dict[str, Any]] = []
        for ex in existing_tasks:
            ex_fp = _task_fingerprint(ex)
            if ex_fp == fp:
                near_matches.append({"record_id": ex["record_id"], "kind": "exact", "score": 1.0})
                continue
            score = _near_duplicate_score(t, ex)
            if score >= 0.72:
                near_matches.append({"record_id": ex["record_id"], "kind": "near", "score": round(score, 3)})
        near_matches = sorted(near_matches, key=lambda x: x["score"], reverse=True)[:3]
        flagged.append({
            "id": _sha256_bytes((fp + str(c["chunk_index"])).encode("utf-8"))[:16],
            "title": str(t.get("title", "")).strip(),
            "chunk_index": c["chunk_index"],
            "pages": c["pages"],
            "dup_matches": near_matches,
            "task": t,
        })

    skipped_note = f"{errored} section(s) could not be processed (timeout or error)." if errored else ""

    return templates.TemplateResponse(
        request,
        "import_pdf_preview.html",
        {
            "ingestion": dict(ing),
            "candidates": flagged,
            "error": None,
            "skipped_note": skipped_note,
            "done": True,
        },
    )


def _commit_schema10_payload(
    conn,
    chunk_rows,
    candidate_id: list[str],
    ingestion_id: str,
    filename: str,
    domain: str,
    actor: str,
    pdf_path: str = "",
) -> int:
    """Assemble and commit a schema 1.0 payload from done chunks.

    Merges task lists across chunks and inserts them as draft tasks.
    Returns tasks_created count.
    """
    now = utc_now_iso()
    initial_status = _import_initial_status(conn)

    # Collect selected tasks from all chunks, preserving chunk order
    all_task_items: list[dict[str, Any]] = []

    seen_fp: set[str] = set()
    for cr in chunk_rows:
        if not cr["llm_result_json"]:
            continue
        try:
            data = json.loads(cr["llm_result_json"])
        except Exception:
            continue
        tasks = data.get("tasks") if isinstance(data, dict) else []
        if not isinstance(tasks, list):
            tasks = []

        chunk_idx = int(cr["chunk_index"])
        pages = _json_load(cr["pages_json"]) or []
        section_ranges = data.get("_section_ranges") if isinstance(data, dict) else None

        for t in tasks:
            if not isinstance(t, dict):
                continue
            fp = _task_fingerprint(t)
            cid = _sha256_bytes((fp + str(chunk_idx)).encode("utf-8"))[:16]
            if cid not in candidate_id:
                continue
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            all_task_items.append({
                "task": t,
                "pages": pages,
                "chunk_index": chunk_idx,
                "section_ranges": section_ranges,
            })

    task_records: list[dict[str, Any]] = []
    for item in all_task_items:
        record_id = str(uuid.uuid4())
        task_records.append({**item, "record_id": record_id})

    # Insert tasks
    tasks_created = 0
    for item in task_records:
        t = item["task"]
        title = str(t.get("title", "")).strip()
        outcome = str(t.get("outcome", "")).strip()
        procedure_name = str(t.get("procedure_name", "")).strip() or title
        facts = t.get("facts") or []
        concepts = t.get("concepts") or []
        deps = t.get("dependencies") or []
        steps = t.get("steps") or []
        steps_norm = _normalize_steps(steps)
        if not steps_norm:
            logger.warning(
                "Skipping task '%s' during commit (ingestion=%s): no steps extracted",
                title[:80], ingestion_id[:8],
            )
            continue

        if "irreversible" in t:
            irrev = 1 if t["irreversible"] else 0
        else:
            irrev = 1 if bool(t.get("irreversible_flag")) else 0

        assets = [{
            "url": f"ingestion:{ingestion_id}",
            "type": "link",
            "label": f"source_pdf:{filename} pages:{item['pages']}",
        }]
        if pdf_path:
            page_nums = [p for p in (item["pages"] if isinstance(item["pages"], list) else []) if isinstance(p, int)]
            steps_norm, img_assets = _extract_and_match_images(
                pdf_path, page_nums, steps_norm, item["record_id"],
                section_ranges=item.get("section_ranges"),
            )
            assets.extend(img_assets)

        conn.execute(
            """INSERT INTO tasks(
              record_id, version, status,
              title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
              irreversible_flag, task_assets_json, domain, software_name, software_version,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item["record_id"], 1, initial_status,
                title, outcome,
                _json_dump([str(x) for x in facts]),
                _json_dump([str(x) for x in concepts]),
                procedure_name,
                _json_dump(steps_norm),
                _json_dump([str(x) for x in deps]),
                irrev,
                _json_dump(assets),
                domain,
                t.get("software_name") or None,
                t.get("software_version") or None,
                now, now, actor, actor,
                None, None,
                f"import:pdf ingestion={ingestion_id}",
                1, "AI-imported: check for duplicates and correctness",
            ),
        )
        audit("task", item["record_id"], 1, "create", actor, note="import:pdf", conn=conn)
        tasks_created += 1

    return tasks_created


@router.post("/import/pdf/commit")
def import_pdf_commit(
    request: Request,
    ingestion_id: str = Form(...),
    candidate_id: list[str] = Form([]),
):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        ing = conn.execute("SELECT * FROM ingestions WHERE id=? AND created_by=?", (ingestion_id, actor)).fetchone()
        if not ing:
            raise HTTPException(404)

        chunk_rows = conn.execute(
            "SELECT chunk_index, pages_json, text, llm_result_json, chunk_type FROM ingestion_chunks "
            "WHERE ingestion_id=? AND selected=1 AND chunk_status='done' ORDER BY chunk_index ASC",
            (ingestion_id,),
        ).fetchall()

        if not chunk_rows:
            return RedirectResponse(url="/import/pdf", status_code=303)

        domain = (ing["domain"] or "").strip() if "domain" in ing.keys() else ""
        pdf_path = (ing["file_path"] or "") if "file_path" in ing.keys() else ""

        _commit_schema10_payload(
            conn, chunk_rows, candidate_id,
            ingestion_id, ing["filename"] or "", domain, actor,
            pdf_path=pdf_path,
        )

    return RedirectResponse(url="/tasks?status=draft", status_code=303)


@router.get("/import/json", response_class=HTMLResponse)
def import_json_form(request: Request):
    require(request.state.role, "import:json")
    return templates.TemplateResponse(request, "import_json.html", {})


def _parse_task_json(obj: dict[str, Any]) -> dict[str, Any]:
    title = str(obj.get("title", "")).strip()
    outcome = str(obj.get("outcome", "")).strip()
    procedure_name = str(obj.get("procedure_name", "")).strip() or title
    if not title:
        raise HTTPException(status_code=400, detail="Task import: title is required")
    if not outcome:
        raise HTTPException(status_code=400, detail=f"Task import '{title}': outcome is required")

    def _to_str_list(val: Any) -> list[str]:
        if not val:
            return []
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [str(v) for v in val if v]
        return []

    facts = _to_str_list(obj.get("facts"))
    concepts = _to_str_list(obj.get("concepts"))
    deps = _to_str_list(obj.get("dependencies"))
    steps = obj.get("steps") or []

    steps_norm = _normalize_steps(steps)
    _validate_steps_required(steps_norm)

    # Accept both schema 1.0 boolean "irreversible" and legacy integer "irreversible_flag"
    if "irreversible" in obj:
        irreversible_flag = 1 if obj["irreversible"] else 0
    else:
        irreversible_flag = 1 if bool(obj.get("irreversible_flag")) else 0
    assets = obj.get("task_assets") or obj.get("assets") or []
    if not isinstance(assets, list):
        raise HTTPException(status_code=400, detail=f"Task import '{title}': task_assets must be a list")

    return {
        "record_id": str(obj.get("record_id") or "").strip() or str(uuid.uuid4()),
        "version": int(obj.get("version") or 1),
        # Import is ingress: always draft. Trust boundary is human review.
        "status": "draft",
        "title": title,
        "outcome": outcome,
        "procedure_name": procedure_name,
        "software_name": str(obj["software_name"]).strip() or None if obj.get("software_name") else None,
        "software_version": str(obj["software_version"]).strip() or None if obj.get("software_version") else None,
        "facts": [str(x) for x in facts],
        "concepts": [str(x) for x in concepts],
        "dependencies": [str(x) for x in deps],
        "steps": steps_norm,
        "irreversible_flag": irreversible_flag,
        "task_assets": assets,
        "needs_review_flag": 1 if bool(obj.get("needs_review_flag")) else 0,
        "needs_review_note": (str(obj.get("needs_review_note")) if obj.get("needs_review_note") is not None else None),
    }


def _parse_workflow_json(obj: dict[str, Any]) -> dict[str, Any]:
    title = str(obj.get("title", "")).strip()
    objective = str(obj.get("objective", "")).strip()
    if not title:
        raise HTTPException(status_code=400, detail="Workflow import: title is required")
    if not objective:
        raise HTTPException(status_code=400, detail=f"Workflow import '{title}': objective is required")

    raw_refs = obj.get("task_refs") or obj.get("tasks") or []
    refs: list[tuple[str, int]] = []

    if isinstance(raw_refs, list):
        for item in raw_refs:
            if isinstance(item, str):
                if "@" not in item:
                    raise HTTPException(status_code=400, detail=f"Workflow import '{title}': invalid task ref '{item}'")
                rid, ver = item.split("@", 1)
                refs.append((rid.strip(), int(ver.strip())))
            elif isinstance(item, dict):
                rid = str(item.get("record_id") or item.get("task_record_id") or "").strip()
                ver = item.get("version") or item.get("task_version")
                if not rid or ver is None:
                    raise HTTPException(status_code=400, detail=f"Workflow import '{title}': task_refs items require record_id + version")
                refs.append((rid, int(ver)))
            else:
                raise HTTPException(status_code=400, detail=f"Workflow import '{title}': task_refs must contain strings or objects")
    else:
        raise HTTPException(status_code=400, detail=f"Workflow import '{title}': task_refs must be a list")

    if not refs:
        raise HTTPException(status_code=400, detail=f"Workflow import '{title}': at least one task_ref is required")

    return {
        "record_id": str(obj.get("record_id") or "").strip() or str(uuid.uuid4()),
        "version": int(obj.get("version") or 1),
        # Import is ingress: always draft. Trust boundary is human review.
        "status": "draft",
        "title": title,
        "objective": objective,
        "refs": refs,
        "needs_review_flag": 1 if bool(obj.get("needs_review_flag")) else 0,
        "needs_review_note": (str(obj.get("needs_review_note")) if obj.get("needs_review_note") is not None else None),
    }


@router.post("/import/json")
def import_json_run(
    request: Request,
    upload: UploadFile = File(...),
    actor_note: str = Form("Imported from JSON"),
):
    require(request.state.role, "import:json")
    actor = request.state.user

    raw = upload.file.read()
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    tasks_in: list[dict[str, Any]] = []
    workflows_in: list[dict[str, Any]] = []

    if isinstance(payload, dict):
        if isinstance(payload.get("tasks"), list):
            tasks_in = [x for x in payload.get("tasks") if isinstance(x, dict)]
        if isinstance(payload.get("workflows"), list):
            workflows_in = [x for x in payload.get("workflows") if isinstance(x, dict)]
        # Allow single objects
        if payload.get("type") == "task":
            tasks_in = [payload]
        if payload.get("type") == "workflow":
            workflows_in = [payload]
    elif isinstance(payload, list):
        # list of heterogeneous objects
        for x in payload:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "workflow":
                workflows_in.append(x)
            else:
                # default to task
                tasks_in.append(x)
    else:
        raise HTTPException(status_code=400, detail="Import JSON must be an object or a list")

    if not tasks_in and not workflows_in:
        raise HTTPException(status_code=400, detail="No tasks/workflows found in uploaded JSON")

    created_task_ids: list[str] = []
    created_workflow_ids: list[str] = []
    now = utc_now_iso()

    with db() as conn:
        initial_status = _import_initial_status(conn)
        # tasks first
        for t in tasks_in:
            item = _parse_task_json(t)
            item["status"] = initial_status

            # Prevent overwrite
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE record_id=? AND version=?",
                (item["record_id"], item["version"]),
            ).fetchone()
            if exists:
                raise HTTPException(
                    status_code=409,
                    detail=f"Task import conflict: {item['record_id']}@{item['version']} already exists",
                )

            conn.execute(
                """
                INSERT INTO tasks(
                  record_id, version, status,
                  title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
                  irreversible_flag, task_assets_json,
                  domain, software_name, software_version,
                  created_at, updated_at, created_by, updated_by,
                  reviewed_at, reviewed_by, change_note,
                  needs_review_flag, needs_review_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["record_id"],
                    item["version"],
                    item["status"],
                    item["title"],
                    item["outcome"],
                    _json_dump(item["facts"]),
                    _json_dump(item["concepts"]),
                    item["procedure_name"],
                    _json_dump(item["steps"]),
                    _json_dump(item["dependencies"]),
                    item["irreversible_flag"],
                    _json_dump(item["task_assets"]),
                    "",
                    item.get("software_name"),
                    item.get("software_version"),
                    now,
                    now,
                    actor,
                    actor,
                    None,
                    None,
                    actor_note.strip() or "Imported from JSON",
                    item["needs_review_flag"],
                    item["needs_review_note"],
                ),
            )
            audit("task", item["record_id"], item["version"], "create", actor, note="import:json")
            created_task_ids.append(item["record_id"])

        # workflows
        for w in workflows_in:
            item = _parse_workflow_json(w)
            item["status"] = initial_status

            exists = conn.execute(
                "SELECT 1 FROM workflows WHERE record_id=? AND version=?",
                (item["record_id"], item["version"]),
            ).fetchone()
            if exists:
                raise HTTPException(
                    status_code=409,
                    detail=f"Workflow import conflict: {item['record_id']}@{item['version']} already exists",
                )

            enforce_workflow_ref_rules(conn, item["refs"])
            # Imported workflows always arrive as draft; confirmation remains a human-only trust boundary.

            conn.execute(
                """
                INSERT INTO workflows(
                  record_id, version, status,
                  title, objective,
                  domains_json,
                  tags_json, meta_json,
                  created_at, updated_at, created_by, updated_by,
                  reviewed_at, reviewed_by, change_note,
                  needs_review_flag, needs_review_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["record_id"],
                    item["version"],
                    item["status"],
                    item["title"],
                    item["objective"],
                    _json_dump(_workflow_domains(conn, item["refs"])),
                    "[]",
                    "{}",
                    now,
                    now,
                    actor,
                    actor,
                    None,
                    None,
                    actor_note.strip() or "Imported from JSON",
                    item["needs_review_flag"],
                    item["needs_review_note"],
                ),
            )
            for idx, (rid, ver) in enumerate(item["refs"], start=1):
                conn.execute(
                    """
                    INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version)
                    VALUES (?,?,?,?,?)
                    """,
                    (item["record_id"], item["version"], idx, rid, ver),
                )

            audit("workflow", item["record_id"], item["version"], "create", actor, note="import:json")
            created_workflow_ids.append(item["record_id"])

    # Redirect to something sensible
    if created_workflow_ids and not created_task_ids:
        return RedirectResponse(url="/workflows", status_code=303)
    return RedirectResponse(url="/tasks?status=draft", status_code=303)
