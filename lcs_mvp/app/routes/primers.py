from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import templates
from ..database import db, utc_now_iso, _active_domains, _user_has_domain, _get_llm_config
from ..audit import audit, _fetch_return_note, _fetch_force_action, get_latest_version
from ..auth import can, require
from ..ingestion import _llm_generate_all_levels
from ..utils import _json_dump, _json_load

router = APIRouter()


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("/primers", response_class=HTMLResponse)
def primers_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    domain: str | None = None,
):
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None

    with db() as conn:
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS latest_version FROM primers GROUP BY record_id ORDER BY record_id"
        ).fetchall()
        domains = _active_domains(conn)

        items = []
        for r in rows:
            rid = r["record_id"]
            latest_v = int(r["latest_version"])
            latest = conn.execute(
                "SELECT * FROM primers WHERE record_id=? AND version=?", (rid, latest_v)
            ).fetchone()
            if not latest:
                continue
            if status and latest["status"] != status:
                continue
            if q_norm and q_norm not in (latest["title"] or "").lower():
                continue
            domain_val = (latest["domain"] or "").strip()
            if domain_norm and domain_val.lower() != domain_norm:
                continue

            workflow_count = conn.execute(
                "SELECT COUNT(*) AS n FROM workflow_primer_refs WHERE primer_record_id=?", (rid,)
            ).fetchone()["n"]

            has_return_note = False
            if latest["status"] == "returned":
                rn = conn.execute(
                    "SELECT 1 FROM audit_log WHERE entity_type='primer' AND record_id=? AND version=? AND action='return_for_changes' LIMIT 1",
                    (rid, latest_v),
                ).fetchone()
                has_return_note = bool(rn)

            has_levels = bool(latest["levels_json"]) if "levels_json" in latest.keys() else False

            items.append({
                "record_id": rid,
                "latest_version": latest_v,
                "title": latest["title"],
                "summary": latest["summary"],
                "status": latest["status"],
                "domain": domain_val,
                "has_levels": has_levels,
                "needs_review_flag": bool(latest["needs_review_flag"]),
                "workflow_count": int(workflow_count),
                "has_return_note": has_return_note,
            })

    return templates.TemplateResponse(
        request,
        "primers_list.html",
        {
            "items": items,
            "status": status,
            "q": q,
            "domain": domain_norm or "",
            "domains": domains,
        },
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.get("/primers/new", response_class=HTMLResponse)
def primer_new_form(request: Request):
    require(request.state.role, "primer:create")
    with db() as conn:
        domains = _active_domains(conn)
    return templates.TemplateResponse(
        request,
        "primer_edit.html",
        {"mode": "new", "primer": None, "warnings": [], "domains": domains},
    )


@router.post("/primers/new")
def primer_create(
    request: Request,
    title: str = Form(...),
    summary: str = Form(...),
    explanation: str = Form(...),
    analogies: str = Form(""),
    domain: str = Form(""),
):
    require(request.state.role, "primer:create")
    actor = request.state.user
    record_id = str(uuid.uuid4())
    now = utc_now_iso()

    with db() as conn:
        domains = _active_domains(conn)
        domain_norm = (domain or "").strip().lower()
        if domain_norm and domain_norm not in domains:
            raise HTTPException(status_code=400, detail=f"Invalid domain '{domain_norm}'")

        conn.execute(
            """INSERT INTO primers(
              record_id, version, status,
              title, summary, explanation, analogies, media_json,
              domain,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id, 1, "draft",
                title.strip(), summary.strip(), explanation.strip(),
                analogies.strip() or None, "[]",
                domain_norm,
                now, now, actor, actor,
                None, None, None,
                0, None,
            ),
        )

    audit("primer", record_id, 1, "create", actor)
    return RedirectResponse(url=f"/primers/{record_id}/1", status_code=303)


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

@router.get("/primers/{record_id}/{version}", response_class=HTMLResponse)
def primer_view(request: Request, record_id: str, version: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)

        primer: dict[str, Any] = dict(row)
        primer["media"] = _json_load(primer.get("media_json") or "[]") or []

        return_note = None
        if primer["status"] == "returned":
            return_note = _fetch_return_note(conn, "primer", record_id, version)

        force_action = _fetch_force_action(conn, "primer", record_id, version)

        # Workflows that have this primer attached
        wf_rows = conn.execute(
            """SELECT w.record_id, w.title, w.version, w.status
               FROM workflow_primer_refs wpr
               JOIN workflows w ON w.record_id = wpr.workflow_record_id
               WHERE wpr.primer_record_id = ?
               AND w.version = (SELECT MAX(w2.version) FROM workflows w2 WHERE w2.record_id = w.record_id)""",
            (record_id,),
        ).fetchall()
        workflows_using = [dict(r) for r in wf_rows]

        # Version history
        all_versions = conn.execute(
            "SELECT version, status FROM primers WHERE record_id=? ORDER BY version DESC",
            (record_id,),
        ).fetchall()

    levels_raw = _json_load(primer.get("levels_json") or "{}")
    levels = levels_raw if isinstance(levels_raw, dict) else {}

    return templates.TemplateResponse(
        request,
        "primer_view.html",
        {
            "primer": primer,
            "return_note": return_note,
            "force_action": force_action,
            "workflows_using": workflows_using,
            "all_versions": [dict(r) for r in all_versions],
            "levels": levels,
        },
    )


# ---------------------------------------------------------------------------
# Edit / Save (new version)
# ---------------------------------------------------------------------------

@router.get("/primers/{record_id}/{version}/edit", response_class=HTMLResponse)
def primer_edit_form(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:revise")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        domains = _active_domains(conn)

    return templates.TemplateResponse(
        request,
        "primer_edit.html",
        {"mode": "edit", "primer": dict(row), "warnings": [], "domains": domains},
    )


@router.post("/primers/{record_id}/{version}/save")
def primer_save(
    request: Request,
    record_id: str,
    version: int,
    title: str = Form(...),
    summary: str = Form(...),
    explanation: str = Form(...),
    analogies: str = Form(""),
    domain: str = Form(""),
    change_note: str = Form(""),
):
    require(request.state.role, "primer:revise")
    actor = request.state.user
    note = change_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required when creating a new version")

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)

        levels_json_val = src["levels_json"] if "levels_json" in src.keys() else None

        domains = _active_domains(conn)
        domain_norm = (domain or "").strip().lower()
        if domain_norm and domain_norm not in domains:
            raise HTTPException(status_code=400, detail=f"Invalid domain '{domain_norm}'")

        if src["status"] == "returned":
            rn = _fetch_return_note(conn, "primer", record_id, version)
            if rn:
                prefix = f"Response to return note by {rn['actor']} at {rn['at']}: {rn['note']} | "
                if prefix not in note:
                    note = prefix + note

        latest_v = get_latest_version(conn, "primers", record_id) or version
        new_v = latest_v + 1
        now = utc_now_iso()

        conn.execute(
            """INSERT INTO primers(
              record_id, version, status,
              title, summary, explanation, analogies, media_json,
              domain, levels_json,
              created_at, updated_at, created_by, updated_by,
              reviewed_at, reviewed_by, change_note,
              needs_review_flag, needs_review_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id, new_v, "draft",
                title.strip(), summary.strip(), explanation.strip(),
                analogies.strip() or None,
                src["media_json"] or "[]",
                domain_norm, levels_json_val,
                now, now, actor, actor,
                None, None, note,
                int(src["needs_review_flag"]),
                src["needs_review_note"],
            ),
        )

    audit("primer", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}")
    return RedirectResponse(url=f"/primers/{record_id}/{new_v}", status_code=303)


# ---------------------------------------------------------------------------
# Submit / Return / Confirm / Force actions
# ---------------------------------------------------------------------------

@router.post("/primers/{record_id}/{version}/submit")
def primer_submit(request: Request, record_id: str, version: int):
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
        domain = (row["domain"] or "").strip()
        if not domain:
            raise HTTPException(409, detail="Cannot submit primer: domain is required")
        if not _user_has_domain(conn, actor, domain):
            raise HTTPException(403, detail=f"Forbidden: you are not authorized for domain '{domain}'")
        conn.execute(
            "UPDATE primers SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
    audit("primer", record_id, version, "submit", actor)
    return RedirectResponse(url=f"/primers/{record_id}/{version}", status_code=303)


@router.post("/primers/{record_id}/{version}/force-submit")
def primer_force_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:force_submit")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("deprecated", "confirmed"):
            raise HTTPException(409, detail=f"Cannot force-submit a {row['status']} primer")
        conn.execute(
            "UPDATE primers SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
    audit("primer", record_id, version, "force_submit", actor, note="admin forced submission")
    return RedirectResponse(url=f"/primers/{record_id}/{version}", status_code=303)


@router.post("/primers/{record_id}/{version}/return")
def primer_return_for_changes(
    request: Request,
    record_id: str,
    version: int,
    note: str = Form(""),
    severity: str = Form("warning"),
):
    require(request.state.role, "primer:confirm")
    actor = request.state.user
    msg = (note or "").strip()
    if not msg:
        raise HTTPException(400, detail="Return note is required")
    sev = (severity or "warning").strip().lower()
    if sev not in ("info", "warning", "critical"):
        sev = "warning"

    with db() as conn:
        row = conn.execute(
            "SELECT status, domain, created_by FROM primers WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(409, detail="Only submitted primers can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(403, detail="Contributors cannot return content they created.")
        domain = (row["domain"] or "").strip()
        if domain and not _user_has_domain(conn, actor, domain):
            raise HTTPException(403, detail=f"Forbidden: you are not authorized for domain '{domain}'")
        conn.execute(
            "UPDATE primers SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )

    audit("primer", record_id, version, "return_for_changes", actor, note=f"[{sev}] {msg}")
    return RedirectResponse(url=f"/primers/{record_id}/{version}", status_code=303)


@router.post("/primers/{record_id}/{version}/confirm")
def primer_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:confirm")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status, domain, created_by FROM primers WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(409, detail="Only submitted primers can be confirmed")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(403, detail="Contributors cannot confirm content they created.")
        domain = (row["domain"] or "").strip()
        if not domain:
            raise HTTPException(409, detail="Cannot confirm primer: domain is required")
        if not _user_has_domain(conn, actor, domain):
            raise HTTPException(403, detail=f"Forbidden: you are not authorized to confirm domain '{domain}'")

        now = utc_now_iso()
        conn.execute(
            "UPDATE primers SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (now, actor, record_id),
        )
        conn.execute(
            "UPDATE primers SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (now, actor, now, actor, record_id, version),
        )

        # Soft-signal attached workflows that their Primer has been updated
        try:
            wf_ids = [r["workflow_record_id"] for r in conn.execute(
                "SELECT workflow_record_id FROM workflow_primer_refs WHERE primer_record_id=?", (record_id,)
            ).fetchall()]
            for wf_rid in wf_ids:
                latest_wf = conn.execute(
                    "SELECT record_id, MAX(version) AS v, status FROM workflows WHERE record_id=?", (wf_rid,)
                ).fetchone()
                if latest_wf and latest_wf["status"] in ("draft", "submitted"):
                    conn.execute(
                        "UPDATE workflows SET needs_review_flag=1, needs_review_note=? WHERE record_id=? AND version=?",
                        ("Attached Primer updated — review may be warranted", wf_rid, int(latest_wf["v"])),
                    )
        except Exception:
            pass

    audit("primer", record_id, version, "confirm", actor)
    return RedirectResponse(url=f"/primers/{record_id}/{version}", status_code=303)


@router.post("/primers/{record_id}/{version}/force-confirm")
def primer_force_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:force_confirm")
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] in ("deprecated",):
            raise HTTPException(409, detail=f"Cannot force-confirm a {row['status']} primer")
        now = utc_now_iso()
        conn.execute(
            "UPDATE primers SET status='deprecated', updated_at=?, updated_by=? WHERE record_id=? AND status='confirmed'",
            (now, actor, record_id),
        )
        conn.execute(
            "UPDATE primers SET status='confirmed', reviewed_at=?, reviewed_by=?, updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (now, actor, now, actor, record_id, version),
        )
    audit("primer", record_id, version, "force_confirm", actor, note="admin forced confirmation")
    return RedirectResponse(url=f"/primers/{record_id}/{version}", status_code=303)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@router.post("/primers/{record_id}/{version}/generate-all-levels")
def primer_generate_all_levels(request: Request, record_id: str, version: int):
    require(request.state.role, "primer:create")
    actor = request.state.user

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM primers WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)
        if src["status"] not in ("draft", "returned"):
            raise HTTPException(409, detail="Levels can only be generated for draft or returned primers")
        cfg = _get_llm_config(conn)

    levels = _llm_generate_all_levels(src["explanation"], src["title"], cfg)

    with db() as conn:
        conn.execute(
            "UPDATE primers SET levels_json=?, updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (_json_dump(levels), utc_now_iso(), actor, record_id, version),
        )
    audit("primer", record_id, version, "generate_levels", actor)
    return RedirectResponse(url=f"/primers/{record_id}/{version}", status_code=303)


@router.post("/primers/{record_id}/delete")
def primer_delete(request: Request, record_id: str):
    actor = request.state.user
    role = request.state.role

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM primers WHERE record_id=? ORDER BY version DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)

        if role != "admin":
            if row["created_by"] != actor:
                raise HTTPException(403, detail="You can only delete your own records.")
            if row["status"] not in ("draft", "submitted", "returned"):
                raise HTTPException(403, detail="Only draft/submitted/returned primers can be deleted.")

        # Block deletion if any confirmed workflow references this primer
        ref = conn.execute(
            """SELECT 1 FROM workflow_primer_refs wpr
               JOIN workflows w ON w.record_id = wpr.workflow_record_id
               WHERE wpr.primer_record_id = ? AND w.status = 'confirmed' LIMIT 1""",
            (record_id,),
        ).fetchone()
        if ref:
            raise HTTPException(
                409,
                detail="Cannot delete: this Primer is attached to a confirmed Workflow. Detach it first.",
            )

        conn.execute("DELETE FROM workflow_primer_refs WHERE primer_record_id=?", (record_id,))
        conn.execute("DELETE FROM primers WHERE record_id=?", (record_id,))

    audit("primer", record_id, 0, "delete", actor)
    return RedirectResponse(url="/primers", status_code=303)
