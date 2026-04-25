"""
JSON REST API for agent/programmatic access to blueprinted.io.

Auth: POST /login with form-encoded credentials (username, password) to obtain a
session cookie (lcs_session). Include that cookie on all subsequent API calls.

The existing HTML routes are the source of truth for governance logic. This layer
reuses the same database writes and audit() calls without reimplementing them.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..database import (
    db, utc_now_iso, _active_domains, _user_has_domain, _user_id,
    _workflow_domains, workflow_readiness, enforce_workflow_ref_rules, _get_llm_config,
)
from ..audit import audit, get_latest_version, _normalize_domains, _fetch_return_note
from ..ingestion import _llm_generate_all_levels
from ..linting import _normalize_steps, _validate_steps_required
from ..auth import require
from ..utils import _json_dump, _json_load
from .tasks import _cascade_workflow_updates
from .assessments import _assessment_domains, _assessment_lint, _assessment_export_dict

router = APIRouter(prefix="/api", tags=["api"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StepIn(BaseModel):
    text: str
    completion: str
    actions: list[str] = []
    notes: str = ""


class TaskCreateBody(BaseModel):
    title: str
    outcome: str
    procedure_name: str
    domain: str = ""
    facts: list[str] = []
    concepts: list[str] = []
    steps: list[StepIn] = []
    dependencies: list[str] = []


class TaskReviseBody(BaseModel):
    title: str
    outcome: str
    procedure_name: str
    domain: str = ""
    facts: list[str] = []
    concepts: list[str] = []
    steps: list[StepIn] = []
    dependencies: list[str] = []
    change_note: str


class TaskRefIn(BaseModel):
    record_id: str
    version: int


class WorkflowCreateBody(BaseModel):
    title: str
    objective: str
    task_refs: list[TaskRefIn]


class WorkflowReviseBody(BaseModel):
    title: str
    objective: str
    task_refs: list[TaskRefIn]
    change_note: str


class AssessmentOptionIn(BaseModel):
    key: str  # A, B, C, or D
    text: str


class AssessmentRefIn(BaseModel):
    ref_type: str  # "task" or "workflow"
    ref_record_id: str
    ref_version: int


class AssessmentCreateBody(BaseModel):
    stem: str
    claim: str = "auto"
    correct_key: str = "A"
    options: list[AssessmentOptionIn]
    rationale: str = ""
    refs: list[AssessmentRefIn] = []


class AssessmentReviseBody(BaseModel):
    stem: str
    claim: str = "auto"
    correct_key: str = "A"
    options: list[AssessmentOptionIn]
    rationale: str = ""
    refs: list[AssessmentRefIn] = []
    change_note: str


class ReturnBody(BaseModel):
    note: str
    severity: str = "warning"


class RetireBody(BaseModel):
    note: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _steps_from_body(steps: list[StepIn]) -> list[dict[str, Any]]:
    return [
        {
            "text": s.text,
            "completion": s.completion,
            "actions": s.actions,
            "notes": s.notes,
        }
        for s in steps
    ]


def _norm_sev(severity: str) -> str:
    """Coerce severity to a valid value; default to 'warning'."""
    sev = (severity or "warning").strip().lower()
    return sev if sev in ("info", "warning", "critical") else "warning"


# ---------------------------------------------------------------------------
# Tasks — viewer / author
# ---------------------------------------------------------------------------

@router.get("/tasks")
def api_tasks_list(
    request: Request,
    status: str | None = None,
    domain: str | None = None,
    q: str | None = None,
):
    """List tasks (latest version per record). Filter by status, domain, or title search."""
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None

    items = []
    with db() as conn:
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS latest_version FROM tasks GROUP BY record_id ORDER BY record_id"
        ).fetchall()
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
            if domain_norm and (latest["domain"] or "").strip().lower() != domain_norm:
                continue
            if q_norm and q_norm not in (latest["title"] or "").lower():
                continue

            items.append({
                "id": rid,
                "version": latest_v,
                "title": latest["title"],
                "status": latest["status"],
                "domain": latest["domain"] or "",
                "outcome": latest["outcome"] or "",
                "author": latest["created_by"] or "",
            })

    return items


@router.get("/tasks/{record_id}/{version}")
def api_task_get(request: Request, record_id: str, version: int):
    """Fetch a specific task version including facts, concepts, steps, and dependencies."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = dict(row)
    steps = _normalize_steps(_json_load(task["steps_json"]))

    return {
        "record_id": task["record_id"],
        "version": task["version"],
        "status": task["status"],
        "title": task["title"],
        "outcome": task["outcome"],
        "procedure_name": task["procedure_name"],
        "domain": task["domain"] or "",
        "facts": _json_load(task["facts_json"]) or [],
        "concepts": _json_load(task["concepts_json"]) or [],
        "steps": steps,
        "dependencies": _json_load(task["dependencies_json"]) or [],
        "created_by": task["created_by"],
        "updated_by": task["updated_by"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


@router.post("/tasks", status_code=201)
def api_task_create(request: Request, body: TaskCreateBody):
    """Create a new task at version 1 in draft status. Requires task:create (author)."""
    require(request.state.role, "task:create")
    actor = request.state.user

    steps_list = _steps_from_body(body.steps)
    _validate_steps_required(steps_list)

    record_id = str(uuid.uuid4())
    version = 1
    now = utc_now_iso()

    with db() as conn:
        domains = _active_domains(conn)
        domain_norm = (body.domain or "").strip().lower()
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
                record_id, version, "draft",
                body.title.strip(), body.outcome.strip(),
                _json_dump(body.facts), _json_dump(body.concepts),
                body.procedure_name.strip(),
                _json_dump(steps_list), _json_dump(body.dependencies),
                0, _json_dump([]),
                domain_norm,
                _json_dump([]), _json_dump({}),
                now, now, actor, actor,
                None, None, None,
                0, None,
            ),
        )

    audit("task", record_id, version, "create", actor)
    return {"record_id": record_id, "version": version, "status": "draft"}


@router.post("/tasks/{record_id}/{version}/revise", status_code=201)
def api_task_revise(request: Request, record_id: str, version: int, body: TaskReviseBody):
    """
    Create a new draft version of a task with revised content. Requires task:revise (author).

    Records are immutable — revision always produces a new version number.
    If the source version was returned for changes, the return note is automatically
    prepended to the change_note for traceability.
    """
    require(request.state.role, "task:revise")
    actor = request.state.user

    steps_list = _steps_from_body(body.steps)
    _validate_steps_required(steps_list)

    note = body.change_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required")

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(status_code=404, detail="Task not found")
        if src["status"] == "retired":
            raise HTTPException(status_code=409, detail="Cannot revise a retired task")

        # Prepend return note context for traceability when revising a returned version.
        if src["status"] == "returned":
            rn = _fetch_return_note(conn, "task", record_id, version)
            if rn:
                prefix = f"Response to return note by {rn['actor']} at {rn['at']}: {rn['note']} | "
                if prefix not in note:
                    note = prefix + note

        domains = _active_domains(conn)
        domain_norm = (body.domain or "").strip().lower()
        if domain_norm and domain_norm not in domains:
            raise HTTPException(status_code=400, detail=f"Invalid domain '{domain_norm}'")

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
                record_id, new_v, "draft",
                body.title.strip(), body.outcome.strip(),
                _json_dump(body.facts), _json_dump(body.concepts),
                body.procedure_name.strip(),
                _json_dump(steps_list), _json_dump(body.dependencies),
                src["irreversible_flag"], src["task_assets_json"],
                domain_norm,
                "[]",
                (src["meta_json"] if "meta_json" in src.keys() else "{}"),
                now, now, actor, actor,
                None, None, note,
                int(src["needs_review_flag"]), src["needs_review_note"],
            ),
        )
        _cascade_workflow_updates(conn, record_id, new_v, actor)
        conn.commit()

    audit("task", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}")
    return {"record_id": record_id, "version": new_v, "status": "draft"}


@router.post("/tasks/{record_id}/{version}/submit")
def api_task_submit(request: Request, record_id: str, version: int):
    """Submit a draft task for review. Requires task:submit (author) and domain membership."""
    require(request.state.role, "task:submit")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="Only draft tasks can be submitted")

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
    return {"record_id": record_id, "version": version, "status": "submitted"}


@router.post("/tasks/{record_id}/{version}/confirm")
def api_task_confirm(request: Request, record_id: str, version: int):
    """
    Confirm a submitted task. Requires task:confirm (reviewer) and domain membership.

    Deprecates any previously confirmed version of the same record, then cascades
    to update confirmed workflows that reference older versions of this task.
    """
    require(request.state.role, "task:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain, created_by FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted tasks can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot confirm content they created.")

        domain = (row["domain"] or "").strip()
        if not domain:
            raise HTTPException(status_code=409, detail="Cannot confirm task: domain is required")
        if not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized to confirm domain '{domain}'")

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

        # Cascade is best-effort; failures must not block the confirm itself.
        try:
            conn.execute("SAVEPOINT before_cascade")
            _cascade_workflow_updates(conn, record_id, version, actor)
            conn.execute("RELEASE before_cascade")
        except Exception:
            conn.execute("ROLLBACK TO before_cascade")

    audit("task", record_id, version, "confirm", actor)
    return {"record_id": record_id, "version": version, "status": "confirmed"}


@router.post("/tasks/{record_id}/{version}/return")
def api_task_return(request: Request, record_id: str, version: int, body: ReturnBody):
    """Return a submitted task to draft with a reviewer note. Requires task:confirm (reviewer)."""
    require(request.state.role, "task:confirm")
    actor = request.state.user

    msg = (body.note or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Return note is required")
    full_note = f"[{_norm_sev(body.severity)}] {msg}"

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain, created_by FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted tasks can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot return content they created.")

        domain = (row["domain"] or "").strip()
        if domain and not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{domain}'")

        conn.execute(
            "UPDATE tasks SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("task", record_id, version, "return_for_changes", actor, note=full_note)
    return {"record_id": record_id, "version": version, "status": "returned"}


@router.post("/tasks/{record_id}/{version}/retire")
def api_task_retire(request: Request, record_id: str, version: int, body: RetireBody):
    """Retire a task with no replacement. Requires task:confirm (reviewer). Retired tasks are invalid workflow refs."""
    require(request.state.role, "task:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] in ("retired", "deprecated"):
            raise HTTPException(status_code=409, detail="Task is already retired/superseded")

        domain = (row["domain"] or "").strip()
        if domain and not _user_has_domain(conn, actor, domain):
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{domain}'")

        note = (body.note or "").strip() or "Retired with no replacement"
        conn.execute(
            "UPDATE tasks SET status='retired', updated_at=?, updated_by=?, change_note=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, note, record_id, version),
        )

    audit("task", record_id, version, "retire", actor, note=note)
    return {"record_id": record_id, "version": version, "status": "retired"}


@router.post("/tasks/{record_id}/{version}/force-submit")
def api_task_force_submit(request: Request, record_id: str, version: int):
    """Admin override: force a task to submitted regardless of current status. Requires task:force_submit (admin)."""
    require(request.state.role, "task:force_submit")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] in ("deprecated", "retired", "confirmed"):
            raise HTTPException(status_code=409, detail=f"Cannot force-submit a {row['status']} task")
        conn.execute(
            "UPDATE tasks SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("task", record_id, version, "force_submit", actor, note="admin forced submission")
    return {"record_id": record_id, "version": version, "status": "submitted"}


@router.post("/tasks/{record_id}/{version}/force-confirm")
def api_task_force_confirm(request: Request, record_id: str, version: int):
    """Admin override: confirm a task bypassing the normal submit gate. Requires task:force_confirm (admin)."""
    require(request.state.role, "task:force_confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        if row["status"] in ("deprecated", "retired"):
            raise HTTPException(status_code=409, detail=f"Cannot force-confirm a {row['status']} task")

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

        try:
            conn.execute("SAVEPOINT before_cascade")
            _cascade_workflow_updates(conn, record_id, version, actor)
            conn.execute("RELEASE before_cascade")
        except Exception:
            conn.execute("ROLLBACK TO before_cascade")

    audit("task", record_id, version, "force_confirm", actor, note="admin forced confirmation")
    return {"record_id": record_id, "version": version, "status": "confirmed"}


# ---------------------------------------------------------------------------
# Workflows — viewer / author
# ---------------------------------------------------------------------------

@router.get("/workflows")
def api_workflows_list(request: Request, status: str | None = None):
    """List workflows (latest version per record) with their task reference lists. Filter by status."""
    items = []
    with db() as conn:
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS latest_version FROM workflows GROUP BY record_id ORDER BY record_id"
        ).fetchall()
        for r in rows:
            rid = r["record_id"]
            latest_v = int(r["latest_version"])
            latest = conn.execute(
                "SELECT * FROM workflows WHERE record_id=? AND version=?", (rid, latest_v)
            ).fetchone()
            if not latest:
                continue
            if status and latest["status"] != status:
                continue

            refs = conn.execute(
                "SELECT task_record_id, task_version FROM workflow_task_refs"
                " WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
                (rid, latest_v),
            ).fetchall()
            task_ids = [
                {"record_id": x["task_record_id"], "version": int(x["task_version"])}
                for x in refs
            ]

            items.append({
                "id": rid,
                "version": latest_v,
                "title": latest["title"],
                "status": latest["status"],
                "task_ids": task_ids,
            })

    return items


@router.post("/workflows", status_code=201)
def api_workflow_create(request: Request, body: WorkflowCreateBody):
    """Create a new workflow at version 1 in draft status. Requires workflow:create (author)."""
    require(request.state.role, "workflow:create")
    actor = request.state.user

    refs = [(ref.record_id, ref.version) for ref in body.task_refs]
    if not refs:
        raise HTTPException(status_code=400, detail="Workflow must include at least one task reference")

    record_id = str(uuid.uuid4())
    version = 1
    now = utc_now_iso()

    with db() as conn:
        enforce_workflow_ref_rules(conn, refs)

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
                record_id, version, "draft",
                body.title.strip(), body.objective.strip(),
                _json_dump(_workflow_domains(conn, refs)),
                _json_dump([]), _json_dump({}),
                now, now, actor, actor,
                None, None, None,
                0, None,
            ),
        )
        for idx, (rid, ver) in enumerate(refs, start=1):
            conn.execute(
                """
                INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version)
                VALUES (?,?,?,?,?)
                """,
                (record_id, version, idx, rid, ver),
            )

    audit("workflow", record_id, version, "create", actor)
    return {"record_id": record_id, "version": version, "status": "draft"}


@router.post("/workflows/{record_id}/{version}/revise", status_code=201)
def api_workflow_revise(request: Request, record_id: str, version: int, body: WorkflowReviseBody):
    """
    Create a new draft version of a workflow with revised content. Requires workflow:revise (author).

    Records are immutable — revision always produces a new version number.
    Workflow domains are re-derived from the new task ref set automatically.
    """
    require(request.state.role, "workflow:revise")
    actor = request.state.user

    note = body.change_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required")

    refs = [(ref.record_id, ref.version) for ref in body.task_refs]
    if not refs:
        raise HTTPException(status_code=400, detail="Workflow must include at least one task reference")

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(status_code=404, detail="Workflow not found")

        enforce_workflow_ref_rules(conn, refs)

        latest_v = get_latest_version(conn, "workflows", record_id) or version
        new_v = latest_v + 1
        now = utc_now_iso()

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
                record_id, new_v, "draft",
                body.title.strip(), body.objective.strip(),
                _json_dump(_workflow_domains(conn, refs)),
                (src["tags_json"] if "tags_json" in src.keys() else "[]"),
                (src["meta_json"] if "meta_json" in src.keys() else "{}"),
                now, now, actor, actor,
                None, None, note,
                int(src["needs_review_flag"]), src["needs_review_note"],
            ),
        )
        for idx, (rid, ver) in enumerate(refs, start=1):
            conn.execute(
                """
                INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version)
                VALUES (?,?,?,?,?)
                """,
                (record_id, new_v, idx, rid, ver),
            )

    audit("workflow", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}")
    return {"record_id": record_id, "version": new_v, "status": "draft"}


@router.post("/workflows/{record_id}/{version}/submit")
def api_workflow_submit(request: Request, record_id: str, version: int):
    """Submit a draft workflow for review. Requires workflow:submit (author) and membership in all workflow domains."""
    require(request.state.role, "workflow:submit")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="Only draft workflows can be submitted")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs"
            " WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        doms = _workflow_domains(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        missing = [d for d in doms if not _user_has_domain(conn, actor, d)]
        if missing:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: you are not authorized for workflow domain(s): {', '.join(missing)}",
            )

        conn.execute(
            "UPDATE workflows SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("workflow", record_id, version, "submit", actor)
    return {"record_id": record_id, "version": version, "status": "submitted"}


@router.post("/workflows/{record_id}/{version}/confirm")
def api_workflow_confirm(request: Request, record_id: str, version: int):
    """
    Confirm a submitted workflow. Requires workflow:confirm (reviewer) and all-domain membership.

    All referenced task versions must already be confirmed (readiness gate).
    Deprecates any previously confirmed version of the same record.
    """
    require(request.state.role, "workflow:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted workflows can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot confirm content they created.")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs"
            " WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        ref_pairs = [(r["task_record_id"], int(r["task_version"])) for r in refs]

        readiness = workflow_readiness(conn, ref_pairs)
        if readiness != "ready":
            raise HTTPException(
                status_code=409,
                detail="Cannot confirm workflow: all referenced task versions must be confirmed.",
            )

        doms = _workflow_domains(conn, ref_pairs)
        missing = [d for d in doms if not _user_has_domain(conn, actor, d)]
        if missing:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: you are not authorized to confirm workflow domain(s): {', '.join(missing)}",
            )

        conn.execute(
            "UPDATE workflows SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (utc_now_iso(), actor, record_id),
        )
        conn.execute(
            """
            UPDATE workflows
            SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=?
            WHERE record_id=? AND version=?
            """,
            (utc_now_iso(), actor, utc_now_iso(), actor, record_id, version),
        )

    audit("workflow", record_id, version, "confirm", actor)
    return {"record_id": record_id, "version": version, "status": "confirmed"}


@router.post("/workflows/{record_id}/{version}/return")
def api_workflow_return(request: Request, record_id: str, version: int, body: ReturnBody):
    """Return a submitted workflow to draft with a reviewer note. Requires workflow:confirm (reviewer)."""
    require(request.state.role, "workflow:confirm")
    actor = request.state.user

    msg = (body.note or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Return note is required")
    full_note = f"[{_norm_sev(body.severity)}] {msg}"

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted workflows can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot return content they created.")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs"
            " WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        doms = _workflow_domains(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        missing = [d for d in doms if not _user_has_domain(conn, actor, d)]
        if missing:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: not authorized for domain(s): {', '.join(missing)}",
            )

        conn.execute(
            "UPDATE workflows SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("workflow", record_id, version, "return_for_changes", actor, note=full_note)
    return {"record_id": record_id, "version": version, "status": "returned"}


@router.post("/workflows/{record_id}/{version}/force-submit")
def api_workflow_force_submit(request: Request, record_id: str, version: int):
    """Admin override: force a workflow to submitted regardless of current status. Requires workflow:force_submit (admin)."""
    require(request.state.role, "workflow:force_submit")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if row["status"] in ("deprecated", "confirmed"):
            raise HTTPException(status_code=409, detail=f"Cannot force-submit a {row['status']} workflow")
        conn.execute(
            "UPDATE workflows SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("workflow", record_id, version, "force_submit", actor, note="admin forced submission")
    return {"record_id": record_id, "version": version, "status": "submitted"}


@router.post("/workflows/{record_id}/{version}/force-confirm")
def api_workflow_force_confirm(request: Request, record_id: str, version: int):
    """
    Admin override: confirm a workflow bypassing the normal submit gate. Requires workflow:force_confirm (admin).

    The referenced task readiness check still applies — all tasks must be confirmed.
    """
    require(request.state.role, "workflow:force_confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if row["status"] == "deprecated":
            raise HTTPException(status_code=409, detail="Cannot force-confirm a deprecated workflow")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs"
            " WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        ref_pairs = [(r["task_record_id"], int(r["task_version"])) for r in refs]
        readiness = workflow_readiness(conn, ref_pairs)
        if readiness != "ready":
            raise HTTPException(
                status_code=409,
                detail="Cannot force-confirm workflow: referenced task versions must still be confirmed.",
            )

        conn.execute(
            "UPDATE workflows SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (utc_now_iso(), actor, record_id),
        )
        conn.execute(
            """
            UPDATE workflows
            SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=?
            WHERE record_id=? AND version=?
            """,
            (utc_now_iso(), actor, utc_now_iso(), actor, record_id, version),
        )

    audit("workflow", record_id, version, "force_confirm", actor, note="admin forced confirmation")
    return {"record_id": record_id, "version": version, "status": "confirmed"}


# ---------------------------------------------------------------------------
# Assessments — assessment_author / reviewer
# ---------------------------------------------------------------------------

@router.get("/assessments")
def api_assessments_list(
    request: Request,
    status: str | None = None,
    domain: str | None = None,
    q: str | None = None,
):
    """List assessment items (latest version per record). Filter by status, domain, or stem search."""
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None

    items = []
    with db() as conn:
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS latest_version FROM assessment_items GROUP BY record_id ORDER BY record_id"
        ).fetchall()
        for r in rows:
            rid = r["record_id"]
            latest_v = int(r["latest_version"])
            latest = conn.execute(
                "SELECT * FROM assessment_items WHERE record_id=? AND version=?", (rid, latest_v)
            ).fetchone()
            if not latest:
                continue
            if status and latest["status"] != status:
                continue
            if q_norm and q_norm not in (latest["stem"] or "").lower():
                continue

            doms = _normalize_domains(latest["domains_json"])
            if domain_norm and domain_norm not in set(doms):
                continue

            items.append({
                "id": rid,
                "version": latest_v,
                "stem": latest["stem"],
                "status": latest["status"],
                "claim": latest["claim"] or "auto",
                "domains": doms,
            })

    return items


@router.get("/assessments/{record_id}/{version}")
def api_assessment_get(request: Request, record_id: str, version: int):
    """Fetch a specific assessment version including options, rationale, refs, and lint findings."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found")
        ref_rows = conn.execute(
            "SELECT ref_type, ref_record_id, ref_version FROM assessment_refs"
            " WHERE assessment_record_id=? AND assessment_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()

    item = _assessment_export_dict(row)
    item["refs"] = [dict(r) for r in ref_rows]
    return item


@router.post("/assessments", status_code=201)
def api_assessment_create(request: Request, body: AssessmentCreateBody):
    """
    Create a new assessment item at version 1 in draft status. Requires assessment:create (assessment_author).

    Lint is computed and stored automatically. Provide exactly 4 options with keys A, B, C, D.
    refs attach the item to task or workflow records, which also determines its domains.
    """
    require(request.state.role, "assessment:create")
    actor = request.state.user

    options = [{"key": o.key.strip().upper(), "text": o.text.strip()} for o in body.options]
    refs = [
        {"ref_type": r.ref_type.strip().lower(), "ref_record_id": r.ref_record_id.strip(), "ref_version": r.ref_version}
        for r in body.refs
    ]

    record_id = str(uuid.uuid4())
    version = 1
    now = utc_now_iso()

    with db() as conn:
        domains = _assessment_domains(conn, refs)
        lint = _assessment_lint(body.stem, options, body.correct_key, body.claim)

        conn.execute(
            """
            INSERT INTO assessment_items(
              record_id, version, status,
              stem, options_json, correct_key, rationale,
              claim, domains_json, lint_json, refs_json,
              tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id, version, "draft",
                body.stem.strip(),
                _json_dump(options),
                body.correct_key.strip().upper(),
                body.rationale.strip(),
                body.claim.strip().lower(),
                _json_dump(domains),
                _json_dump(lint),
                _json_dump(refs),
                _json_dump([]), _json_dump({}),
                now, now, actor, actor,
                None, None, None,
                0, None,
            ),
        )
        for idx, r in enumerate(refs, start=1):
            conn.execute(
                "INSERT INTO assessment_refs(assessment_record_id, assessment_version, order_index, ref_type, ref_record_id, ref_version) VALUES (?,?,?,?,?,?)",
                (record_id, version, idx, r["ref_type"], r["ref_record_id"], int(r["ref_version"])),
            )
        # audit() is called with conn= because we are inside the transaction; opening a second
        # connection here would raise OperationalError: database is locked.
        audit("assessment", record_id, version, "create", actor, conn=conn)

    return {"record_id": record_id, "version": version, "status": "draft"}


@router.post("/assessments/{record_id}/{version}/revise", status_code=201)
def api_assessment_revise(request: Request, record_id: str, version: int, body: AssessmentReviseBody):
    """
    Create a new draft version of an assessment with revised content. Requires assessment:revise (assessment_author).

    Records are immutable — revision always produces a new version number.
    Lint is recomputed for the new version. If the source was returned for changes,
    the return note is prepended to the change_note for traceability.
    """
    require(request.state.role, "assessment:revise")
    actor = request.state.user

    note = body.change_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required")

    options = [{"key": o.key.strip().upper(), "text": o.text.strip()} for o in body.options]
    refs = [
        {"ref_type": r.ref_type.strip().lower(), "ref_record_id": r.ref_record_id.strip(), "ref_version": r.ref_version}
        for r in body.refs
    ]

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(status_code=404, detail="Assessment not found")

        if src["status"] == "returned":
            rn = _fetch_return_note(conn, "assessment", record_id, version)
            if rn:
                prefix = f"Response to return note by {rn['actor']} at {rn['at']}: {rn['note']} | "
                if prefix not in note:
                    note = prefix + note

        latest_v = get_latest_version(conn, "assessment_items", record_id) or version
        new_v = latest_v + 1
        now = utc_now_iso()

        domains = _assessment_domains(conn, refs)
        lint = _assessment_lint(body.stem, options, body.correct_key, body.claim)
        meta_prev = _json_load((src["meta_json"] if "meta_json" in src.keys() else "{}") or "{}") or {}

        conn.execute(
            """
            INSERT INTO assessment_items(
              record_id, version, status,
              stem, options_json, correct_key, rationale,
              claim, domains_json, lint_json, refs_json,
              tags_json, meta_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id, new_v, "draft",
                body.stem.strip(),
                _json_dump(options),
                body.correct_key.strip().upper(),
                body.rationale.strip(),
                body.claim.strip().lower(),
                _json_dump(domains),
                _json_dump(lint),
                _json_dump(refs),
                (src["tags_json"] if "tags_json" in src.keys() else "[]"),
                _json_dump(meta_prev),
                now, now, actor, actor,
                None, None, note,
                int(src["needs_review_flag"]), src["needs_review_note"],
            ),
        )
        for idx, r in enumerate(refs, start=1):
            conn.execute(
                "INSERT INTO assessment_refs(assessment_record_id, assessment_version, order_index, ref_type, ref_record_id, ref_version) VALUES (?,?,?,?,?,?)",
                (record_id, new_v, idx, r["ref_type"], r["ref_record_id"], int(r["ref_version"])),
            )
        audit("assessment", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}", conn=conn)

    return {"record_id": record_id, "version": new_v, "status": "draft"}


@router.post("/assessments/{record_id}/{version}/submit")
def api_assessment_submit(request: Request, record_id: str, version: int):
    """
    Submit a draft assessment for review. Requires assessment:submit (assessment_author).

    Blocked if: no refs are attached (no domain), actor lacks any required domain,
    or the stored lint contains error-level findings.
    """
    require(request.state.role, "assessment:submit")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, domains_json, lint_json FROM assessment_items WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if row["status"] != "draft":
            raise HTTPException(status_code=409, detail="Only draft assessments can be submitted")

        doms = _normalize_domains(row["domains_json"])
        if not doms:
            raise HTTPException(status_code=409, detail="Cannot submit assessment: attach it to at least one task/workflow")
        for d in doms:
            if not _user_has_domain(conn, actor, d):
                raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{d}'")

        lint = _json_load(row["lint_json"]) or []
        for f in lint:
            if (f.get("level") or "").lower() == "error":
                raise HTTPException(status_code=409, detail="Cannot submit assessment: fix lint errors first")

        conn.execute(
            "UPDATE assessment_items SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
        audit("assessment", record_id, version, "submit", actor, conn=conn)

    return {"record_id": record_id, "version": version, "status": "submitted"}


@router.post("/assessments/{record_id}/{version}/return")
def api_assessment_return(request: Request, record_id: str, version: int, body: ReturnBody):
    """Return a submitted assessment to draft with a reviewer note. Requires assessment:confirm (reviewer)."""
    require(request.state.role, "assessment:confirm")
    actor = request.state.user

    msg = (body.note or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Return note is required")
    full_note = f"[{_norm_sev(body.severity)}] {msg}"

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted assessments can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot return content they created.")

        conn.execute(
            "UPDATE assessment_items SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
        audit("assessment", record_id, version, "return_for_changes", actor, note=full_note, conn=conn)

    return {"record_id": record_id, "version": version, "status": "returned"}


@router.post("/assessments/{record_id}/{version}/confirm")
def api_assessment_confirm(request: Request, record_id: str, version: int):
    """Confirm a submitted assessment. Requires assessment:confirm (contributor). Deprecates the previously confirmed version."""
    require(request.state.role, "assessment:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted assessments can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot confirm content they created.")

        conn.execute(
            "UPDATE assessment_items SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (utc_now_iso(), actor, record_id),
        )
        conn.execute(
            "UPDATE assessment_items SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, utc_now_iso(), actor, record_id, version),
        )
        audit("assessment", record_id, version, "confirm", actor, conn=conn)

    return {"record_id": record_id, "version": version, "status": "confirmed"}


# ---------------------------------------------------------------------------
# Review queue — contributor / admin
# ---------------------------------------------------------------------------

@router.get("/review")
def api_review_queue(request: Request, item_type: str = ""):
    """
    Return all submitted tasks, workflows, and assessments the caller is authorized to review.

    Scope is determined by the caller's domain memberships (contributor) or all active domains (admin).
    Optionally filter by item_type: 'task', 'workflow', or 'assessment'.
    """
    if request.state.role not in ("contributor", "admin"):
        raise HTTPException(status_code=403, detail="Forbidden: contributor/admin only")

    filter_type = (item_type or "").strip().lower()
    if filter_type not in ("task", "workflow", "assessment"):
        filter_type = ""

    items: list[dict[str, Any]] = []

    with db() as conn:
        if request.state.role == "admin":
            doms = _active_domains(conn)
        else:
            uid = _user_id(conn, request.state.user)
            dom_rows = conn.execute(
                "SELECT domain FROM user_domains WHERE user_id=?", (uid,)
            ).fetchall() if uid else []
            doms = [str(r["domain"]) for r in dom_rows]

        if doms:
            dset = {d.strip().lower() for d in doms if d}
            qmarks = ",".join(["?"] * len(doms))

            if not filter_type or filter_type == "task":
                t_rows = conn.execute(
                    f"SELECT record_id, version, title, status, domain, created_at FROM tasks"
                    f" WHERE status='submitted' AND domain IN ({qmarks})",
                    doms,
                ).fetchall()
                for r in t_rows:
                    items.append({
                        "type": "task",
                        "record_id": r["record_id"],
                        "version": int(r["version"]),
                        "title": r["title"],
                        "status": r["status"],
                        "domains": [str(r["domain"])],
                        "created_at": str(r["created_at"] or ""),
                    })

            if not filter_type or filter_type == "workflow":
                w_rows = conn.execute(
                    "SELECT record_id, version, title, status, domains_json, created_at FROM workflows WHERE status='submitted'"
                ).fetchall()
                for r in w_rows:
                    wdoms = _normalize_domains(r["domains_json"])
                    if dset.intersection(wdoms):
                        items.append({
                            "type": "workflow",
                            "record_id": r["record_id"],
                            "version": int(r["version"]),
                            "title": r["title"],
                            "status": r["status"],
                            "domains": wdoms,
                            "created_at": str(r["created_at"] or ""),
                        })

            if not filter_type or filter_type == "assessment":
                a_rows = conn.execute(
                    "SELECT record_id, version, stem, status, domains_json, created_at FROM assessment_items WHERE status='submitted'"
                ).fetchall()
                for r in a_rows:
                    adoms = _normalize_domains(r["domains_json"])
                    if dset.intersection(adoms):
                        items.append({
                            "type": "assessment",
                            "record_id": r["record_id"],
                            "version": int(r["version"]),
                            "title": str(r["stem"])[:80],
                            "status": r["status"],
                            "domains": adoms,
                            "created_at": str(r["created_at"] or ""),
                        })

    items.sort(key=lambda it: (
        str(it.get("created_at") or ""),
        str(it.get("type") or ""),
        str(it.get("title") or "").lower(),
    ))
    return {"items": items, "authorized_domains": doms}


# ---------------------------------------------------------------------------
# Delivery — content_publisher
# ---------------------------------------------------------------------------

@router.get("/delivery")
def api_delivery(
    request: Request,
    q: str | None = None,
    domain: str | None = None,
):
    """List confirmed workflows available for delivery. Requires delivery:view (content_publisher and above)."""
    require(request.state.role, "delivery:view")

    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None

    workflows = []
    with db() as conn:
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS v FROM workflows WHERE status='confirmed' GROUP BY record_id ORDER BY record_id"
        ).fetchall()
        for r in rows:
            rid = r["record_id"]
            v = int(r["v"])
            wf = conn.execute(
                "SELECT record_id, version, title, tags_json, domains_json FROM workflows WHERE record_id=? AND version=?",
                (rid, v),
            ).fetchone()
            if not wf:
                continue

            title = str(wf["title"] or "")
            if q_norm and q_norm not in title.lower():
                continue

            wf_domains = _normalize_domains(wf["domains_json"])
            if domain_norm and domain_norm not in set(wf_domains):
                continue

            wf_tags = [
                str(x).strip().lower()
                for x in (_json_load(wf["tags_json"]) or [])
                if str(x).strip()
            ]

            workflows.append({
                "record_id": wf["record_id"],
                "version": v,
                "title": title,
                "domains": wf_domains,
                "tags": wf_tags,
            })

    workflows.sort(key=lambda w: (w.get("title") or ""))
    return {"workflows": workflows}


# ---------------------------------------------------------------------------
# Audit log — audit role
# ---------------------------------------------------------------------------

@router.get("/audit")
def api_audit_log(
    request: Request,
    entity_type: str | None = None,
    record_id: str | None = None,
    limit: int = 200,
):
    """Query the audit log. Requires audit:view (audit/admin). Max limit 1000."""
    require(request.state.role, "audit:view")

    entity_type = (entity_type or "").strip() or None
    record_id = (record_id or "").strip() or None
    limit = max(1, min(int(limit or 200), 1000))

    where: list[str] = []
    params: list[Any] = []
    if entity_type:
        where.append("entity_type=?")
        params.append(entity_type)
    if record_id:
        where.append("record_id=?")
        params.append(record_id)

    sql = "SELECT id, entity_type, record_id, version, action, actor, at, note FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY at DESC, id DESC LIMIT ?"
    params.append(limit)

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {"entries": [dict(r) for r in rows], "count": len(rows)}


# ---------------------------------------------------------------------------
# DB state snapshot — all roles
# ---------------------------------------------------------------------------

@router.get("/db/state")
def api_db_state(request: Request):
    """
    Return a full database state snapshot in one call.

    Useful for agents to orient themselves before issuing writes: task and workflow
    counts by status, plus the full list of confirmed tasks with their domains.
    """
    with db() as conn:
        task_counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        wf_counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM workflows GROUP BY status"
        ).fetchall()
        confirmed_tasks = conn.execute(
            "SELECT record_id, version, title, domain FROM tasks WHERE status='confirmed' ORDER BY title"
        ).fetchall()

    return {
        "tasks": {r["status"]: r["n"] for r in task_counts},
        "workflows": {r["status"]: r["n"] for r in wf_counts},
        "confirmed_tasks": [
            {
                "id": r["record_id"],
                "version": r["version"],
                "title": r["title"],
                "domain": r["domain"] or "",
            }
            for r in confirmed_tasks
        ],
    }


# ---------------------------------------------------------------------------
# Primers API
# ---------------------------------------------------------------------------

class PrimerCreateBody(BaseModel):
    title: str
    summary: str
    explanation: str
    analogies: str = ""
    domain: str = ""


class PrimerReviseBody(BaseModel):
    title: str
    summary: str
    explanation: str
    analogies: str = ""
    domain: str = ""
    change_note: str


class PrimerReturnBody(BaseModel):
    note: str
    severity: str = "warning"


def _primer_row_to_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["media"] = _json_load(d.pop("media_json", None)) or []
    d["levels"] = _json_load(d.pop("levels_json", None)) or {}
    return d


@router.get("/primers")
def api_primers_list(
    request: Request,
    status: str | None = None,
    domain: str | None = None,
    q: str | None = None,
):
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None

    with db() as conn:
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS latest_version FROM primers GROUP BY record_id ORDER BY record_id"
        ).fetchall()
        items = []
        for r in rows:
            rid = r["record_id"]
            latest_v = int(r["latest_version"])
            p = conn.execute(
                "SELECT record_id, version, status, title, summary, domain, levels_json FROM primers WHERE record_id=? AND version=?",
                (rid, latest_v),
            ).fetchone()
            if not p:
                continue
            if status and p["status"] != status:
                continue
            if domain_norm and (p["domain"] or "").strip().lower() != domain_norm:
                continue
            if q_norm and q_norm not in (p["title"] or "").lower():
                continue
            items.append({
                "record_id": p["record_id"],
                "version": int(p["version"]),
                "status": p["status"],
                "title": p["title"],
                "summary": p["summary"],
                "domain": p["domain"] or "",
                "has_levels": bool(p["levels_json"]),
            })
    return {"primers": items}


@router.get("/primers/{record_id}/{version}")
def api_primer_detail(request: Request, record_id: str, version: int):
    with db() as conn:
        p = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not p:
            raise HTTPException(404)
        return _primer_row_to_dict(p)


@router.post("/primers", status_code=201)
def api_primer_create(request: Request, body: PrimerCreateBody):
    require(request.state.role, "primer:create")
    actor = request.state.user
    record_id = str(uuid.uuid4())
    now = utc_now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO primers(
              record_id, version, status, title, summary, explanation, analogies,
              media_json, domain,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note, needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id, 1, "draft",
                body.title.strip(), body.summary.strip(), body.explanation.strip(),
                body.analogies.strip() or None, "[]", body.domain.strip().lower() or "",
                now, now, actor, actor, None, None, None, 0, None,
            ),
        )
        audit("primer", record_id, 1, "create", actor, conn=conn)
    return {"record_id": record_id, "version": 1, "status": "draft"}


@router.post("/primers/{record_id}/{version}/revise", status_code=201)
def api_primer_revise(request: Request, record_id: str, version: int, body: PrimerReviseBody):
    require(request.state.role, "primer:revise")
    actor = request.state.user
    note = (body.change_note or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required")
    now = utc_now_iso()
    with db() as conn:
        src = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)
        levels_json_val = src["levels_json"] if "levels_json" in src.keys() else None
        latest_v = get_latest_version(conn, "primers", record_id) or version
        new_v = latest_v + 1
        conn.execute(
            """
            INSERT INTO primers(
              record_id, version, status, title, summary, explanation, analogies,
              media_json, domain, levels_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note, needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id, new_v, "draft",
                body.title.strip(), body.summary.strip(), body.explanation.strip(),
                body.analogies.strip() or None, src["media_json"] or "[]",
                body.domain.strip().lower() or "", levels_json_val,
                now, now, actor, actor, None, None, note,
                int(src["needs_review_flag"]), src["needs_review_note"],
            ),
        )
        audit("primer", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}", conn=conn)
    return {"record_id": record_id, "version": new_v, "status": "draft"}


@router.post("/primers/{record_id}/{version}/generate-all-levels", status_code=200)
def api_primer_generate_all_levels(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:create")
    actor = request.state.user
    with db() as conn:
        src = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)
        cfg = _get_llm_config(conn)
    levels = _llm_generate_all_levels(src["explanation"], src["title"], cfg)
    with db() as conn:
        conn.execute(
            "UPDATE primers SET levels_json=?, updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (_json_dump(levels), utc_now_iso(), actor, record_id, version),
        )
        audit("primer", record_id, version, "generate_levels", actor, conn=conn)
    return {"record_id": record_id, "version": version, "levels": levels}


@router.post("/primers/{record_id}/{version}/submit")
def api_primer_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:submit")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status, domain FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "draft":
            raise HTTPException(409, detail="Only draft primers can be submitted")
        if not row["domain"]:
            raise HTTPException(400, detail="A domain must be set before submitting")
        if not _user_has_domain(conn, actor, row["domain"]):
            raise HTTPException(403, detail=f"You are not authorised for domain '{row['domain']}'")
        conn.execute(
            "UPDATE primers SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
        audit("primer", record_id, version, "submit", actor, conn=conn)
    return {"status": "submitted"}


@router.post("/primers/{record_id}/{version}/confirm")
def api_primer_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:confirm")
    actor = request.state.user
    now = utc_now_iso()
    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by, domain FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(409, detail="Only submitted primers can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(403, detail="Contributors cannot confirm content they created.")
        if row["domain"] and not _user_has_domain(conn, actor, row["domain"]):
            raise HTTPException(403, detail=f"Not authorised for domain '{row['domain']}'")
        conn.execute(
            "UPDATE primers SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (now, actor, record_id),
        )
        conn.execute(
            "UPDATE primers SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (now, actor, now, actor, record_id, version),
        )
        audit("primer", record_id, version, "confirm", actor, conn=conn)
    return {"status": "confirmed"}


@router.post("/primers/{record_id}/{version}/return")
def api_primer_return(request: Request, record_id: str, version: int, body: PrimerReturnBody):
    require(request.state.role, "primer:confirm")
    actor = request.state.user
    msg = (body.note or "").strip()
    if not msg:
        raise HTTPException(400, detail="Return note is required")
    sev = (body.severity or "warning").lower()
    if sev not in ("info", "warning", "critical"):
        sev = "warning"
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(409, detail="Only submitted primers can be returned")
        conn.execute(
            "UPDATE primers SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
        audit("primer", record_id, version, "return_for_changes", actor, note=f"[{sev}] {msg}", conn=conn)
    return {"status": "returned"}


@router.get("/workflows/{record_id}/primers")
def api_workflow_primers(request: Request, record_id: str):
    with db() as conn:
        wf = conn.execute("SELECT record_id FROM workflows WHERE record_id=? LIMIT 1", (record_id,)).fetchone()
        if not wf:
            raise HTTPException(404)
        rows = conn.execute(
            """
            SELECT p.record_id, p.title, p.summary, p.status, p.version, p.domain
            FROM workflow_primer_refs wpr
            JOIN primers p ON p.record_id = wpr.primer_record_id
            WHERE wpr.workflow_record_id = ?
              AND p.version = (SELECT MAX(p2.version) FROM primers p2 WHERE p2.record_id = p.record_id AND p2.status = 'confirmed')
            ORDER BY p.title
            """,
            (record_id,),
        ).fetchall()
    return {"primers": [dict(r) for r in rows]}


class WorkflowPrimerAttachBody(BaseModel):
    primer_record_id: str


@router.post("/workflows/{record_id}/primers")
def api_workflow_attach_primer(request: Request, record_id: str, body: WorkflowPrimerAttachBody):
    require(request.state.role, "workflow:revise")
    actor = request.state.user
    with db() as conn:
        wf = conn.execute("SELECT record_id FROM workflows WHERE record_id=? LIMIT 1", (record_id,)).fetchone()
        if not wf:
            raise HTTPException(404)
        confirmed = conn.execute(
            "SELECT record_id FROM primers WHERE record_id=? AND status='confirmed' LIMIT 1",
            (body.primer_record_id,),
        ).fetchone()
        if not confirmed:
            raise HTTPException(400, detail="Primer must be confirmed before attaching")
        conn.execute(
            "INSERT OR IGNORE INTO workflow_primer_refs(workflow_record_id, primer_record_id, attached_at, attached_by) VALUES (?,?,?,?)",
            (record_id, body.primer_record_id, utc_now_iso(), actor),
        )
    return {"attached": True}


@router.delete("/workflows/{record_id}/primers/{primer_record_id}")
def api_workflow_detach_primer(request: Request, record_id: str, primer_record_id: str):
    require(request.state.role, "workflow:revise")
    with db() as conn:
        conn.execute(
            "DELETE FROM workflow_primer_refs WHERE workflow_record_id=? AND primer_record_id=?",
            (record_id, primer_record_id),
        )
    return {"detached": True}
