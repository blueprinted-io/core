from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import templates
from ..database import db, utc_now_iso, _active_domains, _user_has_domain
from ..audit import audit, _fetch_return_note, _fetch_force_action, get_latest_version
from ..linting import lint_steps, _normalize_steps, _zip_steps, _validate_steps_required
from ..auth import require
from ..utils import _json_dump, _json_load, parse_lines, parse_meta

router = APIRouter()


@router.get("/tasks", response_class=HTMLResponse)
def tasks_list(request: Request, status: str | None = None, q: str | None = None, domain: str | None = None, tag: str | None = None):
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None
    tag_norm = (tag or "").strip().lower() or None

    with db() as conn:
        sql = "SELECT record_id, MAX(version) AS latest_version FROM tasks GROUP BY record_id ORDER BY record_id"
        rows = conn.execute(sql).fetchall()

        # Option lists (for dropdown filters)
        domains = _active_domains(conn)
        all_tags: set[str] = set()

        items = []
        for r in rows:
            rid = r["record_id"]
            latest_v = int(r["latest_version"])
            latest = conn.execute(
                "SELECT * FROM tasks WHERE record_id=? AND version=?", (rid, latest_v)
            ).fetchone()
            if not latest:
                continue
            if status and latest["status"] != status:
                continue
            if q_norm and q_norm not in (latest["title"] or "").lower():
                continue

            # derived: update_pending_confirmation
            confirmed_v = conn.execute(
                "SELECT MAX(version) AS v FROM tasks WHERE record_id=? AND status='confirmed'",
                (rid,),
            ).fetchone()["v"]
            update_pending = False
            if confirmed_v is not None and latest_v > int(confirmed_v) and latest["status"] in ("draft", "submitted"):
                update_pending = True

            tags = [str(x).strip().lower() for x in (_json_load(latest["tags_json"]) if "tags_json" in latest.keys() else [])]
            domain_val = (latest["domain"] if "domain" in latest.keys() else "")

            for t in tags:
                if t:
                    all_tags.add(t)

            # Apply filters
            if domain_norm and (domain_val or "").strip().lower() != domain_norm:
                continue
            if tag_norm and tag_norm not in set(tags):
                continue

            # Returned-for-changes signal: does this version have a return note?
            has_return_note = False
            if latest["status"] == "returned":
                rn = conn.execute(
                    "SELECT 1 FROM audit_log WHERE entity_type='task' AND record_id=? AND version=? AND action='return_for_changes' LIMIT 1",
                    (rid, latest_v),
                ).fetchone()
                has_return_note = bool(rn)

            items.append(
                {
                    "record_id": rid,
                    "latest_version": latest_v,
                    "title": latest["title"],
                    "status": latest["status"],
                    "needs_review_flag": bool(latest["needs_review_flag"]),
                    "update_pending_confirmation": update_pending,
                    "tags": tags,
                    "domain": domain_val,
                    "has_return_note": has_return_note,
                }
            )

    return templates.TemplateResponse(
        request,
        "tasks_list.html",
        {
            "items": items,
            "status": status,
            "q": q,
            "domain": domain_norm or "",
            "tag": tag_norm or "",
            "domains": domains,
            "tags": sorted(all_tags),
        },
    )


@router.get("/tasks/new", response_class=HTMLResponse)
def task_new_form(request: Request):
    require(request.state.role, "task:create")
    with db() as conn:
        domains = _active_domains(conn)
    return templates.TemplateResponse(
        request,
        "task_edit.html",
        {"mode": "new", "task": None, "warnings": [], "domains": domains},
    )


@router.post("/tasks/new")
def task_create(
    request: Request,
    title: str = Form(...),
    outcome: str = Form(...),
    procedure_name: str = Form(...),
    domain: str = Form(""),
    tags: str = Form(""),
    meta: str = Form(""),
    facts: str = Form(""),
    concepts: str = Form(""),
    dependencies: str = Form(""),
    step_text: list[str] = Form([]),
    step_completion: list[str] = Form([]),
    step_actions: list[str] = Form([]),
    step_notes: list[str] = Form([]),
    irreversible_flag: bool = Form(False),
):
    require(request.state.role, "task:create")
    actor = request.state.user
    record_id = str(uuid.uuid4())
    version = 1

    facts_list = parse_lines(facts)
    concepts_list = parse_lines(concepts)
    deps_list = parse_lines(dependencies)
    # Phase 1: tasks are intentionally tagless (workflow-only tags model).
    tags_list: list[str] = []
    meta_obj = parse_meta(meta)
    steps_list = _zip_steps(step_text, step_completion, step_actions, step_notes)
    _validate_steps_required(steps_list)

    warnings = lint_steps(steps_list)

    now = utc_now_iso()
    with db() as conn:
        domains = _active_domains(conn)
        domain_norm = (domain or "").strip().lower()
        if domain_norm and domain_norm not in domains:
            raise HTTPException(status_code=400, detail=f"Invalid domain '{domain_norm}'")

        conn.execute(
            """
            INSERT INTO tasks(
              record_id, version, status,
              title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
              irreversible_flag, task_assets_json,
              domain,
              tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                version,
                "draft",
                title.strip(),
                outcome.strip(),
                _json_dump(facts_list),
                _json_dump(concepts_list),
                procedure_name.strip(),
                _json_dump(steps_list),
                _json_dump(deps_list),
                1 if irreversible_flag else 0,
                _json_dump([]),
                domain_norm,
                _json_dump(tags_list),
                _json_dump(meta_obj),
                now,
                now,
                actor,
                actor,
                None,
                None,
                None,
                0,
                None,
            ),
        )
    audit("task", record_id, version, "create", actor)

    # Show edit page with warnings banner
    return RedirectResponse(url=f"/tasks/{record_id}/{version}/edit?created=1", status_code=303)


@router.get("/tasks/{record_id}/{version}", response_class=HTMLResponse)
def task_view(request: Request, record_id: str, version: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(404)

    task = dict(row)
    task["facts"] = _json_load(task["facts_json"])
    task["concepts"] = _json_load(task["concepts_json"])
    task["dependencies"] = _json_load(task["dependencies_json"])

    raw_steps = _json_load(task["steps_json"])
    task["steps"] = _normalize_steps(raw_steps)
    for st in task["steps"]:
        if "actions" not in st or st["actions"] is None:
            st["actions"] = []

    warnings = lint_steps(task["steps"])

    # Surface workflows that reference this exact task version.
    workflows_using: list[dict[str, Any]] = []
    with db() as conn:
        wf_rows = conn.execute(
            """
            SELECT w.record_id, w.version, w.title, w.status
            FROM workflows w
            JOIN workflow_task_refs wr
              ON wr.workflow_record_id = w.record_id
             AND wr.workflow_version = w.version
            WHERE wr.task_record_id=? AND wr.task_version=?
            ORDER BY w.updated_at DESC
            """,
            (record_id, int(version)),
        ).fetchall()
    for wf in wf_rows:
        workflows_using.append(
            {
                "record_id": wf["record_id"],
                "version": int(wf["version"]),
                "title": wf["title"],
                "status": wf["status"],
            }
        )

    # If returned, surface the most recent return note (if any)
    return_note = None
    if task.get("status") == "returned":
        with db() as conn:
            return_note = _fetch_return_note(conn, "task", record_id, version)

    # Override scar: did an admin force-confirm or force-submit this version?
    force_action = None
    with db() as conn:
        force_action = _fetch_force_action(conn, "task", record_id, version)

    return templates.TemplateResponse(
        request,
        "task_view.html",
        {
            "task": task,
            "warnings": warnings,
            "return_note": return_note,
            "workflows_using": workflows_using,
            "force_action": force_action,
        },
    )


@router.get("/tasks/{record_id}/{version}/status")
def task_status(record_id: str, version: int):
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return {"status": row["status"]}


@router.get("/tasks/{record_id}/{version}/edit", response_class=HTMLResponse)
def task_edit_form(request: Request, record_id: str, version: int):
    require(request.state.role, "task:revise")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(404)

    task = dict(row)
    task["facts"] = "\n".join(_json_load(task["facts_json"]) or [])
    task["concepts"] = "\n".join(_json_load(task["concepts_json"]) or [])
    task["dependencies"] = "\n".join(_json_load(task["dependencies_json"]) or [])

    raw_steps = _json_load(task["steps_json"])
    task["steps"] = _normalize_steps(raw_steps)
    for st in task["steps"]:
        if "actions" not in st or st["actions"] is None:
            st["actions"] = []

    warnings = lint_steps(task["steps"])

    with db() as conn:
        domains = _active_domains(conn)

    return templates.TemplateResponse(
        request,
        "task_edit.html",
        {"mode": "edit", "task": task, "warnings": warnings, "domains": domains},
    )


@router.post("/tasks/{record_id}/{version}/save")
def task_save(
    request: Request,
    record_id: str,
    version: int,
    title: str = Form(...),
    outcome: str = Form(...),
    procedure_name: str = Form(...),
    domain: str = Form(""),
    tags: str = Form(""),
    meta: str = Form(""),
    facts: str = Form(""),
    concepts: str = Form(""),
    dependencies: str = Form(""),
    step_text: list[str] = Form([]),
    step_completion: list[str] = Form([]),
    step_actions: list[str] = Form([]),
    step_notes: list[str] = Form([]),
    irreversible_flag: bool = Form(False),
    change_note: str = Form(""),
):
    """Records are immutable.

    Saving changes always creates a NEW VERSION (draft) with a required change_note.
    """
    require(request.state.role, "task:revise")
    actor = request.state.user

    facts_list = parse_lines(facts)
    concepts_list = parse_lines(concepts)
    deps_list = parse_lines(dependencies)
    # Phase 1: tasks are intentionally tagless (workflow-only tags model).
    tags_list: list[str] = []
    meta_obj = parse_meta(meta)
    steps_list = _zip_steps(step_text, step_completion, step_actions, step_notes)
    _validate_steps_required(steps_list)

    note = change_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required when creating a new version")

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)
        if src["status"] == "retired":
            raise HTTPException(409, detail="Cannot revise a retired task without unretiring/replacing policy")

        # If the source version was returned for changes, force the change_note to reference the return note.
        if src["status"] == "returned":
            rn = _fetch_return_note(conn, "task", record_id, version)
            if rn:
                prefix = f"Response to return note by {rn['actor']} at {rn['at']}: {rn['note']} | "
                if prefix not in note:
                    note = prefix + note

        # New version number is latest + 1
        latest_v = get_latest_version(conn, "tasks", record_id) or version
        new_v = latest_v + 1
        now = utc_now_iso()

        conn.execute(
            """
            INSERT INTO tasks(
              record_id, version, status,
              title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
              irreversible_flag, task_assets_json,
              domain,
              tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                new_v,
                "draft",
                title.strip(),
                outcome.strip(),
                _json_dump(facts_list),
                _json_dump(concepts_list),
                procedure_name.strip(),
                _json_dump(steps_list),
                _json_dump(deps_list),
                1 if irreversible_flag else 0,
                src["task_assets_json"],
                (domain or "").strip().lower(),
                _json_dump(tags_list),
                _json_dump(meta_obj),
                now,
                now,
                actor,
                actor,
                None,
                None,
                note,
                int(src["needs_review_flag"]),
                src["needs_review_note"],
            ),
        )

        # Cascade: update workflows referencing previous task version
        _cascade_workflow_updates(conn, record_id, new_v, actor)
        conn.commit()

    audit("task", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}")
    return RedirectResponse(url=f"/tasks/{record_id}/{new_v}", status_code=303)


@router.post("/tasks/{record_id}/{version}/new-version")
def task_new_version(request: Request, record_id: str, version: int):
    require(request.state.role, "task:revise")
    actor = request.state.user
    with db() as conn:
        src = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)
        if src["status"] == "retired":
            raise HTTPException(409, detail="Cannot revise a retired task without unretiring/replacing policy")
        latest_v = get_latest_version(conn, "tasks", record_id) or version
        new_v = latest_v + 1
        now = utc_now_iso()

        conn.execute(
            """
            INSERT INTO tasks(
              record_id, version, status,
              title, outcome, facts_json, concepts_json, procedure_name, steps_json, dependencies_json,
              irreversible_flag, task_assets_json,
              domain,
              tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                new_v,
                "draft",
                src["title"],
                src["outcome"],
                src["facts_json"],
                src["concepts_json"],
                src["procedure_name"],
                src["steps_json"],
                src["dependencies_json"],
                src["irreversible_flag"],
                src["task_assets_json"],
                (src["domain"] if "domain" in src.keys() else ""),
                "[]",  # Phase 1: tasks are tagless.
                (src["meta_json"] if "meta_json" in src.keys() else "{}"),
                now,
                now,
                actor,
                actor,
                None,
                None,
                f"Created new version from v{version}",
                int(src["needs_review_flag"]),
                src["needs_review_note"],
            ),
        )

        # Cascade: update workflows referencing previous task version
        _cascade_workflow_updates(conn, record_id, new_v, actor)
        conn.commit()

    audit("task", record_id, new_v, "new_version", actor, note=f"from v{version}")
    return RedirectResponse(url=f"/tasks/{record_id}/{new_v}/edit", status_code=303)


@router.post("/tasks/{record_id}/{version}/submit")
def task_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "task:submit")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status, domain FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "draft":
            raise HTTPException(409, detail="Only draft tasks can be submitted")

        domain = (row["domain"] or "").strip()
        if not domain:
            raise HTTPException(status_code=409, detail="Cannot submit task: domain is required")
        if not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{domain}'")

        conn.execute(
            "UPDATE tasks SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
    audit("task", record_id, version, "submit", actor)
    return RedirectResponse(url=f"/tasks/{record_id}/{version}", status_code=303)


@router.post("/tasks/{record_id}/{version}/force-submit")
def task_force_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "task:force_submit")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("deprecated", "retired", "confirmed"):
            raise HTTPException(409, detail=f"Cannot force-submit a {row['status']} task")
        conn.execute(
            "UPDATE tasks SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
    audit("task", record_id, version, "force_submit", actor, note="admin forced submission")
    return RedirectResponse(url=f"/tasks/{record_id}/{version}", status_code=303)


@router.post("/tasks/{record_id}/{version}/return")
def task_return_for_changes(
    request: Request,
    record_id: str,
    version: int,
    note: str = Form(""),
    severity: str = Form("warning"),
):
    require(request.state.role, "task:confirm")
    actor = request.state.user
    msg = (note or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Return note is required")
    sev = (severity or "warning").strip().lower()
    if sev not in ("info", "warning", "critical"):
        sev = "warning"
    msg = f"[{sev}] {msg}"

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain, created_by FROM tasks WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted tasks can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot return content they created.")

        # Domain gate (admin implicitly authorized via _user_has_domain)
        domain = (row["domain"] or "").strip()
        if domain and not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{domain}'")

        conn.execute(
            "UPDATE tasks SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("task", record_id, version, "return_for_changes", actor, note=msg)
    return RedirectResponse(url=f"/tasks/{record_id}/{version}", status_code=303)


@router.post("/tasks/{record_id}/{version}/confirm")
def task_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "task:confirm")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status, domain, created_by FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(409, detail="Only submitted tasks can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot confirm content they created.")

        domain = (row["domain"] or "").strip()
        if not domain:
            raise HTTPException(status_code=409, detail="Cannot confirm task: domain is required")
        if not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized to confirm domain '{domain}'")

        # Deprecate any previously confirmed version
        conn.execute(
            "UPDATE tasks SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (utc_now_iso(), actor, record_id),
        )

        conn.execute(
            """
            UPDATE tasks
            SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=?
            WHERE record_id=? AND version=?
            """,
            (utc_now_iso(), actor, utc_now_iso(), actor, record_id, version),
        )

        # Cascade is best-effort: update any pending workflow replacements.
        # Use a savepoint so cascade failures never block the confirm itself.
        try:
            conn.execute("SAVEPOINT before_cascade")
            _cascade_workflow_updates(conn, record_id, version, actor)
            conn.execute("RELEASE before_cascade")
        except Exception:
            conn.execute("ROLLBACK TO before_cascade")

    new_badges = audit("task", record_id, version, "confirm", actor)
    badge_qs = f"?badges={','.join(new_badges)}" if new_badges else ""
    return RedirectResponse(url=f"/tasks/{record_id}/{version}{badge_qs}", status_code=303)


@router.post("/tasks/{record_id}/{version}/force-confirm")
def task_force_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "task:force_confirm")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("deprecated", "retired"):
            raise HTTPException(409, detail=f"Cannot force-confirm a {row['status']} task")

        # Still enforce: you can't confirm an empty/bad record (structure checks are enforced earlier).
        # Admin override is for lifecycle, not semantics.

        # Deprecate any previously confirmed version
        conn.execute(
            "UPDATE tasks SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (utc_now_iso(), actor, record_id),
        )

        conn.execute(
            """
            UPDATE tasks
            SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=?
            WHERE record_id=? AND version=?
            """,
            (utc_now_iso(), actor, utc_now_iso(), actor, record_id, version),
        )

        # Cascade is best-effort.
        try:
            conn.execute("SAVEPOINT before_cascade")
            _cascade_workflow_updates(conn, record_id, version, actor)
            conn.execute("RELEASE before_cascade")
        except Exception:
            conn.execute("ROLLBACK TO before_cascade")

    audit("task", record_id, version, "force_confirm", actor, note="admin forced confirmation")
    return RedirectResponse(url=f"/tasks/{record_id}/{version}", status_code=303)


@router.post("/tasks/{record_id}/{version}/retire")
def task_retire(request: Request, record_id: str, version: int, note: str = Form("")):
    """Retire a task version with no replacement.

    Retired task versions are treated as invalid workflow references and do not auto-cascade.
    Use for intentionally removed capabilities/features.
    """
    require(request.state.role, "task:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("retired", "deprecated"):
            raise HTTPException(409, detail="Task is already retired/superseded")

        domain = (row["domain"] or "").strip()
        if domain and not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{domain}'")

        conn.execute(
            "UPDATE tasks SET status='retired', updated_at=?, updated_by=?, change_note=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, (note or "Retired with no replacement"), record_id, version),
        )

    audit("task", record_id, version, "retire", actor, note=(note or "retired with no replacement"))
    return RedirectResponse(url=f"/tasks/{record_id}/{version}", status_code=303)


@router.post("/tasks/{record_id}/delete")
def task_delete(request: Request, record_id: str):
    """Hard-delete all versions of a task.

    Admins can always delete. Contributors can delete only their own records
    that are still in draft or submitted status.
    """
    actor = request.state.user
    role = request.state.role

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM tasks WHERE record_id=? ORDER BY version DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)

        if role != "admin":
            if row["created_by"] != actor:
                raise HTTPException(status_code=403, detail="You can only delete your own records.")
            if row["status"] not in ("draft", "submitted"):
                raise HTTPException(status_code=403, detail="Only draft or submitted records can be deleted.")

        conn.execute("DELETE FROM tasks WHERE record_id=?", (record_id,))
        audit("task", record_id, 0, "delete", actor, note="hard delete", conn=conn)

    return RedirectResponse(url="/tasks", status_code=303)


def _cascade_workflow_updates(conn: sqlite3.Connection, task_record_id: str, new_task_version: int, actor: str):
    """When a task is revised, cascade to confirmed workflows referencing older versions.

    Rules:
    - Confirmed workflow + new task version → create submitted workflow (or update if exists)
    - While workflow is unconfirmed (submitted/draft) → accumulate task changes (no new version)
    - When workflow confirmed again → new version if more task updates arrive
    """
    # Find all confirmed workflows that reference any version of this task (except the new one)
    workflows_to_update = conn.execute(
        """
        SELECT DISTINCT w.record_id, w.version, w.title, w.status
        FROM workflows w
        JOIN workflow_task_refs wr ON w.record_id = wr.workflow_record_id AND w.version = wr.workflow_version
        WHERE wr.task_record_id = ?
        AND w.status = 'confirmed'
        AND wr.task_version < ?
        """,
        (task_record_id, new_task_version)
    ).fetchall()

    for wf in workflows_to_update:
        wf_record_id = wf["record_id"]
        wf_confirmed_version = wf["version"]

        # Check if there's already a draft version of this workflow
        latest_wf = conn.execute(
            "SELECT MAX(version) as max_v FROM workflows WHERE record_id = ?",
            (wf_record_id,)
        ).fetchone()
        latest_wf_version = latest_wf["max_v"] if latest_wf else wf_confirmed_version

        # Get current refs for the workflow (either draft or confirmed)
        current_refs = conn.execute(
            """
            SELECT task_record_id, task_version, order_index
            FROM workflow_task_refs
            WHERE workflow_record_id = ? AND workflow_version = ?
            ORDER BY order_index
            """,
            (wf_record_id, latest_wf_version)
        ).fetchall()

        # Build updated refs: replace old task version with new one
        updated_refs = []
        for ref in current_refs:
            if ref["task_record_id"] == task_record_id:
                updated_refs.append((task_record_id, new_task_version, ref["order_index"]))
            else:
                updated_refs.append((ref["task_record_id"], ref["task_version"], ref["order_index"]))

        # Check if non-confirmed version already exists
        non_confirmed = conn.execute(
            "SELECT 1 FROM workflows WHERE record_id = ? AND version = ? AND status != 'confirmed'",
            (wf_record_id, latest_wf_version)
        ).fetchone()

        if non_confirmed:
            # Update existing non-confirmed version's task refs
            conn.execute(
                "DELETE FROM workflow_task_refs WHERE workflow_record_id = ? AND workflow_version = ?",
                (wf_record_id, latest_wf_version)
            )
            for ref_record_id, ref_version, order_idx in updated_refs:
                conn.execute(
                    """
                    INSERT INTO workflow_task_refs (workflow_record_id, workflow_version, task_record_id, task_version, order_index)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (wf_record_id, latest_wf_version, ref_record_id, ref_version, order_idx)
                )
            conn.execute(
                "UPDATE workflows SET updated_at = ?, updated_by = ? WHERE record_id = ? AND version = ?",
                (utc_now_iso(), actor, wf_record_id, latest_wf_version)
            )
        else:
            # Create new submitted version
            new_wf_version = latest_wf_version + 1
            now = utc_now_iso()

            # Copy workflow data from confirmed version
            src_wf = conn.execute(
                "SELECT * FROM workflows WHERE record_id = ? AND version = ?",
                (wf_record_id, wf_confirmed_version)
            ).fetchone()

            if src_wf:
                conn.execute(
                    """
                    INSERT INTO workflows (
                        record_id, version, status,
                        title, objective, domains_json, tags_json, meta_json,
                        created_at, updated_at, created_by, updated_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        wf_record_id, new_wf_version, "submitted",
                        src_wf["title"],
                        src_wf["objective"],
                        src_wf["domains_json"],
                        src_wf["tags_json"],
                        src_wf["meta_json"],
                        now, now, actor, actor
                    )
                )

                # Insert updated task refs
                for ref_record_id, ref_version, order_idx in updated_refs:
                    conn.execute(
                        """
                        INSERT INTO workflow_task_refs (workflow_record_id, workflow_version, task_record_id, task_version, order_index)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (wf_record_id, new_wf_version, ref_record_id, ref_version, order_idx)
                    )
