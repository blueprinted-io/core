from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import templates, UPLOADS_DIR
from ..database import db, utc_now_iso, _workflow_domains, enforce_workflow_ref_rules, _get_llm_config
from ..linting import _normalize_steps, _validate_steps_required
from ..audit import audit
from ..auth import require
from ..ingestion import (
    _llm_probe, _llm_chat,
    _sha256_bytes, _task_fingerprint, _near_duplicate_score,
    _pdf_extract_pages, _chunk_text, _pdf_extract_outline, _chunk_by_structure,
)
from ..utils import _json_dump, _json_load

router = APIRouter()


@router.get("/_llm/status")
def llm_status(request: Request):
    require(request.state.role, "import:pdf")
    with db() as conn:
        cfg = _get_llm_config(conn)
    probe = _llm_probe(cfg["llm_base_url"], cfg["llm_api_key"])
    model = cfg.get("llm_model") or ""
    return {"ok": bool(probe.get("ok")), "detail": str(probe.get("detail")), "model": model}


@router.get("/import/pdf", response_class=HTMLResponse)
def import_pdf_form(request: Request):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        rows = conn.execute(
            "SELECT id, filename, created_at, status, cursor_chunk FROM ingestions WHERE source_type='pdf' AND created_by=? ORDER BY created_at DESC LIMIT 50",
            (actor,),
        ).fetchall()

    with db() as conn:
        cfg = _get_llm_config(conn)
    probe = _llm_probe(cfg["llm_base_url"], cfg["llm_api_key"])

    return templates.TemplateResponse(
        request,
        "import_pdf.html",
        {
            "llm_ok": bool(probe.get("ok")),
            "llm_detail": str(probe.get("detail")),
            "llm_model": cfg.get("llm_model") or "",
            "llm_configured": bool(cfg.get("llm_base_url")),
            "ingestions": [dict(r) for r in rows],
        },
    )


@router.post("/import/pdf/prepare")
def import_pdf_prepare(
    request: Request,
    pdf: UploadFile = File(...),
    actor_note: str = Form("Imported from PDF"),
):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        cfg = _get_llm_config(conn)

    probe = _llm_probe(cfg["llm_base_url"], cfg["llm_api_key"])
    if not probe.get("ok"):
        raise HTTPException(status_code=502, detail=f"LLM is not reachable: {probe.get('detail')}. Ask an admin to configure the LLM provider at /admin/llm.")

    max_tasks = cfg["llm_max_tasks_per_chunk"]

    # Save upload + compute hash identity
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", pdf.filename or "upload.pdf")
    file_id = str(uuid.uuid4())
    out_path = os.path.join(UPLOADS_DIR, f"{file_id}__{safe_name}")
    file_bytes = pdf.file.read()
    with open(out_path, "wb") as f:
        f.write(file_bytes)

    sha = _sha256_bytes(file_bytes)

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM ingestions WHERE source_type='pdf' AND source_sha256=? AND created_by=? ORDER BY created_at DESC LIMIT 1",
            (sha, actor),
        ).fetchone()

        if existing:
            ingestion_id = str(existing["id"])
        else:
            ingestion_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO ingestions(id, source_type, source_sha256, filename, created_by, created_at, status, cursor_chunk, max_tasks_per_run, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ingestion_id, "pdf", sha, safe_name, actor, utc_now_iso(), "draft", 0, max_tasks, actor_note.strip() or "Imported from PDF"),
            )

        # If we don't have cached chunks yet, extract + store now.
        cached = conn.execute(
            "SELECT 1 FROM ingestion_chunks WHERE ingestion_id=? LIMIT 1",
            (ingestion_id,),
        ).fetchone()

        if not cached:
            pages = _pdf_extract_pages(out_path)
            outline = _pdf_extract_outline(out_path)
            if outline:
                chunks = _chunk_by_structure(pages, outline)
            else:
                chunks = _chunk_text(pages, max_chars=12000)
            if not chunks:
                raise HTTPException(status_code=400, detail="No extractable text found in PDF")
            now = utc_now_iso()
            for idx, ch in enumerate(chunks):
                conn.execute(
                    "INSERT OR REPLACE INTO ingestion_chunks(ingestion_id, chunk_index, pages_json, text, llm_result_json, created_at, section_title) VALUES (?,?,?,?,?,?,?)",
                    (ingestion_id, idx, _json_dump(ch.get("pages", [])), ch.get("text", ""), None, now, ch.get("section_title") or None),
                )

    return RedirectResponse(url=f"/import/pdf/run?ingestion_id={ingestion_id}", status_code=303)


@router.get("/import/pdf/run", response_class=HTMLResponse)
def import_pdf_run(request: Request, ingestion_id: str):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    with db() as conn:
        cfg = _get_llm_config(conn)

    probe = _llm_probe(cfg["llm_base_url"], cfg["llm_api_key"])
    if not probe.get("ok"):
        return templates.TemplateResponse(
            request,
            "import_pdf_preview.html",
            {
                "ingestion": {"id": ingestion_id, "cursor_chunk": "?", "filename": "(unknown)"},
                "candidates": [],
                "workflows": [],
                "error": f"LLM is not reachable: {probe.get('detail')}. Ask an admin to configure it at /admin/llm.",
                "done": False,
            },
        )

    max_tasks = cfg["llm_max_tasks_per_chunk"]
    max_chunks = cfg["llm_max_chunks_per_run"]

    with db() as conn:
        ing = conn.execute(
            "SELECT * FROM ingestions WHERE id=? AND created_by=?",
            (ingestion_id, actor),
        ).fetchone()
        if not ing:
            raise HTTPException(404)

        cursor = int(ing["cursor_chunk"])
        chunk_rows = conn.execute(
            "SELECT chunk_index, pages_json, text, llm_result_json, section_title FROM ingestion_chunks WHERE ingestion_id=? AND chunk_index>=? ORDER BY chunk_index ASC LIMIT ?",
            (ingestion_id, cursor, max_chunks),
        ).fetchall()

        if not chunk_rows:
            return templates.TemplateResponse(
                request,
                "import_pdf_preview.html",
                {
                    "ingestion": dict(ing),
                    "candidates": [],
                    "workflows": [],
                    "error": None,
                    "done": True,
                },
            )

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
            if not row:
                continue
            existing_tasks.append(
                {
                    "record_id": r["record_id"],
                    "title": row["title"],
                    "outcome": row["outcome"],
                    "steps": _json_load(row["steps_json"]) or [],
                }
            )

    system = {
        "role": "system",
        "content": (
            "You are extracting governed learning Tasks from technical documentation. "
            "You MUST follow the schema strictly. Do not invent steps that are not supported by the provided source. "
            "If uncertain, omit. Every step MUST include a completion check. "
            "Concepts are best-effort and should be minimal."
        ),
    }

    user_prompt_tpl = (
        "From the following SOURCE TEXT (with page markers), propose up to {per_chunk} Task records.\n\n"
        "Rules:\n"
        "- A Task is one atomic outcome.\n"
        "- Provide: title, outcome, facts[], concepts[], dependencies[], procedure_name.\n"
        "- Provide steps[] where each step has: text, completion, and optional actions[].\n"
        "- Steps and completion MUST be concrete and verifiable.\n"
        "- Do NOT include troubleshooting.\n"
        "- Return JSON ONLY: {{\"tasks\": [ ... ]}} (no markdown, no commentary).\n\n"
        "{section_header}"
        "SOURCE TEXT:\n{source}\n"
    )

    candidates: list[dict[str, Any]] = []
    skipped_chunks: list[int] = []
    fatal_error: str | None = None

    for cr in chunk_rows:
        chunk_index = int(cr["chunk_index"])
        cached_raw = (cr["llm_result_json"] or "").strip()

        # Skip previously timed-out chunks
        if cached_raw == '"timeout"':
            skipped_chunks.append(chunk_index)
            continue

        if cached_raw:
            try:
                data = json.loads(cached_raw)
            except Exception:
                data = None
        else:
            section_title = (cr["section_title"] or "").strip()
            section_header = f"SECTION: {section_title}\n\n" if section_title else ""
            user_prompt = (
                user_prompt_tpl
                .replace("{per_chunk}", str(max_tasks))
                .replace("{section_header}", section_header)
                .replace("{source}", cr["text"])
            )
            try:
                raw = _llm_chat(
                    [system, {"role": "user", "content": user_prompt}],
                    cfg,
                )
                try:
                    data = json.loads(raw)
                except Exception:
                    fatal_error = f"Model returned non-JSON for chunk {chunk_index}"
                    break

                with db() as conn:
                    conn.execute(
                        "UPDATE ingestion_chunks SET llm_result_json=? WHERE ingestion_id=? AND chunk_index=?",
                        (_json_dump(data), ingestion_id, chunk_index),
                    )
            except HTTPException as e:
                if e.status_code == 504:
                    # Timeout: mark chunk as skipped and continue
                    skipped_chunks.append(chunk_index)
                    with db() as conn:
                        conn.execute(
                            "UPDATE ingestion_chunks SET llm_result_json=? WHERE ingestion_id=? AND chunk_index=?",
                            ('"timeout"', ingestion_id, chunk_index),
                        )
                    continue
                fatal_error = str(e.detail)
                break
            except Exception as e:
                fatal_error = str(e)
                break

        if data is None:
            continue
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            continue

        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title", "")).strip()
            if not title:
                continue
            candidates.append({
                "chunk_index": chunk_index,
                "pages": _json_load(cr["pages_json"]) or [],
                "task": t,
            })

    if fatal_error:
        return templates.TemplateResponse(
            request,
            "import_pdf_preview.html",
            {
                "ingestion": {"id": ingestion_id, "cursor_chunk": cursor, "filename": ing["filename"]},
                "candidates": [],
                "workflows": [],
                "error": fatal_error,
                "done": False,
            },
        )

    # Merge + cap to max_tasks
    # De-dupe within candidate list by fingerprint
    out: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    for c in candidates:
        fp = _task_fingerprint(c["task"])
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        out.append(c)
        if len(out) >= max_tasks:
            break

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

        flagged.append(
            {
                "id": _sha256_bytes((fp + str(c["chunk_index"])).encode("utf-8"))[:16],
                "title": str(t.get("title", "")).strip(),
                "chunk_index": c["chunk_index"],
                "pages": c["pages"],
                "dup_matches": near_matches,
            }
        )

    # Propose workflows from candidate titles (optional, best-effort)
    wf_candidates: list[dict[str, Any]] = []
    if flagged:
        titles = [x["title"] for x in flagged]
        wf_system = {"role": "system", "content": "You propose small Workflows from a list of Task titles. Return JSON only."}
        wf_user = (
            "Given these Task titles, propose up to 3 Workflow candidates.\n"
            "Return JSON ONLY: {\"workflows\": [{\"title\":...,\"objective\":...,\"task_titles\":[...] }]}\n"
            "Rules: a workflow must reference 2-6 tasks by exact title; do not invent titles.\n\n"
            + _json_dump({"task_titles": titles})
        )
        try:
            raw = _llm_chat([wf_system, {"role": "user", "content": wf_user}], cfg)
            wf_data = json.loads(raw)
            wfs = wf_data.get("workflows") if isinstance(wf_data, dict) else None
            if isinstance(wfs, list):
                for wf in wfs[:3]:
                    if not isinstance(wf, dict):
                        continue
                    wt = str(wf.get("title", "")).strip()
                    obj = str(wf.get("objective", "")).strip()
                    tts = wf.get("task_titles") or []
                    if not wt or not obj or not isinstance(tts, list):
                        continue
                    wf_candidates.append({
                        "id": _sha256_bytes((wt + obj).encode("utf-8"))[:16],
                        "title": wt,
                        "objective": obj,
                        "task_titles": [str(x) for x in tts if str(x).strip() in titles],
                    })
        except Exception:
            pass  # Workflow suggestions are optional; don't fail the whole run

    skipped_note = f" ({len(skipped_chunks)} chunk(s) timed out and were skipped)" if skipped_chunks else ""

    return templates.TemplateResponse(
        request,
        "import_pdf_preview.html",
        {
            "ingestion": {"id": ingestion_id, "cursor_chunk": int(ing["cursor_chunk"]), "filename": ing["filename"]},
            "candidates": flagged,
            "workflows": wf_candidates,
            "error": None,
            "skipped_note": skipped_note,
            "done": False,
        },
    )


@router.post("/import/pdf/commit")
def import_pdf_commit(
    request: Request,
    ingestion_id: str = Form(...),
    candidate_id: list[str] = Form([]),
    workflow_id: list[str] = Form([]),
):
    require(request.state.role, "import:pdf")
    actor = request.state.user

    # Load last run candidates from cached llm results for current cursor window.
    with db() as conn:
        ing = conn.execute("SELECT * FROM ingestions WHERE id=? AND created_by=?", (ingestion_id, actor)).fetchone()
        if not ing:
            raise HTTPException(404)

        cursor = int(ing["cursor_chunk"])
        cfg = _get_llm_config(conn)
        max_chunks = cfg["llm_max_chunks_per_run"]
        chunk_rows = conn.execute(
            "SELECT chunk_index, pages_json, text, llm_result_json FROM ingestion_chunks WHERE ingestion_id=? AND chunk_index>=? ORDER BY chunk_index ASC LIMIT ?",
            (ingestion_id, cursor, max_chunks),
        ).fetchall()

        if not chunk_rows:
            return RedirectResponse(url="/tasks?status=draft", status_code=303)

        # Reconstruct candidates deterministically
        reconstructed: list[dict[str, Any]] = []
        for cr in chunk_rows:
            if not cr["llm_result_json"]:
                continue
            data = json.loads(cr["llm_result_json"])
            tasks = data.get("tasks") if isinstance(data, dict) else []
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                fp = _task_fingerprint(t)
                cid = _sha256_bytes((fp + str(int(cr["chunk_index"]))).encode("utf-8"))[:16]
                if cid not in candidate_id:
                    continue
                reconstructed.append({"task": t, "pages": _json_load(cr["pages_json"]) or []})

        now = utc_now_iso()
        created_tasks: dict[str, tuple[str, int]] = {}  # title -> (record_id, version)

        # Insert selected tasks
        for item in reconstructed:
            t = item["task"]
            title = str(t.get("title", "")).strip()
            outcome = str(t.get("outcome", "")).strip()
            procedure_name = str(t.get("procedure_name", "")).strip() or title
            facts = t.get("facts") or []
            concepts = t.get("concepts") or []
            deps = t.get("dependencies") or []
            steps = t.get("steps") or []

            steps_norm = _normalize_steps(steps)
            _validate_steps_required(steps_norm)

            record_id = str(uuid.uuid4())
            version = 1

            assets = [
                {
                    "url": f"ingestion:{ingestion_id}",
                    "type": "link",
                    "label": f"source_pdf:{ing['filename']} pages:{item['pages']}",
                }
            ]

            conn.execute(
                """
                INSERT INTO tasks(
                  record_id, version, status,
                  title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
                  irreversible_flag, task_assets_json,
                  domain,
                  created_at, updated_at, created_by, updated_by,
                  reviewed_at, reviewed_by, change_note,
                  needs_review_flag, needs_review_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    version,
                    "draft",
                    title,
                    outcome,
                    _json_dump([str(x) for x in facts]),
                    _json_dump([str(x) for x in concepts]),
                    procedure_name,
                    _json_dump(steps_norm),
                    _json_dump([str(x) for x in deps]),
                    0,
                    _json_dump(assets),
                    "",
                    now,
                    now,
                    actor,
                    actor,
                    None,
                    None,
                    f"import:pdf ingestion={ingestion_id}",
                    1,
                    "AI-imported: check for duplicates and correctness",
                ),
            )
            audit("task", record_id, version, "create", actor, note="import:pdf")
            created_tasks[title] = (record_id, version)

        # Insert workflows selected
        # Recompute workflow candidates from selected titles (best-effort)
        if workflow_id and created_tasks:
            titles = list(created_tasks.keys())
            wf_system = {"role": "system", "content": "You propose small Workflows from a list of Task titles. Return JSON only."}
            wf_user = (
                "Given these Task titles, propose up to 3 Workflow candidates.\n"
                "Return JSON ONLY: {\"workflows\": [{\"id\":...,\"title\":...,\"objective\":...,\"task_titles\":[...] }]}\n"
                "Rules: a workflow must reference 2-6 tasks by exact title; do not invent titles.\n\n"
                + _json_dump({"task_titles": titles})
            )
            cfg = _get_llm_config(conn)
            raw = _llm_chat([wf_system, {"role": "user", "content": wf_user}], cfg)
            data = json.loads(raw)
            wfs = data.get("workflows") if isinstance(data, dict) else None
            if isinstance(wfs, list):
                for wf in wfs:
                    if not isinstance(wf, dict):
                        continue
                    wid = str(wf.get("id", "")).strip() or _sha256_bytes((str(wf.get("title",""))+str(wf.get("objective",""))).encode("utf-8"))[:16]
                    if wid not in workflow_id:
                        continue
                    title = str(wf.get("title", "")).strip()
                    objective = str(wf.get("objective", "")).strip()
                    tts = wf.get("task_titles") or []
                    if not title or not objective or not isinstance(tts, list):
                        continue

                    wf_rid = str(uuid.uuid4())
                    wf_ver = 1
                    conn.execute(
                        "INSERT INTO workflows(record_id, version, status, title, objective, domains_json, tags_json, meta_json, created_at, updated_at, created_by, updated_by, reviewed_at, reviewed_by, change_note, needs_review_flag, needs_review_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (wf_rid, wf_ver, "draft", title, objective, "[]", "[]", "{}", now, now, actor, actor, None, None, f"import:pdf ingestion={ingestion_id}", 1, "AI-imported: check composition"),
                    )

                    order = 1
                    for tt in [str(x) for x in tts if str(x) in created_tasks]:
                        tr, tv = created_tasks[tt]
                        conn.execute(
                            "INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version) VALUES (?,?,?,?,?)",
                            (wf_rid, wf_ver, order, tr, tv),
                        )
                        order += 1

                    audit("workflow", wf_rid, wf_ver, "create", actor, note="import:pdf")

        # Advance cursor if commit happened (clean, deterministic)
        if reconstructed:
            conn.execute(
                "UPDATE ingestions SET cursor_chunk=cursor_chunk+? , status='in_progress' WHERE id=?",
                (len(chunk_rows), ingestion_id),
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

    facts = obj.get("facts") or []
    concepts = obj.get("concepts") or []
    deps = obj.get("dependencies") or []
    steps = obj.get("steps") or []

    if not isinstance(facts, list) or not isinstance(concepts, list) or not isinstance(deps, list):
        raise HTTPException(status_code=400, detail=f"Task import '{title}': facts/concepts/dependencies must be lists")

    steps_norm = _normalize_steps(steps)
    _validate_steps_required(steps_norm)

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
        # tasks first
        for t in tasks_in:
            item = _parse_task_json(t)
            # Import is ingress: always draft.
            # (Seeding/demo data should write directly to the DB via seed scripts, not via import.)
            item["status"] = "draft"

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
                  domain,
                  created_at, updated_at, created_by, updated_by,
                  reviewed_at, reviewed_by, change_note,
                  needs_review_flag, needs_review_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            # Import is ingress: always draft.
            # (Seeding/demo data should write directly to the DB via seed scripts, not via import.)
            item["status"] = "draft"

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
