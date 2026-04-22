from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import templates
from ..database import db, utc_now_iso, _active_domains, _user_has_domain, _workflow_domains
from ..audit import audit, _normalize_domains, _fetch_return_note, get_latest_version
from ..linting import _normalize_steps
from ..auth import require
from ..utils import _json_dump, _json_load

router = APIRouter()


def _assessment_domains(conn: sqlite3.Connection, refs: list[dict[str, Any]]) -> list[str]:
    """Derive domains from attached task/workflow refs."""
    doms: set[str] = set()
    for r in refs or []:
        rt = (r.get("ref_type") or "").strip().lower()
        rid = str(r.get("ref_record_id") or "").strip()
        ver = int(r.get("ref_version") or 0)
        if not rt or not rid or ver <= 0:
            continue
        if rt == "task":
            row = conn.execute("SELECT domain FROM tasks WHERE record_id=? AND version=?", (rid, ver)).fetchone()
            d = (str(row["domain"]) if row else "").strip().lower()
            if d:
                doms.add(d)
        elif rt == "workflow":
            row = conn.execute("SELECT domains_json FROM workflows WHERE record_id=? AND version=?", (rid, ver)).fetchone()
            if row and row["domains_json"]:
                for d in (_json_load(row["domains_json"]) or []):
                    dn = (str(d) or "").strip().lower()
                    if dn:
                        doms.add(dn)
    return sorted(doms)


def _assessment_lint(stem: str, options: list[dict[str, str]], correct_key: str, claim: str) -> list[dict[str, Any]]:
    """Return lint findings [{level, code, msg}]. level in (error|warn)."""
    findings: list[dict[str, Any]] = []

    stem_norm = (stem or "").strip()
    if not stem_norm:
        findings.append({"level": "error", "code": "stem.empty", "msg": "Stem is required"})

    if "which of the following" in stem_norm.lower():
        findings.append({"level": "warn", "code": "stem.which_of_following", "msg": "Avoid 'which of the following' phrasing"})

    # Options
    if len(options) != 4:
        findings.append({"level": "error", "code": "options.count", "msg": "Exactly 4 options are required"})

    keys = [str(o.get("key") or "").strip().upper() for o in options]
    texts = [str(o.get("text") or "").strip() for o in options]

    if any(not k or k not in ("A", "B", "C", "D") for k in keys):
        findings.append({"level": "error", "code": "options.keys", "msg": "Option keys must be A, B, C, D"})

    if any(not t for t in texts):
        findings.append({"level": "error", "code": "options.empty", "msg": "All option texts are required"})

    ck = (correct_key or "").strip().upper()
    if ck not in keys:
        findings.append({"level": "error", "code": "correct.missing", "msg": "Correct answer key must match one of the options"})

    # Duplicate texts
    seen: set[str] = set()
    for t in texts:
        tn = re.sub(r"\s+", " ", (t or "").strip().lower())
        if not tn:
            continue
        if tn in seen:
            findings.append({"level": "error", "code": "options.duplicate", "msg": "Duplicate option text detected"})
            break
        seen.add(tn)

    # Absolute terms heuristic
    abs_terms = (" always ", " never ", " only ")
    for idx, t in enumerate(texts):
        tl = f" {t.lower()} "
        if any(a in tl for a in abs_terms):
            findings.append({"level": "warn", "code": "options.absolute_terms", "msg": f"Option {keys[idx] or '?'} contains absolute terms (always/never/only)"})
            break

    # Length band (warn)
    wcounts = [len((t or "").split()) for t in texts if t]
    if wcounts:
        if max(wcounts) - min(wcounts) > 6:
            findings.append({"level": "warn", "code": "options.length_band", "msg": "Options vary widely in length; this can create visual clues"})

    claim_norm = (claim or "").strip()
    if claim_norm not in ("auto", "fact_probe", "concept_probe", "procedure_proxy"):
        findings.append({"level": "error", "code": "claim.invalid", "msg": "Invalid claim (auto|fact_probe|concept_probe|procedure_proxy)"})

    # Auto is allowed and intentionally does not apply claim-specific linting.
    if claim_norm == "auto":
        return findings

    # Only apply scenario hinting when explicitly in procedure_proxy.
    if claim_norm == "procedure_proxy":

        # Soft check: require some scenario signal.
        if not any(x in stem_norm.lower() for x in ("scenario", "you", "environment", "given", "after")):
            findings.append({"level": "warn", "code": "procedure_proxy.weak_scenario", "msg": "Procedure proxy items should usually be scenario-framed"})

    return findings


def _assessment_export_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    options = _json_load(r["options_json"]) or []
    # Robustness: Handle legacy dict format {"A": "...", ...}
    if isinstance(options, dict):
        normalized = []
        for k in ["A", "B", "C", "D"]:
            normalized.append({"key": k, "text": str(options.get(k) or "")})
        options = normalized
    return {
        "type": "assessment",
        "record_id": r["record_id"],
        "version": int(r["version"]),
        "status": r["status"],
        "stem": r["stem"],
        "options": options,
        "correct_key": r["correct_key"],
        "rationale": r.get("rationale") or "",
        "claim": r.get("claim") or "fact_probe",
        "domains": _json_load(r.get("domains_json") or "[]") or [],
        "refs": _json_load(r.get("refs_json") or "[]") or [],
        "lint": _json_load(r.get("lint_json") or "[]") or [],
        "needs_review_flag": bool(r.get("needs_review_flag")),
        "needs_review_note": r.get("needs_review_note"),
        "meta": {
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "created_by": r.get("created_by"),
            "updated_by": r.get("updated_by"),
            "reviewed_at": r.get("reviewed_at"),
            "reviewed_by": r.get("reviewed_by"),
            "change_note": r.get("change_note"),
        },
    }


@router.get("/_refs/search")
def refs_search(request: Request, kind: str = "task", q: str = "", limit: int = 20):
    """Lightweight ref search for the assessment ref selector UI."""
    require(request.state.role, "assessment:create")

    kind = (kind or "task").strip().lower()
    q_norm = (q or "").strip().lower()
    limit = max(1, min(int(limit or 20), 50))

    with db() as conn:
        if kind == "workflow":
            rows = conn.execute("SELECT record_id, MAX(version) AS latest_version FROM workflows GROUP BY record_id").fetchall()
            items: list[dict[str, Any]] = []
            for r in rows:
                rid = r["record_id"]
                v = int(r["latest_version"])
                w = conn.execute(
                    "SELECT record_id, version, title, status, domains_json FROM workflows WHERE record_id=? AND version=?",
                    (rid, v),
                ).fetchone()
                if not w:
                    continue
                if q_norm and q_norm not in (w["title"] or "").lower():
                    continue
                doms = _normalize_domains(w["domains_json"])
                items.append({"ref_type": "workflow", "record_id": w["record_id"], "version": int(w["version"]), "title": w["title"], "status": w["status"], "domains": doms})
            items.sort(key=lambda it: (it.get("title") or ""))
            return {"items": items[:limit]}

        # default tasks
        rows = conn.execute("SELECT record_id, MAX(version) AS latest_version FROM tasks GROUP BY record_id").fetchall()
        items = []
        for r in rows:
            rid = r["record_id"]
            v = int(r["latest_version"])
            t = conn.execute(
                "SELECT record_id, version, title, status, domain FROM tasks WHERE record_id=? AND version=?",
                (rid, v),
            ).fetchone()
            if not t:
                continue
            if q_norm and q_norm not in (t["title"] or "").lower():
                continue
            items.append({"ref_type": "task", "record_id": t["record_id"], "version": int(t["version"]), "title": t["title"], "status": t["status"], "domain": t["domain"]})
        items.sort(key=lambda it: (it.get("title") or ""))
        return {"items": items[:limit]}


@router.get("/_refs/peek", response_class=HTMLResponse)
def refs_peek(request: Request, ref_type: str, record_id: str, version: int, component: str = "facts"):
    """Open a small window for assessment authors to view underlying task/workflow data.

    Intentional constraint: we do not surface dependencies as an assessable target.
    Dependencies tend to produce low-quality "prereq chain" questions.

    This is intentionally a separate page (not inline) to avoid spamming the assessment form.
    """
    require(request.state.role, "assessment:create")

    ref_type = (ref_type or "").strip().lower()
    component = (component or "facts").strip().lower()
    if component not in ("facts", "concepts", "procedure"):
        component = "facts"

    with db() as conn:
        if ref_type == "task":
            row = conn.execute(
                "SELECT record_id, version, title, status, domain, outcome, procedure_name, facts_json, concepts_json, steps_json FROM tasks WHERE record_id=? AND version=?",
                (record_id, int(version)),
            ).fetchone()
            if not row:
                raise HTTPException(404)
            return templates.TemplateResponse(
                request,
                "ref_peek.html",
                {
                    "kind": "task",
                    "ref_type": "task",
                    "record_id": row["record_id"],
                    "version": int(row["version"]),
                    "title": row["title"],
                    "status": row["status"],
                    "domain": row["domain"],
                    "outcome": row["outcome"],
                    "procedure_name": row["procedure_name"],
                    "component": component,
                    "facts": _json_load(row["facts_json"]) or [],
                    "concepts": _json_load(row["concepts_json"]) or [],
                    "steps": _normalize_steps(_json_load(row["steps_json"]) or []),
                    "tasks": [],
                },
            )

        if ref_type == "workflow":
            row = conn.execute(
                "SELECT record_id, version, title, status, objective FROM workflows WHERE record_id=? AND version=?",
                (record_id, int(version)),
            ).fetchone()
            if not row:
                raise HTTPException(404)

            refs = conn.execute(
                "SELECT task_record_id, task_version FROM workflow_task_refs WHERE workflow_record_id=? AND workflow_version=? ORDER BY order_index",
                (record_id, int(version)),
            ).fetchall()

            tasks: list[dict[str, Any]] = []
            agg: list[str] = []
            seen: dict[str, str] = {}

            def _norm_key(s: str) -> str:
                return re.sub(r"\s+", " ", (s or "").strip().lower())

            for r in refs:
                t = conn.execute(
                    "SELECT record_id, version, title, domain, outcome, procedure_name, facts_json, concepts_json, steps_json FROM tasks WHERE record_id=? AND version=?",
                    (r["task_record_id"], int(r["task_version"])),
                ).fetchone()
                if not t:
                    continue

                tasks.append(
                    {
                        "record_id": t["record_id"],
                        "version": int(t["version"]),
                        "title": t["title"],
                        "domain": t["domain"],
                        "outcome": t["outcome"],
                        "procedure_name": t["procedure_name"],
                        "steps": _normalize_steps(_json_load(t["steps_json"]) or []),
                    }
                )

                if component == "facts":
                    for x in (_json_load(t["facts_json"]) or []):
                        k = _norm_key(str(x))
                        if k and k not in seen:
                            seen[k] = str(x)
                elif component == "concepts":
                    for x in (_json_load(t["concepts_json"]) or []):
                        k = _norm_key(str(x))
                        if k and k not in seen:
                            seen[k] = str(x)

            if component in ("facts", "concepts"):
                agg = sorted(seen.values(), key=lambda s: _norm_key(s))

            return templates.TemplateResponse(
                request,
                "ref_peek.html",
                {
                    "kind": "workflow",
                    "ref_type": "workflow",
                    "record_id": row["record_id"],
                    "version": int(row["version"]),
                    "title": row["title"],
                    "status": row["status"],
                    "objective": row["objective"],
                    "component": component,
                    "agg": agg,
                    "tasks": tasks,
                    # unused for workflow branch but template expects keys
                    "domain": "",
                    "outcome": "",
                    "procedure_name": "",
                    "facts": [],
                    "concepts": [],
                    "steps": [],
                },
            )

    raise HTTPException(status_code=400, detail="Invalid ref_type")


@router.get("/assessments", response_class=HTMLResponse)
def assessments_list(request: Request, status: str | None = None, q: str | None = None, domain: str | None = None, claim: str | None = None):
    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None
    claim_norm = (claim or "").strip().lower() or None

    with db() as conn:
        sql = "SELECT record_id, MAX(version) AS latest_version FROM assessment_items GROUP BY record_id ORDER BY record_id"
        rows = conn.execute(sql).fetchall()

        domains = _active_domains(conn)
        items: list[dict[str, Any]] = []
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
            # sqlite3.Row doesn't implement .get
            if claim_norm and (str(latest["claim"] or "").strip().lower() != claim_norm):
                continue

            doms = _normalize_domains(latest["domains_json"])
            if domain_norm and domain_norm not in set(doms):
                continue

            items.append(
                {
                    "record_id": rid,
                    "latest_version": latest_v,
                    "stem": latest["stem"],
                    "status": latest["status"],
                    "claim": (latest["claim"] or "auto"),
                    "domains": doms,
                    "needs_review_flag": bool(latest["needs_review_flag"]),
                }
            )

    return templates.TemplateResponse(
        request,
        "assessments_list.html",
        {"items": items, "status": status, "q": q, "domain": domain_norm or "", "domains": domains, "claim": claim_norm or ""},
    )


@router.get("/delivery", response_class=HTMLResponse)
def delivery_page(request: Request, q: str | None = None, domain: str | None = None, tag: str | None = None):
    require(request.state.role, "delivery:view")

    q_norm = (q or "").strip().lower()
    domain_norm = (domain or "").strip().lower() or None
    tag_norm = (tag or "").strip().lower() or None

    with db() as conn:
        domains = _active_domains(conn)

        # Latest confirmed version per workflow
        rows = conn.execute(
            "SELECT record_id, MAX(version) AS v FROM workflows WHERE status='confirmed' GROUP BY record_id ORDER BY record_id"
        ).fetchall()

        workflows: list[dict[str, Any]] = []
        all_tags: set[str] = set()

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

            wf_tags = [str(x).strip().lower() for x in (_json_load(wf["tags_json"]) or []) if str(x).strip()]
            for t in wf_tags:
                all_tags.add(t)

            if tag_norm and tag_norm not in set(wf_tags):
                continue

            wf_domains = _normalize_domains(wf["domains_json"])
            if domain_norm and domain_norm not in set(wf_domains):
                continue

            workflows.append({"record_id": wf["record_id"], "version": v, "title": title})

    workflows.sort(key=lambda w: (w.get("title") or ""))

    return templates.TemplateResponse(
        request,
        "delivery.html",
        {"workflows": workflows, "q": q, "domain": domain_norm or "", "tag": tag_norm or "", "domains": domains, "tags": sorted(all_tags)},
    )


@router.post("/delivery/export")
def delivery_export(request: Request, workflow_key: str = Form(""), modality: str = Form("docx")):
    require(request.state.role, "delivery:export")

    wk = (workflow_key or "").strip()
    if "@" not in wk:
        raise HTTPException(status_code=400, detail="workflow_key is required")
    rid, ver_s = wk.split("@", 1)
    try:
        ver = int(ver_s)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid workflow version")

    mod = (modality or "docx").strip().lower()
    if mod == "docx":
        return RedirectResponse(url=f"/workflows/{rid}/{ver}/export.docx", status_code=303)
    if mod == "md":
        return RedirectResponse(url=f"/workflows/{rid}/{ver}/export.md", status_code=303)
    if mod == "html":
        return RedirectResponse(url=f"/workflows/{rid}/{ver}/export.html", status_code=303)

    raise HTTPException(status_code=409, detail=f"Modality '{mod}' is not operational yet")


@router.get("/assessments/new", response_class=HTMLResponse)
def assessment_new_form(
    request: Request,
    q: str | None = None,
    task_record_id: str | None = None,
    task_version: int | None = None,
    ref_type: str | None = None,
    ref_record_id: str | None = None,
    ref_version: int | None = None,
):
    require(request.state.role, "assessment:create")

    # Support older task_* params, and newer generic ref_* params.
    if task_record_id and task_version and not (ref_type and ref_record_id and ref_version):
        ref_type = "task"
        ref_record_id = task_record_id
        ref_version = int(task_version)

    if not (ref_type and ref_record_id and ref_version):
        # Picker UI
        q_norm = (q or "").strip().lower()
        with db() as conn:
            # Tasks (latest versions)
            t_rows = conn.execute("SELECT record_id, MAX(version) AS latest_version FROM tasks GROUP BY record_id ORDER BY record_id").fetchall()
            tasks: list[dict[str, Any]] = []
            for r in t_rows:
                rid = r["record_id"]
                v = int(r["latest_version"])
                t = conn.execute("SELECT record_id, version, title, status, domain FROM tasks WHERE record_id=? AND version=?", (rid, v)).fetchone()
                if not t:
                    continue
                if q_norm and q_norm not in (t["title"] or "").lower():
                    continue
                tasks.append(dict(t))
            tasks = tasks[:30]

            # Workflows (latest versions)
            w_rows = conn.execute("SELECT record_id, MAX(version) AS latest_version FROM workflows GROUP BY record_id ORDER BY record_id").fetchall()
            workflows: list[dict[str, Any]] = []
            for r in w_rows:
                rid = r["record_id"]
                v = int(r["latest_version"])
                w = conn.execute("SELECT record_id, version, title, status, domains_json FROM workflows WHERE record_id=? AND version=?", (rid, v)).fetchone()
                if not w:
                    continue
                if q_norm and q_norm not in (w["title"] or "").lower():
                    continue
                wd = dict(w)
                wd["domains"] = _normalize_domains(w["domains_json"])
                workflows.append(wd)
            workflows = workflows[:30]

        return templates.TemplateResponse(request, "assessment_pick_ref.html", {"q": q, "tasks": tasks, "workflows": workflows})

    ref = {"ref_type": (ref_type or "").strip().lower(), "ref_record_id": (ref_record_id or "").strip(), "ref_version": int(ref_version)}

    return templates.TemplateResponse(
        request,
        "assessment_edit.html",
        {
            "mode": "new",
            "item": None,
            "refs": [ref],
            "lint": [],
        },
    )


@router.post("/assessments/new")
def assessment_create(
    request: Request,
    stem: str = Form(""),
    claim: str = Form("auto"),
    correct_key: str = Form("A"),
    option_a: str = Form(""),
    option_b: str = Form(""),
    option_c: str = Form(""),
    option_d: str = Form(""),
    rationale: str = Form(""),
    change_note: str = Form(""),
    # authoring helpers (stored in meta_json)
    target_fact: str = Form(""),
    relation_verb: str = Form(""),
    scenario_truth: str = Form(""),
    ref_type: list[str] = Form([]),
    ref_record_id: list[str] = Form([]),
    ref_version: list[int] = Form([]),
):
    require(request.state.role, "assessment:create")
    actor = request.state.user

    record_id = str(uuid.uuid4())
    version = 1
    now = utc_now_iso()

    options = [
        {"key": "A", "text": (option_a or "").strip()},
        {"key": "B", "text": (option_b or "").strip()},
        {"key": "C", "text": (option_c or "").strip()},
        {"key": "D", "text": (option_d or "").strip()},
    ]

    refs: list[dict[str, Any]] = []
    for rt, rid, ver in zip(ref_type or [], ref_record_id or [], ref_version or []):
        rt_n = (rt or "").strip().lower()
        rid_n = (rid or "").strip()
        try:
            ver_i = int(ver)
        except ValueError:
            ver_i = 0
        if rt_n and rid_n and ver_i > 0:
            refs.append({"ref_type": rt_n, "ref_record_id": rid_n, "ref_version": ver_i})

    with db() as conn:
        domains = _assessment_domains(conn, refs)
        lint = _assessment_lint(stem, options, correct_key, claim)

        meta_obj = {
            "target_fact": (target_fact or "").strip(),
            "relation_verb": (relation_verb or "").strip(),
            "scenario_truth": (scenario_truth or "").strip(),
        }

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
                record_id,
                version,
                "draft",
                (stem or "").strip(),
                _json_dump(options),
                (correct_key or "A").strip().upper(),
                (rationale or "").strip(),
                (claim or "auto").strip().lower(),
                _json_dump(domains),
                _json_dump(lint),
                _json_dump(refs),
                _json_dump([]),
                _json_dump(meta_obj),
                now,
                now,
                actor,
                actor,
                None,
                None,
                (change_note or "").strip() or None,
                0,
                None,
            ),
        )

        # refs table
        for idx, r in enumerate(refs, start=1):
            conn.execute(
                "INSERT INTO assessment_refs(assessment_record_id, assessment_version, order_index, ref_type, ref_record_id, ref_version) VALUES (?,?,?,?,?,?)",
                (record_id, version, idx, r["ref_type"], r["ref_record_id"], int(r["ref_version"])),
            )

        audit("assessment", record_id, version, "create", actor, conn=conn)

    return RedirectResponse(url=f"/assessments/{record_id}/{version}/edit?created=1", status_code=303)


@router.get("/assessments/{record_id}/{version}", response_class=HTMLResponse)
def assessment_view(request: Request, record_id: str, version: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)

        ref_rows = conn.execute(
            "SELECT ref_type, ref_record_id, ref_version FROM assessment_refs WHERE assessment_record_id=? AND assessment_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()

    item = _assessment_export_dict(row)
    item["refs"] = [dict(r) for r in ref_rows]

    return_note = None
    if item.get("status") == "returned":
        with db() as conn:
            return_note = _fetch_return_note(conn, "assessment", record_id, version)

    return templates.TemplateResponse(request, "assessment_view.html", {"item": item, "return_note": return_note})


@router.get("/assessments/{record_id}/{version}/edit", response_class=HTMLResponse)
def assessment_edit_form(request: Request, record_id: str, version: int):
    require(request.state.role, "assessment:revise")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)

        ref_rows = conn.execute(
            "SELECT ref_type, ref_record_id, ref_version FROM assessment_refs WHERE assessment_record_id=? AND assessment_version=? ORDER BY order_index",
            (record_id, version),
        ).fetchall()

    item = dict(row)
    lint = _json_load(item.get("lint_json") or "[]") or []
    options = _json_load(item.get("options_json") or "[]") or []

    # Robustness: Handle legacy dict format {"A": "...", ...}
    if isinstance(options, dict):
        normalized = []
        for k in ["A", "B", "C", "D"]:
            normalized.append({"key": k, "text": str(options.get(k) or "")})
        options = normalized

    item["options"] = options

    return templates.TemplateResponse(
        request,
        "assessment_edit.html",
        {"mode": "edit", "item": item, "refs": [dict(r) for r in ref_rows], "lint": lint},
    )


@router.post("/assessments/{record_id}/{version}/save")
def assessment_save(
    request: Request,
    record_id: str,
    version: int,
    stem: str = Form(""),
    claim: str = Form("auto"),
    correct_key: str = Form("A"),
    option_a: str = Form(""),
    option_b: str = Form(""),
    option_c: str = Form(""),
    option_d: str = Form(""),
    rationale: str = Form(""),
    change_note: str = Form(""),
    # authoring helpers (stored in meta_json)
    target_fact: str = Form(""),
    relation_verb: str = Form(""),
    scenario_truth: str = Form(""),
    ref_type: list[str] = Form([]),
    ref_record_id: list[str] = Form([]),
    ref_version: list[int] = Form([]),
):
    """Immutable records: saving creates a NEW VERSION (draft) with required change_note."""
    require(request.state.role, "assessment:revise")
    actor = request.state.user

    note = (change_note or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="change_note is required when creating a new version")

    options = [
        {"key": "A", "text": (option_a or "").strip()},
        {"key": "B", "text": (option_b or "").strip()},
        {"key": "C", "text": (option_c or "").strip()},
        {"key": "D", "text": (option_d or "").strip()},
    ]

    refs: list[dict[str, Any]] = []
    for rt, rid, ver in zip(ref_type or [], ref_record_id or [], ref_version or []):
        rt_n = (rt or "").strip().lower()
        rid_n = (rid or "").strip()
        try:
            ver_i = int(ver)
        except ValueError:
            ver_i = 0
        if rt_n and rid_n and ver_i > 0:
            refs.append({"ref_type": rt_n, "ref_record_id": rid_n, "ref_version": ver_i})

    with db() as conn:
        src = conn.execute(
            "SELECT * FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not src:
            raise HTTPException(404)

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
        lint = _assessment_lint(stem, options, correct_key, claim)

        meta_prev = _json_load((src["meta_json"] if "meta_json" in src.keys() else "{}") or "{}") or {}
        meta_prev.update(
            {
                "target_fact": (target_fact or "").strip(),
                "relation_verb": (relation_verb or "").strip(),
                "scenario_truth": (scenario_truth or "").strip(),
            }
        )

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
                record_id,
                new_v,
                "draft",
                (stem or "").strip(),
                _json_dump(options),
                (correct_key or "A").strip().upper(),
                (rationale or "").strip(),
                (claim or "auto").strip().lower(),
                _json_dump(domains),
                _json_dump(lint),
                _json_dump(refs),
                (src["tags_json"] if "tags_json" in src.keys() else "[]"),
                _json_dump(meta_prev),
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

        # refs table for new version
        for idx, r in enumerate(refs, start=1):
            conn.execute(
                "INSERT INTO assessment_refs(assessment_record_id, assessment_version, order_index, ref_type, ref_record_id, ref_version) VALUES (?,?,?,?,?,?)",
                (record_id, new_v, idx, r["ref_type"], r["ref_record_id"], int(r["ref_version"])),
            )

        audit("assessment", record_id, new_v, "new_version", actor, note=f"from v{version}: {note}", conn=conn)

    return RedirectResponse(url=f"/assessments/{record_id}/{new_v}", status_code=303)


@router.post("/assessments/{record_id}/{version}/submit")
def assessment_submit(request: Request, record_id: str, version: int):
    require(request.state.role, "assessment:submit")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, domains_json FROM assessment_items WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "draft":
            raise HTTPException(409, detail="Only draft assessments can be submitted")

        # Require at least one domain via refs
        doms = _normalize_domains(row["domains_json"])
        if not doms:
            raise HTTPException(status_code=409, detail="Cannot submit assessment: attach it to at least one task/workflow")
        for d in doms:
            if not _user_has_domain(conn, actor, d):
                raise HTTPException(status_code=403, detail=f"Forbidden: you are not authorized for domain '{d}'")

        # Must pass lint errors
        lint = _json_load(
            conn.execute("SELECT lint_json FROM assessment_items WHERE record_id=? AND version=?", (record_id, version)).fetchone()["lint_json"]
        )
        for f in (lint or []):
            if (f.get("level") or "").lower() == "error":
                raise HTTPException(status_code=409, detail="Cannot submit assessment: fix lint errors first")

        conn.execute(
            "UPDATE assessment_items SET status='submitted', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
        audit("assessment", record_id, version, "submit", actor, conn=conn)

    return RedirectResponse(url=f"/assessments/{record_id}/{version}", status_code=303)


@router.post("/assessments/{record_id}/{version}/return")
def assessment_return_for_changes(
    request: Request,
    record_id: str,
    version: int,
    note: str = Form(""),
    severity: str = Form("warning"),
):
    require(request.state.role, "assessment:confirm")
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
            "SELECT status, created_by FROM assessment_items WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "submitted":
            raise HTTPException(status_code=409, detail="Only submitted assessments can be returned")
        if request.state.role == "contributor" and row["created_by"] == actor:
            raise HTTPException(status_code=403, detail="Contributors cannot return content they created.")

        conn.execute(
            "UPDATE assessment_items SET status='returned', updated_at=?, updated_by=? WHERE record_id=? AND version=?",
            (utc_now_iso(), actor, record_id, version),
        )
        audit("assessment", record_id, version, "return_for_changes", actor, note=msg, conn=conn)

    return RedirectResponse(url=f"/assessments/{record_id}/{version}", status_code=303)


@router.post("/assessments/{record_id}/{version}/confirm")
def assessment_confirm(request: Request, record_id: str, version: int):
    require(request.state.role, "assessment:confirm")
    actor = request.state.user

    with db() as conn:
        row = conn.execute(
            "SELECT status, created_by FROM assessment_items WHERE record_id=? AND version=?",
            (record_id, version),
        ).fetchone()
        if not row:
            raise HTTPException(404)
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
        new_badges = audit("assessment", record_id, version, "confirm", actor, conn=conn)

    badge_qs = f"?badges={','.join(new_badges)}" if new_badges else ""
    return RedirectResponse(url=f"/assessments/{record_id}/{version}{badge_qs}", status_code=303)
