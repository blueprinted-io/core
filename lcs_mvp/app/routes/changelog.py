from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("blueprinted.changelog")

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from ..config import templates, DB_PATH_CTX
from ..database import db, utc_now_iso, _active_domains, _get_llm_config
from ..audit import audit, get_latest_version
from ..auth import require
from ..linting import _normalize_steps, _validate_steps_required
from ..utils import _json_dump, _json_load
from ..ingestion import (
    _pdf_extract_pages, _pdf_is_scanned,
    _run_changelog_screening, _run_changelog_proposing,
)
from .tasks import _cascade_workflow_updates

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_run(conn, run_id: str, actor: str):
    run = conn.execute("SELECT * FROM changelog_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(404)
    if run["created_by"] != actor:
        raise HTTPException(403)
    return run


def _confirmed_tasks_in_scope(conn, software_name: str | None, scope_domain: str | None) -> list:
    """Return latest confirmed version of each task matching the scope filters."""
    wheres = ["t.status='confirmed'"]
    params: list[Any] = []
    if software_name:
        wheres.append("t.software_name=?")
        params.append(software_name)
    if scope_domain:
        wheres.append("t.domain=?")
        params.append(scope_domain)
    where_clause = " AND ".join(wheres)
    return conn.execute(
        f"""
        SELECT t.* FROM tasks t
        INNER JOIN (
            SELECT record_id, MAX(version) AS max_v
            FROM tasks
            WHERE {where_clause}
            GROUP BY record_id
        ) latest ON t.record_id = latest.record_id AND t.version = latest.max_v
        """,
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Entry form
# ---------------------------------------------------------------------------

@router.get("/import/changelog")
def changelog_index(request: Request):
    require(request.state.role, "import:changelog")
    with db() as conn:
        domains = _active_domains(conn)
        # Recent runs for this user
        runs = conn.execute(
            "SELECT * FROM changelog_runs WHERE created_by=? ORDER BY created_at DESC LIMIT 20",
            (request.state.user,),
        ).fetchall()
    return templates.TemplateResponse("import_changelog.html", {
        "request": request, "domains": domains, "runs": runs,
    })


# ---------------------------------------------------------------------------
# Prepare: parse content, create run, redirect to scope confirmation
# ---------------------------------------------------------------------------

@router.post("/import/changelog/prepare")
async def changelog_prepare(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    software_name: str = Form(""),
    scope_domain: str = Form(""),
    json_text: str = Form(""),
    upload: UploadFile | None = File(None),
):
    require(request.state.role, "import:changelog")
    actor = request.state.user

    # Extract content
    content = ""
    source_type = "text"

    if upload and upload.filename:
        raw = await upload.read()
        fname = (upload.filename or "").lower()
        if fname.endswith(".pdf"):
            source_type = "pdf"
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            try:
                pages = _pdf_extract_pages(tmp_path)
                if _pdf_is_scanned(pages):
                    raise HTTPException(422, detail="PDF appears to be scanned (no extractable text).")
                content = "\n\n".join(p["text"] for p in pages if p.get("text", "").strip())
            finally:
                os.unlink(tmp_path)
        else:
            content = raw.decode("utf-8", errors="replace")
            source_type = "text"
    elif json_text.strip():
        content = json_text.strip()
        source_type = "text"

    if not content.strip():
        raise HTTPException(400, detail="No changelog content provided.")

    sn = software_name.strip() or None
    sd = scope_domain.strip() or None

    with db() as conn:
        task_count = len(_confirmed_tasks_in_scope(conn, sn, sd))
        if task_count == 0:
            raise HTTPException(400, detail="No confirmed tasks match the selected scope. Adjust software name or domain filter.")

        run_id = str(uuid.uuid4())
        now = utc_now_iso()
        conn.execute(
            """INSERT INTO changelog_runs(id, title, content, software_name, scope_domain, source_type, created_by, created_at, job_status)
               VALUES (?,?,?,?,?,?,?,?,'pending')""",
            (run_id, title.strip(), content, sn, sd, source_type, actor, now),
        )

    return RedirectResponse(f"/import/changelog/screen/{run_id}", status_code=303)


# ---------------------------------------------------------------------------
# Scope confirmation + start screening
# ---------------------------------------------------------------------------

@router.get("/import/changelog/screen/{run_id}")
def changelog_screen_get(request: Request, run_id: str):
    require(request.state.role, "import:changelog")
    with db() as conn:
        run = _load_run(conn, run_id, request.state.user)
        tasks = _confirmed_tasks_in_scope(conn, run["software_name"], run["scope_domain"])
    return templates.TemplateResponse("import_changelog_screen.html", {
        "request": request, "run": run, "task_count": len(tasks),
        "content_preview": run["content"][:400],
    })


@router.post("/import/changelog/screen/{run_id}")
def changelog_screen_post(request: Request, run_id: str, background_tasks: BackgroundTasks):
    require(request.state.role, "import:changelog")
    actor = request.state.user
    db_path = DB_PATH_CTX.get()

    with db() as conn:
        run = _load_run(conn, run_id, actor)
        if run["job_status"] not in ("pending", "screened"):
            raise HTTPException(400, detail="Run is not in a state that allows re-screening.")
        tasks = _confirmed_tasks_in_scope(conn, run["software_name"], run["scope_domain"])
        if not tasks:
            raise HTTPException(400, detail="No confirmed tasks in scope.")

        # Clear any previous impacts and insert fresh rows
        conn.execute("DELETE FROM changelog_impacts WHERE run_id=?", (run_id,))
        for task in tasks:
            conn.execute(
                """INSERT INTO changelog_impacts(id, run_id, task_record_id, task_version, item_status)
                   VALUES (?,?,?,?,'pending')""",
                (str(uuid.uuid4()), run_id, task["record_id"], task["version"]),
            )
        conn.execute("UPDATE changelog_runs SET job_status='pending' WHERE id=?", (run_id,))

    background_tasks.add_task(_run_changelog_screening, run_id, db_path)
    return RedirectResponse(f"/import/changelog/review/{run_id}", status_code=303)


# ---------------------------------------------------------------------------
# Review: show screening results, allow deselection, submit to propose
# ---------------------------------------------------------------------------

@router.get("/import/changelog/review/{run_id}")
def changelog_review(request: Request, run_id: str):
    require(request.state.role, "import:changelog")
    with db() as conn:
        run = _load_run(conn, run_id, request.state.user)
        impacts = conn.execute(
            """SELECT ci.*, t.title as task_title, t.domain, t.software_version
               FROM changelog_impacts ci
               JOIN tasks t ON t.record_id = ci.task_record_id AND t.version = ci.task_version
               WHERE ci.run_id=? ORDER BY ci.affected DESC, t.title ASC""",
            (run_id,),
        ).fetchall()
    return templates.TemplateResponse("import_changelog_review.html", {
        "request": request, "run": run, "impacts": impacts,
    })


# ---------------------------------------------------------------------------
# Propose: mark selected impacts, start proposing background
# ---------------------------------------------------------------------------

@router.post("/import/changelog/propose/{run_id}")
def changelog_propose(
    request: Request,
    run_id: str,
    background_tasks: BackgroundTasks,
    selected_ids: list[str] = Form([]),
):
    require(request.state.role, "import:changelog")
    actor = request.state.user
    db_path = DB_PATH_CTX.get()

    with db() as conn:
        run = _load_run(conn, run_id, actor)
        if run["job_status"] != "screened":
            raise HTTPException(400, detail="Screening must complete before proposals can be generated.")
        if not selected_ids:
            raise HTTPException(400, detail="No tasks selected for revision proposal.")

        # Mark selected as 'selected', rest as 'skipped'
        conn.execute(
            "UPDATE changelog_impacts SET item_status='skipped' WHERE run_id=? AND item_status='screened'",
            (run_id,),
        )
        for impact_id in selected_ids:
            conn.execute(
                "UPDATE changelog_impacts SET item_status='selected' WHERE id=? AND run_id=?",
                (impact_id, run_id),
            )
        conn.execute("UPDATE changelog_runs SET job_status='screened' WHERE id=?", (run_id,))

    background_tasks.add_task(_run_changelog_proposing, run_id, db_path)
    return RedirectResponse(f"/import/changelog/proposals/{run_id}", status_code=303)


# ---------------------------------------------------------------------------
# Proposals: show before/after, submit to commit
# ---------------------------------------------------------------------------

@router.get("/import/changelog/proposals/{run_id}")
def changelog_proposals(request: Request, run_id: str):
    require(request.state.role, "import:changelog")
    with db() as conn:
        run = _load_run(conn, run_id, request.state.user)
        impacts = conn.execute(
            """SELECT ci.*, t.title as task_title, t.domain, t.software_version,
                      t.outcome as orig_outcome, t.steps_json as orig_steps_json,
                      t.facts_json as orig_facts_json, t.software_version as orig_sw_version
               FROM changelog_impacts ci
               JOIN tasks t ON t.record_id = ci.task_record_id AND t.version = ci.task_version
               WHERE ci.run_id=? AND ci.item_status IN ('proposed','error')
               ORDER BY t.title ASC""",
            (run_id,),
        ).fetchall()

    # Parse proposed JSON and original steps for template rendering
    proposals = []
    for imp in impacts:
        proposed = None
        if imp["proposed_json"]:
            try:
                proposed = json.loads(imp["proposed_json"])
            except Exception:
                pass
        orig_steps = []
        try:
            orig_steps = json.loads(imp["orig_steps_json"] or "[]")
        except Exception:
            pass
        proposals.append({"impact": imp, "proposed": proposed, "orig_steps": orig_steps})

    return templates.TemplateResponse("import_changelog_proposals.html", {
        "request": request, "run": run, "proposals": proposals,
    })


# ---------------------------------------------------------------------------
# Commit: create new draft task versions
# ---------------------------------------------------------------------------

@router.post("/import/changelog/commit/{run_id}")
def changelog_commit(
    request: Request,
    run_id: str,
    commit_ids: list[str] = Form([]),
):
    require(request.state.role, "import:changelog")
    actor = request.state.user

    with db() as conn:
        run = _load_run(conn, run_id, actor)
        if run["job_status"] != "complete":
            raise HTTPException(400, detail="Proposals must finish generating before committing.")
        if not commit_ids:
            raise HTTPException(400, detail="No proposals selected to commit.")

        impacts = conn.execute(
            "SELECT * FROM changelog_impacts WHERE run_id=? AND id IN ({}) AND item_status='proposed'".format(
                ",".join("?" * len(commit_ids))
            ),
            [run_id] + list(commit_ids),
        ).fetchall()

        now = utc_now_iso()
        committed = 0
        for imp in impacts:
            task = conn.execute(
                "SELECT * FROM tasks WHERE record_id=? AND version=?",
                (imp["task_record_id"], imp["task_version"]),
            ).fetchone()
            if not task:
                continue

            try:
                proposed = json.loads(imp["proposed_json"])
            except Exception:
                continue

            latest_v = get_latest_version(conn, "tasks", imp["task_record_id"]) or imp["task_version"]
            new_v = latest_v + 1

            steps_raw = proposed.get("steps", json.loads(task["steps_json"] or "[]"))
            steps = _normalize_steps(steps_raw)

            change_note = f"Updated for changelog: {run['title']}"

            conn.execute(
                """
                INSERT INTO tasks(
                  record_id, version, status,
                  title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
                  irreversible_flag, task_assets_json,
                  domain, software_name, software_version,
                  tags_json, meta_json,
                  created_at, updated_at, created_by, updated_by,
                  reviewed_at, reviewed_by, change_note,
                  needs_review_flag, needs_review_note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    imp["task_record_id"],
                    new_v,
                    "draft",
                    proposed.get("title", task["title"]),
                    proposed.get("outcome", task["outcome"]),
                    _json_dump(proposed.get("facts", _json_load(task["facts_json"] or "[]"))),
                    _json_dump(proposed.get("concepts", _json_load(task["concepts_json"] or "[]"))),
                    proposed.get("procedure_name", task["procedure_name"]),
                    _json_dump(steps),
                    _json_dump(proposed.get("dependencies", _json_load(task["dependencies_json"] or "[]"))),
                    1 if proposed.get("irreversible", task["irreversible_flag"]) else 0,
                    task["task_assets_json"] or "[]",
                    task["domain"],
                    proposed.get("software_name", task["software_name"]),
                    proposed.get("software_version", task["software_version"]),
                    task["tags_json"] or "[]",
                    task["meta_json"] or "{}",
                    now, now, actor, actor,
                    None, None,
                    change_note,
                    1,
                    f"Changelog-driven update from run {run_id[:8]}",
                ),
            )

            _cascade_workflow_updates(conn, imp["task_record_id"], new_v, actor)
            audit(conn=conn, entity_type="task", record_id=imp["task_record_id"], version=new_v,
                  action="new_version", actor=actor, note=f"changelog:{run_id}")

            conn.execute(
                "UPDATE changelog_impacts SET new_task_version=?, item_status='committed' WHERE id=?",
                (new_v, imp["id"]),
            )
            committed += 1

    return RedirectResponse(f"/tasks?status=draft", status_code=303)
