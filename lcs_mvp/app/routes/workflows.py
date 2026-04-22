from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import templates
from ..database import (
    db, utc_now_iso, _active_domains, _user_has_domain, _workflow_domains,
    workflow_readiness, workflow_readiness_detail,
    enforce_workflow_ref_rules,
)
from ..audit import audit, _normalize_domains, _fetch_return_note, _fetch_force_action, get_latest_version
from ..auth import require
from ..utils import _json_dump, _json_load, parse_lines, parse_meta, parse_tags

router = APIRouter()


def _parse_task_refs(task_refs: str) -> list[tuple[str, int]]:
    """Parse newline-separated: record_id@version."""
    refs: list[tuple[str, int]] = []
    for ln in parse_lines(task_refs):
        if "@" not in ln:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid task reference '{ln}'. Use the format record_id@version, one per line.",
            )
        rid, ver = ln.split("@", 1)
        try:
            refs.append((rid.strip(), int(ver.strip())))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid task version in '{ln}'. Use the format record_id@version.",
            )
    if not refs:
        raise HTTPException(status_code=400, detail="Workflow must include at least one confirmed Task reference")
    return refs


@router.get("/workflows", response_class=HTMLResponse)
def workflows_list(request: Request, status: str | None = None, q: str | None = None, domain: str | None = None, tag: str | None = None):
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None
    tag_norm = (tag or "").strip().lower() or None

    with db() as conn:
        sql = "SELECT record_id, MAX(version) AS latest_version FROM workflows GROUP BY record_id ORDER BY record_id"
        rows = conn.execute(sql).fetchall()

        domains = _active_domains(conn)
        all_tags: set[str] = set()

        items = []
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
            if q_norm and q_norm not in (latest["title"] or "").lower():
                continue

            # Derived readiness + domains
            refs = conn.execute(
                "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
                (rid, latest_v),
            ).fetchall()
            pairs = [(x["task_record_id"], int(x["task_version"])) for x in refs]
            readiness = workflow_readiness(conn, pairs)
            doms = _workflow_domains(conn, pairs)

            tags = [str(x).strip().lower() for x in (_json_load(latest["tags_json"]) if "tags_json" in latest.keys() else [])]
            for t in tags:
                if t:
                    all_tags.add(t)

            # Apply filters
            if domain_norm and domain_norm not in set([d.lower() for d in doms]):
                continue
            if tag_norm and tag_norm not in set(tags):
                continue

            # Store domains_json opportunistically (keeps DB queryable and consistent)
            if "domains_json" in latest.keys():
                conn.execute(
                    "UPDATE workflows SET domains_json=? WHERE record_id=? AND version=?",
                    (_json_dump(doms), rid, latest_v),
                )

            # Get latest confirmed version for this workflow
            latest_confirmed = conn.execute(
                "SELECT MAX(version) as max_v FROM workflows WHERE record_id = ? AND status = 'confirmed'",
                (rid,)
            ).fetchone()
            latest_confirmed_v = latest_confirmed["max_v"] if latest_confirmed and latest_confirmed["max_v"] else None

            has_incoming_replacement = bool(latest_confirmed_v and latest_v > int(latest_confirmed_v))
            if has_incoming_replacement:
                if readiness == "awaiting_task_confirmation":
                    replacement_state = "waiting_on_tasks"
                elif latest["status"] == "submitted":
                    replacement_state = "awaiting_workflow_confirmation"
                else:
                    replacement_state = "incoming"
            else:
                replacement_state = None

            items.append(
                {
                    "record_id": rid,
                    "latest_version": latest_v,
                    "title": latest["title"],
                    "status": latest["status"],
                    "readiness": readiness,
                    "domains": doms,
                    "tags": tags,
                    "replacement_state": replacement_state,
                    "needs_review_flag": bool(latest["needs_review_flag"]),
                    "needs_review_note": latest["needs_review_note"],
                }
            )

    return templates.TemplateResponse(
        request,
        "workflows_list.html",
        {"items": items, "status": status, "q": q, "domain": domain_norm or "", "tag": tag_norm or "", "domains": domains, "tags": sorted(all_tags)},
    )


def _confirmed_tasks_json(conn: Any) -> str:
    rows = conn.execute(
        "SELECT record_id, version, title, domain FROM tasks WHERE status='confirmed' ORDER BY title"
    ).fetchall()
    return json.dumps([
        {"record_id": str(r["record_id"]), "version": int(r["version"]),
         "title": str(r["title"]), "domain": str(r["domain"] or "")}
        for r in rows
    ])


@router.get("/workflows/new", response_class=HTMLResponse)
def workflow_new_form(request: Request):
    require(request.state.role, "workflow:create")
    with db() as conn:
        ct_json = _confirmed_tasks_json(conn)
    return templates.TemplateResponse(
        request,
        "workflow_edit.html",
        {"mode": "new", "workflow": None,
         "confirmed_tasks_json": ct_json, "refs_text_json": json.dumps("")},
    )


@router.post("/workflows/new")
def workflow_create(
    request: Request,
    title: str = Form(...),
    objective: str = Form(...),
    tags: str = Form(""),
    meta: str = Form(""),
    task_refs: str = Form(""),
):
    require(request.state.role, "workflow:create")
    actor = request.state.user
    record_id = str(uuid.uuid4())
    version = 1
    now = utc_now_iso()

    refs = _parse_task_refs(task_refs)
    tags_list = parse_tags(tags)
    meta_obj = parse_meta(meta)

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
                record_id,
                version,
                "draft",
                title.strip(),
                objective.strip(),
                _json_dump(_workflow_domains(conn, refs)),
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
        for idx, (rid, ver) in enumerate(refs, start=1):
            conn.execute(
                """
                INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version)
                VALUES (?,?,?,?,?)
                """,
                (record_id, version, idx, rid, ver),
            )

    audit("workflow", record_id, version, "create", actor)
    return RedirectResponse(url=f"/workflows/{record_id}/{version}", status_code=303)


@router.get("/workflows/{record_id}/{version}", response_class=HTMLResponse)
def workflow_view(request: Request, record_id: str, version: int):
    with db() as conn:
        wf = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not wf:
            raise HTTPException(404)

        # Get all versions for this workflow record
        all_versions = conn.execute(
            "SELECT version, status FROM workflows WHERE record_id=? ORDER BY version",
            (record_id,)
        ).fetchall()

        refs = conn.execute(
            """
            SELECT r.order_index, t.record_id, t.version, t.title, t.status as task_status
            FROM workflow_task_refs r
            JOIN tasks t ON t.record_id=r.task_record_id AND t.version=r.task_version
            WHERE r.workflow_record_id=? AND r.workflow_version=?
            ORDER BY r.order_index
            """,
            (record_id, version),
        ).fetchall()

        refs_pairs = [(r["record_id"], int(r["version"])) for r in refs]
        readiness_info = workflow_readiness_detail(conn, refs_pairs)
        doms = _workflow_domains(conn, refs_pairs)

        # Version context for UI messaging
        latest_confirmed = conn.execute(
            "SELECT MAX(version) AS v FROM workflows WHERE record_id=? AND status='confirmed'",
            (record_id,),
        ).fetchone()
        latest_confirmed_version = int(latest_confirmed["v"]) if latest_confirmed and latest_confirmed["v"] else None

        # Diff task refs against last confirmed workflow version to flag updated tasks
        task_version_changes: dict[str, dict[str, int]] = {}
        if latest_confirmed_version and version > latest_confirmed_version:
            old_refs = conn.execute(
                "SELECT task_record_id, task_version FROM workflow_task_refs"
                " WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
                (record_id, latest_confirmed_version),
            ).fetchall()
            old_versions = {r["task_record_id"]: int(r["task_version"]) for r in old_refs}
            for r in refs:
                old_v = old_versions.get(r["record_id"])
                new_v = int(r["version"])
                if old_v is not None and new_v > old_v:
                    task_version_changes[r["record_id"]] = {"old": old_v, "new": new_v}

        # If viewing confirmed version, surface incoming replacement (if any)
        pending_workflow_version = None
        pending_workflow_status = None
        if wf["status"] == "confirmed":
            pending = conn.execute(
                "SELECT version, status FROM workflows WHERE record_id = ? AND version > ? AND status != 'confirmed' ORDER BY version DESC LIMIT 1",
                (record_id, version)
            ).fetchone()
            if pending:
                pending_workflow_version = int(pending["version"])
                pending_workflow_status = str(pending["status"])

        # Override scar: was this version force-confirmed or force-submitted?
        force_action = _fetch_force_action(conn, "workflow", record_id, version)

        # Return note (if this version was returned for changes)
        return_note = None
        if wf["status"] == "returned":
            return_note = _fetch_return_note(conn, "workflow", record_id, version)

    return templates.TemplateResponse(
        request,
        "workflow_view.html",
        {
            "workflow": dict(wf),
            "all_versions": all_versions,
            "refs": refs,
            "readiness": readiness_info["readiness"],
            "readiness_reasons": readiness_info["reasons"],
            "blocking_task_refs": readiness_info["blocking_task_refs"],
            "domains": doms,
            "latest_confirmed_version": latest_confirmed_version,
            "pending_workflow_version": pending_workflow_version,
            "pending_workflow_status": pending_workflow_status,
            "task_version_changes": task_version_changes,
            "force_action": force_action,
            "return_note": return_note,
        },
    )


@router.get("/workflows/{record_id}/{version}/status")
def workflow_status(record_id: str, version: int):
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return {"status": row["status"]}


@router.get("/workflows/{record_id}/{version}/revise", response_class=HTMLResponse)
def workflow_revise_form(request: Request, record_id: str, version: int):
    require(request.state.role, "workflow:revise")
    with db() as conn:
        wf = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not wf:
            raise HTTPException(404)
        refs = conn.execute(
            """
            SELECT r.order_index, r.task_record_id, r.task_version
            FROM workflow_task_refs r
            WHERE r.workflow_record_id=? AND r.workflow_version=?
            ORDER BY r.order_index
            """,
            (record_id, version),
        ).fetchall()
        ct_json = _confirmed_tasks_json(conn)

    refs_text = "\n".join([f"{r['task_record_id']}@{r['task_version']}" for r in refs])
    return templates.TemplateResponse(
        request,
        "workflow_edit.html",
        {
            "mode": "revise",
            "workflow": dict(wf),
            "confirmed_tasks_json": ct_json,
            "refs_text_json": json.dumps(refs_text),
        },
    )


@router.post("/workflows/{record_id}/{version}/revise")
def workflow_revise(
    request: Request,
    record_id: str,
    version: int,
    title: str = Form(...),
    objective: str = Form(...),
    tags: str = Form(""),
    meta: str = Form(""),
    task_refs: str = Form(""),
    change_note: str = Form(""),
):
    require(request.state.role, "workflow:revise")
    actor = request.state.user

    note = change_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required when creating a new version")

    refs = _parse_task_refs(task_refs)
    tags_list = parse_tags(tags)
    meta_obj = parse_meta(meta)

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)

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
                record_id,
                new_v,
                "draft",
                title.strip(),
                objective.strip(),
                _json_dump(_workflow_domains(conn, refs)),
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

        for idx, (rid, ver) in enumerate(refs, start=1):
            conn.execute(
                """
                INSERT INTO workflow_task_refs(workflow_record_id, workflow_version, order_index, task_record_id, task_version)
                VALUES (?,?,?,?,?)
                """,
                (record_id, new_v, idx, rid, ver),
            )

    audit("workflow", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}")
    return RedirectResponse(url=f"/workflows/{record_id}/{new_v}", status_code=303)


@router.post("/workflows/{record_id}/{version}/submit")
def workflow_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "workflow:submit")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "draft":
            raise HTTPException(409, detail="Only draft workflows can be submitted")

        # Author domain gate: you can't submit workflows containing domains you don't hold.
        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        doms = _workflow_domains(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        missing = [d for d in doms if not _user_has_domain(conn, actor, d)]
        if missing:
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for workflow domain(s): {', '.join(missing)}")

        conn.execute(
            "UPDATE workflows SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
    audit("workflow", record_id, version, "submit", actor)
    return RedirectResponse(url=f"/workflows/{record_id}/{version}", status_code=303)


@router.post("/workflows/{record_id}/{version}/force-submit")
def workflow_force_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "workflow:force_submit")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("deprecated", "confirmed"):
            raise HTTPException(409, detail=f"Cannot force-submit a {row['status']} workflow")
        conn.execute(
            "UPDATE workflows SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
    audit("workflow", record_id, version, "force_submit", actor, note="admin forced submission")
    return RedirectResponse(url=f"/workflows/{record_id}/{version}", status_code=303)


@router.post("/workflows/{record_id}/{version}/confirm")
def workflow_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "workflow:confirm")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(409, detail="Only submitted workflows can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot confirm content they created.")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        readiness = workflow_readiness(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        if readiness != "ready":
            raise HTTPException(
                status_code=409,
                detail="Cannot confirm workflow: all referenced Task versions must be confirmed.",
            )

        doms = _workflow_domains(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        missing = [d for d in doms if not _user_has_domain(conn, actor, d)]
        if missing:
            raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized to confirm workflow domain(s): {', '.join(missing)}")

        # Deprecate any previously confirmed version
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

    new_badges = audit("workflow", record_id, version, "confirm", actor)
    badge_qs = f"?badges={','.join(new_badges)}" if new_badges else ""
    return RedirectResponse(url=f"/workflows/{record_id}/{version}{badge_qs}", status_code=303)


@router.post("/workflows/{record_id}/{version}/force-confirm")
def workflow_force_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "workflow:force_confirm")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] == "deprecated":
            raise HTTPException(409, detail="Cannot force-confirm a deprecated workflow")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        readiness = workflow_readiness(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        if readiness != "ready":
            raise HTTPException(
                status_code=409,
                detail="Cannot force-confirm workflow: referenced Task versions must still be confirmed.",
            )

        # Deprecate any previously confirmed version
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
    return RedirectResponse(url=f"/workflows/{record_id}/{version}", status_code=303)


@router.post("/workflows/{record_id}/{version}/return")
def workflow_return_for_changes(
    request: Request,
    record_id: str,
    version: int,
    note: str = Form(""),
    severity: str = Form("warning"),
):
    require(request.state.role, "workflow:confirm")
    actor = request.state.user
    msg = (note or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Return note is required")

    sev = (severity or "warning").strip().lower()
    if sev not in ("info", "warning", "critical"):
        sev = "warning"
    full_note = f"[{sev}] {msg}"

    with db() as conn:
        row = conn.execute(
            "SELECT status, domains_json, created_by FROM workflows WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted workflows can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot return content they created.")

        refs = conn.execute(
            "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()
        doms = _workflow_domains(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        missing = [d for d in doms if not _user_has_domain(conn, actor, d)]
        if missing:
            raise HTTPException(status_code=403, detail=f"Forbidden: not authorized for domain(s): {', '.join(missing)}")

        conn.execute(
            "UPDATE workflows SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("workflow", record_id, version, "return_for_changes", actor, note=full_note)
    return RedirectResponse(url=f"/workflows/{record_id}/{version}", status_code=303)


@router.post("/workflows/{record_id}/{version}/retire")
def workflow_retire(request: Request, record_id: str, version: int, note: str = Form("")):
    """Retire a workflow version with no replacement."""
    require(request.state.role, "workflow:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("retired", "deprecated"):
            raise HTTPException(409, detail="Workflow is already retired/superseded")

        conn.execute(
            "UPDATE workflows SET status='retired', updated_at=?, updated_by=?, change_note=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, (note or "Retired with no replacement"), record_id, version),
        )

    audit("workflow", record_id, version, "retire", actor, note=(note or "retired with no replacement"))
    return RedirectResponse(url=f"/workflows/{record_id}/{version}", status_code=303)


@router.post("/workflows/{record_id}/delete")
def workflow_delete(request: Request, record_id: str):
    """Hard-delete all versions of a workflow.

    Admins can always delete. Contributors can delete only their own records
    that are still in draft or submitted status.
    """
    actor = request.state.user
    role = request.state.role

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM workflows WHERE record_id=? ORDER BY version DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)

        if role != "admin":
            if row["created_by"] != actor:
                raise HTTPException(status_code=403, detail="You can only delete your own records.")
            if row["status"] not in ("draft", "submitted"):
                raise HTTPException(status_code=403, detail="Only draft or submitted records can be deleted.")

        conn.execute("DELETE FROM workflows WHERE record_id=?", (record_id,))
        audit("workflow", record_id, 0, "delete", actor, note="hard delete", conn=conn)

    return RedirectResponse(url="/workflows", status_code=303)
