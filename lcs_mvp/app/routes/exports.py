from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from docx import Document
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ..config import templates, EXPORTS_DIR
from ..database import db, utc_now_iso, workflow_readiness, _user_id
from ..audit import audit, _normalize_domains
from ..linting import _normalize_steps
from ..auth import require
from ..ingestion import _sha256_bytes, _short_code
from ..utils import _json_dump, _json_load

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_iso_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cleanup_export_artifacts(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete expired export artifacts (files + DB rows).

    Safety: only deletes files inside EXPORTS_DIR.
    """
    now = datetime.now(timezone.utc)
    exports_root = Path(EXPORTS_DIR).resolve()

    scanned = 0
    expired = 0
    deleted_files = 0
    missing_files = 0
    deleted_rows = 0

    rows = conn.execute(
        "SELECT id, path, exported_at, retention_days FROM export_artifacts ORDER BY exported_at ASC"
    ).fetchall()

    for r in rows:
        scanned += 1
        exported_at = _parse_iso_dt(str(r["exported_at"] or ""))
        if not exported_at:
            continue

        retention_days = int(r["retention_days"]) if r["retention_days"] is not None else 30
        if now <= (exported_at + timedelta(days=retention_days)):
            continue

        expired += 1

        p = Path(str(r["path"] or ""))
        try:
            p_abs = p.resolve()
        except (ValueError, OSError) as e:
            logger.warning("Export cleanup: could not resolve path %r: %s", r["path"], e)
            continue

        if exports_root not in p_abs.parents and p_abs != exports_root:
            # refuse to delete outside exports dir
            continue

        if p_abs.exists():
            try:
                p_abs.unlink()
                deleted_files += 1
            except OSError as e:
                logger.warning("Export cleanup: could not delete %s: %s", p_abs, e)
                continue
        else:
            missing_files += 1

        conn.execute("DELETE FROM export_artifacts WHERE id=?", (str(r["id"]),))
        deleted_rows += 1

    return {
        "scanned": scanned,
        "expired": expired,
        "deleted_files": deleted_files,
        "missing_files": missing_files,
        "deleted_rows": deleted_rows,
    }


def _task_export_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    return {
        "type": "task",
        "record_id": r["record_id"],
        "version": int(r["version"]),
        "status": r["status"],
        "title": r["title"],
        "outcome": r["outcome"],
        "facts": _json_load(r["facts_json"]) or [],
        "concepts": _json_load(r["concepts_json"]) or [],
        "procedure_name": r["procedure_name"],
        "steps": _normalize_steps(_json_load(r["steps_json"]) or []),
        "dependencies": _json_load(r["dependencies_json"]) or [],
        "irreversible_flag": bool(r["irreversible_flag"]),
        "task_assets": _json_load(r["task_assets_json"]) or [],
        "needs_review_flag": bool(r["needs_review_flag"]),
        "needs_review_note": r["needs_review_note"],
        "meta": {
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "created_by": r["created_by"],
            "updated_by": r["updated_by"],
            "reviewed_at": r["reviewed_at"],
            "reviewed_by": r["reviewed_by"],
            "change_note": r["change_note"],
        },
    }


def _workflow_export_dict(wf_row: sqlite3.Row, refs_rows: list[sqlite3.Row]) -> dict[str, Any]:
    wf = dict(wf_row)
    return {
        "type": "workflow",
        "record_id": wf["record_id"],
        "version": int(wf["version"]),
        "status": wf["status"],
        "title": wf["title"],
        "objective": wf["objective"],
        "task_refs": [
            {
                "order_index": int(r["order_index"]),
                "record_id": r["task_record_id"],
                "version": int(r["task_version"]),
            }
            for r in refs_rows
        ],
        "needs_review_flag": bool(wf["needs_review_flag"]),
        "needs_review_note": wf["needs_review_note"],
        "meta": {
            "created_at": wf["created_at"],
            "updated_at": wf["updated_at"],
            "created_by": wf["created_by"],
            "updated_by": wf["updated_by"],
            "reviewed_at": wf["reviewed_at"],
            "reviewed_by": wf["reviewed_by"],
            "change_note": wf["change_note"],
        },
    }


@router.get("/exports", response_class=HTMLResponse)
def exports_library(request: Request, workflow: str = "", kind: str = "", by: str = "", msg: str | None = None):
    require(request.state.role, "export:library")

    wf = (workflow or "").strip()
    kind = (kind or "").strip().lower()
    by = (by or "").strip()

    where: list[str] = []
    params: list[object] = []

    if wf:
        where.append("workflow_record_id=?")
        params.append(wf)
    if kind:
        where.append("kind=?")
        params.append(kind)
    if by:
        where.append("exported_by=?")
        params.append(by)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        artifacts = conn.execute(
            f"SELECT id, kind, filename, path, workflow_record_id, workflow_version, exported_at, exported_by, retention_days "
            f"FROM export_artifacts {where_sql} ORDER BY exported_at DESC LIMIT 200",
            params,
        ).fetchall()

    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for a in artifacts:
        d = dict(a)
        exported_at = _parse_iso_dt(str(d.get("exported_at") or ""))
        retention_days = int(d.get("retention_days") or 30)
        if exported_at:
            expires_at = exported_at + timedelta(days=retention_days)
            d["expires_at"] = expires_at.replace(microsecond=0).isoformat()
            d["is_expired"] = now > expires_at
        else:
            d["expires_at"] = None
            d["is_expired"] = False
        out.append(d)

    return templates.TemplateResponse(
        request,
        "admin/exports.html",
        {
            "artifacts": out,
            "msg": msg,
            "filters": {"workflow": wf, "kind": kind, "by": by},
        },
    )


@router.get("/admin/exports", response_class=HTMLResponse)
def admin_exports_redirect(request: Request):
    # Backward-compatible URL for admins.
    require(request.state.role, "export:cleanup")
    return RedirectResponse(url="/exports", status_code=303)


@router.post("/admin/exports/cleanup")
def admin_exports_cleanup(request: Request):
    require(request.state.role, "export:cleanup")
    actor = request.state.user
    with db() as conn:
        stats = _cleanup_export_artifacts(conn)
        conn.commit()
        audit(
            "export_artifacts",
            "retention",
            1,
            "cleanup",
            actor,
            note=f"expired={stats['expired']} deleted_rows={stats['deleted_rows']} deleted_files={stats['deleted_files']} missing_files={stats['missing_files']}",
            conn=conn,
        )

    msg = (
        f"scanned={stats['scanned']} expired={stats['expired']} deleted_rows={stats['deleted_rows']} "
        f"deleted_files={stats['deleted_files']} missing_files={stats['missing_files']}"
    )
    return RedirectResponse(url=f"/exports?msg={msg}", status_code=303)


@router.get("/exports/{export_id}/download")
def export_download(request: Request, export_id: str):
    require(request.state.role, "export:library")
    with db() as conn:
        row = conn.execute(
            "SELECT filename, path FROM export_artifacts WHERE id=?",
            (export_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404)

    p = Path(str(row["path"] or ""))
    try:
        p_abs = p.resolve()
        exports_root = Path(EXPORTS_DIR).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    if exports_root not in p_abs.parents and p_abs != exports_root:
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    if not p_abs.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing")

    return FileResponse(
        str(p_abs),
        filename=str(row["filename"]),
        media_type="application/octet-stream",
    )


@router.get("/workflows/{record_id}/{version}/export.html")
def workflow_export_html(record_id: str, version: int):
    """Export a confirmed workflow as a standalone, print-friendly HTML file."""
    with db() as conn:
        wf = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not wf:
            raise HTTPException(404)

        if wf["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="Export is allowed for confirmed workflows only")

        refs = conn.execute(
            """
            SELECT r.order_index, t.*
            FROM workflow_task_refs r
            JOIN tasks t ON t.record_id=r.task_record_id AND t.version=r.task_version
            WHERE r.workflow_record_id=? AND r.workflow_version=?
            ORDER BY r.order_index
            """,
            (record_id, version),
        ).fetchall()

        readiness = workflow_readiness(
            conn,
            [(r["record_id"], int(r["version"])) for r in refs],
        )
        if readiness != "ready":
            raise HTTPException(status_code=409, detail="Export is allowed only when all referenced Task versions are confirmed")

    def _esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _steps_rows(steps_data: list[dict[str, Any]]) -> str:
        rows = []
        for i, st in enumerate(steps_data, 1):
            actions = st.get("actions") or []
            actions_html = "<br>".join(_esc(str(a)) for a in actions if str(a).strip()) if actions else "—"
            rows.append(
                f"<tr><td>{i}</td><td>{_esc(str(st.get('text','') or ''))}</td>"
                f"<td>{actions_html}</td>"
                f"<td>{_esc(str(st.get('notes','') or '')) or '—'}</td>"
                f"<td>{_esc(str(st.get('completion','') or '')) or '—'}</td></tr>"
            )
        return "\n".join(rows)

    tasks_html = []
    for r in refs:
        steps = _normalize_steps(_json_load(r["steps_json"]) or [])
        facts = _json_load(r["facts_json"]) or []
        concepts = _json_load(r["concepts_json"]) or []
        deps = _json_load(r["dependencies_json"]) or []

        facts_html = "".join(f"<li>{_esc(str(f))}</li>" for f in facts) if facts else "<li><em>None</em></li>"
        concepts_html = "".join(f"<li>{_esc(str(c))}</li>" for c in concepts) if concepts else "<li><em>None</em></li>"
        deps_html = "".join(f"<li>{_esc(str(d))}</li>" for d in deps) if deps else "<li><em>None</em></li>"
        irr_note = "<p><strong>\u26a0 Irreversible:</strong> This task cannot be undone.</p>" if r["irreversible_flag"] else ""

        tasks_html.append(f"""
<section class="task">
  <h2>Task {r['order_index']}: {_esc(str(r['title']))}</h2>
  {irr_note}
  <p><strong>Outcome:</strong> {_esc(str(r['outcome']))}</p>
  <h3>Facts</h3><ul>{facts_html}</ul>
  <h3>Concepts</h3><ul>{concepts_html}</ul>
  <h3>Dependencies</h3><ul>{deps_html}</ul>
  <h3>Procedure: {_esc(str(r['procedure_name']))}</h3>
  <table>
    <thead><tr><th>#</th><th>Step</th><th>Actions</th><th>Notes</th><th>Completion</th></tr></thead>
    <tbody>{_steps_rows(steps)}</tbody>
  </table>
</section>""")

    trace = f"{record_id}@{version} \u00b7 exported {utc_now_iso().split('T')[0]}"
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(str(wf['title']))}</title>
<style>
  body{{font-family:Georgia,serif;max-width:900px;margin:40px auto;padding:0 24px;color:#111;line-height:1.6}}
  h1{{font-size:2em;border-bottom:2px solid #111;padding-bottom:8px}}
  h2{{font-size:1.35em;margin-top:2em;border-bottom:1px solid #ccc;padding-bottom:4px}}
  h3{{font-size:1em;text-transform:uppercase;letter-spacing:.05em;color:#555;margin-top:1.2em}}
  table{{border-collapse:collapse;width:100%;margin-top:8px;font-size:.9em}}
  th,td{{border:1px solid #ccc;padding:6px 10px;vertical-align:top;text-align:left}}
  th{{background:#f5f5f5;font-weight:600}}
  ul{{margin:4px 0;padding-left:20px}}
  .task{{page-break-before:always}}
  .task:first-child{{page-break-before:avoid}}
  .provenance{{font-size:.75em;color:#888;margin-top:3em;border-top:1px solid #eee;padding-top:8px}}
  @media print{{body{{margin:0;padding:16px}}.task{{page-break-before:always}}}}
</style>
</head>
<body>
<h1>{_esc(str(wf['title']))}</h1>
<p><strong>Objective:</strong> {_esc(str(wf['objective']))}</p>
<h2>Task Overview</h2>
<ol>{"".join(f"<li>{_esc(str(r['title']))}</li>" for r in refs)}</ol>
{"".join(tasks_html)}
<div class="provenance">Trace: {_esc(trace)}</div>
</body>
</html>"""

    filename = f"workflow__{_short_code('WF', record_id)}__v{version}.html"
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, item_type: str = ""):
    # Contributors and admins only. (Admin implicitly has all domains.)
    if request.state.role not in ("contributor", "admin"):
        raise HTTPException(status_code=403, detail="Forbidden: contributor/admin only")

    with db() as conn:
        # Determine authorized domains
        if request.state.role == "admin":
            from ..database import _active_domains
            doms = _active_domains(conn)
        else:
            uid = _user_id(conn, request.state.user)
            dom_rows = conn.execute("SELECT domain FROM user_domains WHERE user_id=?", (uid,)).fetchall() if uid else []
            doms = [str(r["domain"]) for r in dom_rows]

        filter_type = (item_type or "").strip().lower()
        if filter_type not in ("task", "workflow", "assessment"):
            filter_type = ""

        items: list[dict[str, Any]] = []
        if doms:
            dset = {d.strip().lower() for d in doms if d}
            qmarks = ",".join(["?"] * len(doms))

            # Priority bucket for tasks that block workflow progression
            # (derived from workflow-side review flag/linkage).
            blocking_task_refs: set[tuple[str, int]] = set()
            block_rows = conn.execute(
                """
                SELECT wr.task_record_id, wr.task_version
                FROM workflow_task_refs wr
                JOIN workflows w
                  ON w.record_id = wr.workflow_record_id
                 AND w.version = wr.workflow_version
                WHERE w.status='submitted' AND w.needs_review_flag=1
                """
            ).fetchall()
            for br in block_rows:
                blocking_task_refs.add((str(br["task_record_id"]), int(br["task_version"])))

            # Tasks
            t_rows = conn.execute(
                f"SELECT record_id, version, title, status, domain, created_at FROM tasks WHERE status='submitted' AND domain IN ({qmarks})",
                doms,
            ).fetchall()
            for r in t_rows:
                rid = str(r["record_id"])
                ver = int(r["version"])
                items.append(
                    {
                        "type": "task",
                        "record_id": rid,
                        "version": ver,
                        "title": r["title"],
                        "status": r["status"],
                        "domains": [str(r["domain"])],
                        "created_at": str(r["created_at"] or ""),
                        "priority_bucket": 0 if (rid, ver) in blocking_task_refs else 2,
                    }
                )

            # Workflows (domain derived)
            w_rows = conn.execute(
                "SELECT record_id, version, title, status, domains_json, created_at FROM workflows WHERE status='submitted'"
            ).fetchall()
            for r in w_rows:
                wdoms = _normalize_domains(r["domains_json"])
                if dset.intersection(wdoms):
                    items.append(
                        {
                            "type": "workflow",
                            "record_id": r["record_id"],
                            "version": int(r["version"]),
                            "title": r["title"],
                            "status": r["status"],
                            "domains": wdoms,
                            "created_at": str(r["created_at"] or ""),
                            "priority_bucket": 2,
                        }
                    )

            # Assessments
            a_rows = conn.execute(
                "SELECT record_id, version, stem, status, domains_json, created_at FROM assessment_items WHERE status='submitted'"
            ).fetchall()
            for r in a_rows:
                adoms = _normalize_domains(r["domains_json"])
                if dset.intersection(adoms):
                    title = str(r["stem"])[:80]
                    items.append(
                        {
                            "type": "assessment",
                            "record_id": r["record_id"],
                            "version": int(r["version"]),
                            "title": title,
                            "status": r["status"],
                            "domains": adoms,
                            "created_at": str(r["created_at"] or ""),
                            "priority_bucket": 2,
                        }
                    )

            if filter_type:
                items = [it for it in items if str(it.get("type")) == filter_type]

            # Sort: blockers first, then oldest, then type/title.
            items.sort(
                key=lambda it: (
                    int(it.get("priority_bucket", 2)),
                    str(it.get("created_at") or ""),
                    str(it.get("type") or ""),
                    str(it.get("title") or "").lower(),
                )
            )

    return templates.TemplateResponse(request, "review_queue.html", {"items": items, "domains": doms, "item_type": filter_type})


@router.get("/audit", response_class=HTMLResponse)
def audit_list(
    request: Request,
    entity_type: str | None = None,
    record_id: str | None = None,
    limit: int = 200,
):
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

    items = [dict(r) for r in rows]

    return templates.TemplateResponse(
        request,
        "audit_list.html",
        {"items": items, "entity_type": entity_type, "record_id": record_id, "limit": limit},
    )


@router.get("/export/task/{record_id}/{version}.json")
def export_task_json(record_id: str, version: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(404)

    if row["status"] != "confirmed":
        raise HTTPException(status_code=409, detail="Export is allowed for confirmed tasks only")

    payload = _task_export_dict(row)
    filename = f"task__{record_id}__v{version}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/workflow/{record_id}/{version}.json")
def export_workflow_json(record_id: str, version: int):
    with db() as conn:
        wf = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not wf:
            raise HTTPException(404)

        if wf["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="Export is allowed for confirmed workflows only")

        refs = conn.execute(
            """
            SELECT order_index, task_record_id, task_version
            FROM workflow_task_refs
            WHERE workflow_record_id=? AND workflow_version=?
            ORDER BY order_index
            """,
            (record_id, version),
        ).fetchall()

        readiness = workflow_readiness(conn, [(r["task_record_id"], int(r["task_version"])) for r in refs])
        if readiness != "ready":
            raise HTTPException(status_code=409, detail="Export is allowed only when all referenced Task versions are confirmed")

    payload = _workflow_export_dict(wf, refs)
    filename = f"workflow__{record_id}__v{version}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/workflows/{record_id}/{version}/export.docx")
def workflow_export_docx(request: Request, record_id: str, version: int):
    """Export a confirmed workflow to DOCX (v0 ILT handout).

    Governance rule: exports are allowed for confirmed workflows only, and only when
    all referenced Task versions are confirmed.
    """
    actor = request.state.user

    with db() as conn:
        wf = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not wf:
            raise HTTPException(404)

        if wf["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="Export is allowed for confirmed workflows only")

        ref_rows = conn.execute(
            """
            SELECT order_index, task_record_id, task_version
            FROM workflow_task_refs
            WHERE workflow_record_id=? AND workflow_version=?
            ORDER BY order_index
            """,
            (record_id, version),
        ).fetchall()

        readiness = workflow_readiness(conn, [(r["task_record_id"], int(r["task_version"])) for r in ref_rows])
        if readiness != "ready":
            raise HTTPException(status_code=409, detail="Export is allowed only when all referenced Task versions are confirmed")

        tasks: list[dict[str, Any]] = []
        for r in ref_rows:
            t = conn.execute(
                "SELECT * FROM tasks WHERE record_id=? AND version=?",
                (r["task_record_id"], int(r["task_version"])),
            ).fetchone()
            if not t:
                raise HTTPException(status_code=409, detail="Export failed: referenced task not found")
            if t["status"] != "confirmed":
                raise HTTPException(status_code=409, detail="Export is allowed only when all referenced Task versions are confirmed")
            tasks.append({"order_index": int(r["order_index"]), "row": dict(t)})

        # Build doc
        doc = Document()

        trace = f"Trace: {_short_code('WF', record_id)} v{version} · {utc_now_iso().split('T')[0]}"

        # Footer trace on each section
        for s in doc.sections:
            fp = s.footer.paragraphs[0] if s.footer.paragraphs else s.footer.add_paragraph()
            fp.text = trace

        doc.add_heading(str(wf["title"]), level=1)
        doc.add_paragraph(str(wf["objective"])).style = doc.styles["Normal"]

        doc.add_heading("Tasks", level=2)
        for t in tasks:
            r = t["row"]
            doc.add_paragraph(f"{t['order_index']}. {r['title']}")

        for t in tasks:
            r = t["row"]
            doc.add_page_break()
            doc.add_heading(f"Task {t['order_index']}: {r['title']}", level=2)
            doc.add_paragraph(f"Outcome: {r['outcome']}")

            steps = _normalize_steps(_json_load(r["steps_json"]) or [])
            doc.add_paragraph(f"Procedure: {r['procedure_name']}")

            table = doc.add_table(rows=1, cols=4)
            hdr = table.rows[0].cells
            hdr[0].text = "Step"
            hdr[1].text = "Actions"
            hdr[2].text = "Notes"
            hdr[3].text = "Completion"

            for st in steps:
                row = table.add_row().cells
                row[0].text = str(st.get("text", "") or "")
                actions = st.get("actions") or []
                row[1].text = "\n".join([str(a) for a in actions if str(a).strip()]) if actions else ""
                row[2].text = str(st.get("notes", "") or "")
                row[3].text = str(st.get("completion", "") or "")

        # Provenance (full UUIDs)
        doc.add_page_break()
        doc.add_heading("Provenance (internal)", level=2)
        doc.add_paragraph(f"Workflow: {record_id} v{version}")
        for r in ref_rows:
            doc.add_paragraph(f"Task: {r['task_record_id']} v{int(r['task_version'])}")

        # Write artifact
        export_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"workflow__{_short_code('WF', record_id)}__v{version}__{ts}.docx"
        out_path = os.path.join(EXPORTS_DIR, filename)

        doc.save(out_path)
        file_bytes = Path(out_path).read_bytes()
        sha = _sha256_bytes(file_bytes)

        conn.execute(
            "INSERT INTO export_artifacts(id, kind, filename, path, sha256, workflow_record_id, workflow_version, task_refs_json, exported_at, exported_by, retention_days) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                export_id,
                "docx",
                filename,
                out_path,
                sha,
                record_id,
                int(version),
                _json_dump([{"record_id": r["task_record_id"], "version": int(r["task_version"])} for r in ref_rows]),
                utc_now_iso(),
                actor,
                30,
            ),
        )
        audit("export", export_id, 1, "create", actor, note=f"kind=docx workflow={record_id}@{version}", conn=conn)

    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


@router.get("/workflows/{record_id}/{version}/export.md")
def workflow_export_md(record_id: str, version: int):
    with db() as conn:
        wf = conn.execute(
            "SELECT * FROM workflows WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not wf:
            raise HTTPException(404)

        if wf["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="Export is allowed for confirmed workflows only")

        refs = conn.execute(
            """
            SELECT r.order_index, t.*
            FROM workflow_task_refs r
            JOIN tasks t ON t.record_id=r.task_record_id AND t.version=r.task_version
            WHERE r.workflow_record_id=? AND r.workflow_version=?
            ORDER BY r.order_index
            """,
            (record_id, version),
        ).fetchall()

        readiness = workflow_readiness(
            conn,
            [(r["record_id"], int(r["version"])) for r in refs],
        )
        if readiness != "ready":
            raise HTTPException(status_code=409, detail="Export is allowed only when all referenced Task versions are confirmed")

    lines: list[str] = []
    lines.append(f"# {wf['title']}")
    lines.append("")

    lines.append(f"**Objective:** {wf['objective']}")
    lines.append("")

    for r in refs:
        steps = _normalize_steps(_json_load(r["steps_json"]))
        facts = _json_load(r["facts_json"]) or []
        concepts = _json_load(r["concepts_json"]) or []
        deps = _json_load(r["dependencies_json"]) or []

        lines.append(f"## Task {r['order_index']}: {r['title']} ({r['record_id']}@{r['version']})")
        if r["status"] != "confirmed":
            lines.append(f"**Task status:** {r['status']} (unconfirmed)")
            lines.append("")
        lines.append("")
        lines.append(f"**Outcome:** {r['outcome']}")
        lines.append("")

        if facts:
            lines.append("**Facts:**")
            for f in facts:
                lines.append(f"- {f}")
            lines.append("")

        if concepts:
            lines.append("**Concepts:**")
            for c in concepts:
                lines.append(f"- {c}")
            lines.append("")

        if deps:
            lines.append("**Dependencies:**")
            for d in deps:
                lines.append(f"- {d}")
            lines.append("")

        lines.append(f"**Procedure:** {r['procedure_name']}")
        lines.append("")

        def _md_cell(s: str) -> str:
            s = (s or "").replace("\n", "<br>")
            s = s.replace("|", "\\|")
            return s

        lines.append("| Step | Actions | Notes | Completion |")
        lines.append("| --- | --- | --- | --- |")
        for st in steps:
            txt = _md_cell(str(st.get("text", "") or ""))
            notes = _md_cell(str(st.get("notes", "") or "").strip()) or "—"
            actions = st.get("actions") or []
            actions_txt = _md_cell("<br>".join([str(a) for a in actions if str(a).strip()])) if actions else "—"
            comp = _md_cell(str(st.get("completion", "") or "")) or "—"
            lines.append(f"| {txt} | {actions_txt} | {notes} | {comp} |")
        lines.append("")

    md = "\n".join(lines)
    filename = f"workflow__{record_id}__v{version}.md"
    return HTMLResponse(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
