from __future__ import annotations

import os
import re
import shutil
import sqlite3
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import DB_KEY_BLANK, DB_KEY_COOKIE, DB_KEY_DEBIAN, DATA_DIR, UPLOADS_DIR, EXPORTS_DIR, templates
from ..config import TASK_IMAGES_DIR
from ..database import (
    db, utc_now_iso,
    _active_domains, _available_db_keys, _create_custom_db_profile,
    _db_profile_label, _hash_password, _list_custom_db_keys,
    _normalize_db_key, _user_id,
    _get_llm_config, _set_system_setting, _get_app_settings,
)
from ..ingestion import _llm_probe
from ..audit import audit
from ..auth import ROLE_ORDER, require, require_admin

router = APIRouter()

_DOMAIN_AGNOSTIC_ROLES = {"viewer", "audit", "content_publisher"}


@router.get("/db", response_class=HTMLResponse)
def db_switch_form(request: Request):
    require(request.state.role, "db:switch")
    profiles = [{"key": k, "label": _db_profile_label(k)} for k in [DB_KEY_DEBIAN, DB_KEY_BLANK] + _list_custom_db_keys()]
    return templates.TemplateResponse(request, "db_switch.html", {"profiles": profiles})


@router.post("/db/switch")
def db_switch(request: Request, db_key: str = Form(DB_KEY_DEBIAN)):
    require(request.state.role, "db:switch")
    key = _normalize_db_key(db_key or DB_KEY_DEBIAN)
    if key not in _available_db_keys():
        raise HTTPException(status_code=400, detail="Invalid db_key")

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(DB_KEY_COOKIE, key, httponly=False, samesite="lax")
    return resp


@router.post("/db/create")
def db_create(request: Request, db_key: str = Form("")):
    require(request.state.role, "db:switch")
    key = (db_key or "").strip().lower()
    if not key:
        raise HTTPException(status_code=400, detail="db_key is required")

    _create_custom_db_profile(key)

    # Switch to it immediately.
    resp = RedirectResponse(url="/db", status_code=303)
    resp.set_cookie(DB_KEY_COOKIE, key, httponly=False, samesite="lax")
    return resp


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, error: str | None = None):
    require_admin(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, COALESCE(demo_password, '') AS demo_password, disabled_at FROM users ORDER BY disabled_at IS NOT NULL, role DESC, username ASC"
        ).fetchall()

        # Attach per-user domains
        users: list[dict[str, Any]] = []
        for r in rows:
            u = dict(r)
            doms = conn.execute(
                "SELECT domain FROM user_domains WHERE user_id=? ORDER BY domain ASC",
                (int(r["id"]),),
            ).fetchall()
            u["domains"] = [str(x["domain"]) for x in doms]
            users.append(u)

    return templates.TemplateResponse(request, "admin/users.html", {"users": users, "error": error})


@router.post("/admin/users/create")
def admin_users_create(request: Request, username: str = Form(""), role: str = Form("viewer"), password: str = Form("")):
    require_admin(request)
    username = (username or "").strip()
    password = password or ""
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    if role not in ROLE_ORDER:
        raise HTTPException(status_code=400, detail="invalid role")
    if not password:
        raise HTTPException(status_code=400, detail="password is required")

    import secrets

    salt = secrets.token_bytes(16).hex()
    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO users(username, role, password_salt_hex, password_hash_hex, demo_password, created_at, created_by)
                VALUES (?,?,?,?,?,?,?)
                """,
                (username, role, salt, _hash_password(password, salt), password, utc_now_iso(), request.state.user),
            )
            audit("user", username, 1, "create", request.state.user, note=f"role={role}", conn=conn)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="username already exists")

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/reset")
def admin_users_reset(request: Request, username: str = Form(""), password: str = Form("")):
    require_admin(request)
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")

    import secrets

    salt = secrets.token_bytes(16).hex()
    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="user not found")
        conn.execute(
            "UPDATE users SET password_salt_hex=?, password_hash_hex=?, demo_password=? WHERE username=?",
            (salt, _hash_password(password, salt), password, username),
        )
        # Revoke sessions
        conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (utc_now_iso(), int(row["id"])))
        audit("user", username, 1, "reset_password", request.state.user, conn=conn)

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/disable")
def admin_users_disable(request: Request, username: str = Form("")):
    require_admin(request)
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")

    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="user not found")
        conn.execute("UPDATE users SET disabled_at=? WHERE username=?", (utc_now_iso(), username))
        conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (utc_now_iso(), int(row["id"])))
        audit("user", username, 1, "disable", request.state.user, conn=conn)

    # If you disabled yourself, you'll be bounced to /login on next request.
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/enable")
def admin_users_enable(request: Request, username: str = Form("")):
    require_admin(request)
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")

    with db() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="user not found")
        conn.execute("UPDATE users SET disabled_at=NULL WHERE username=?", (username,))
        audit("user", username, 1, "enable", request.state.user, conn=conn)

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/delete")
def admin_users_delete(request: Request, username: str = Form("")):
    require_admin(request)
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if username == request.state.user:
        raise HTTPException(status_code=400, detail="cannot delete the current user")

    with db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="user not found")
        uid = int(row["id"])
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        audit("user", username, 1, "delete", request.state.user, conn=conn)

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/domains")
def admin_user_domains_form(request: Request, username: str = Form("")):
    require_admin(request)
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")

    with db() as conn:
        u = conn.execute("SELECT id, username, role FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        if str(u["role"]) in _DOMAIN_AGNOSTIC_ROLES:
            raise HTTPException(status_code=400, detail=f"Role '{u['role']}' has implicit cross-domain visibility and cannot be assigned domains")

        domains = _active_domains(conn)
        selected_rows = conn.execute("SELECT domain FROM user_domains WHERE user_id=?", (int(u["id"]),)).fetchall()
        selected = {str(r["domain"]) for r in selected_rows}

    return templates.TemplateResponse(
        request,
        "admin/user_domains.html",
        {"user": dict(u), "domains": domains, "selected": selected},
    )


@router.post("/admin/users/domains/save")
def admin_user_domains_save(request: Request, username: str = Form(""), domain: list[str] = Form([])):
    require_admin(request)
    username = (username or "").strip()
    selected = sorted({(d or "").strip().lower() for d in (domain or []) if (d or "").strip()})

    with db() as conn:
        u = conn.execute("SELECT id, role FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        if str(u["role"]) in _DOMAIN_AGNOSTIC_ROLES:
            raise HTTPException(status_code=400, detail=f"Role has implicit cross-domain visibility and cannot be assigned domains")

        allowed = set(_active_domains(conn))
        for d in selected:
            if d not in allowed:
                raise HTTPException(status_code=400, detail=f"Invalid domain '{d}'")

        uid = int(u["id"])
        conn.execute("DELETE FROM user_domains WHERE user_id=?", (uid,))
        now = utc_now_iso()
        for d in selected:
            conn.execute(
                "INSERT INTO user_domains(user_id, domain, created_at, created_by) VALUES (?,?,?,?)",
                (uid, d, now, request.state.user),
            )

        audit("user", username, 1, "set_domains", request.state.user, note=",".join(selected), conn=conn)

    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/domains", response_class=HTMLResponse)
def admin_domains(request: Request, error: str | None = None):
    require_admin(request)
    with db() as conn:
        rows = conn.execute("SELECT name, created_at, created_by, disabled_at FROM domains ORDER BY name ASC").fetchall()
    return templates.TemplateResponse(request, "admin/domains.html", {"domains": [dict(r) for r in rows], "error": error})


@router.post("/admin/domains/create")
def admin_domains_create(request: Request, name: str = Form("")):
    require_admin(request)
    name_norm = re.sub(r"\s+", "-", (name or "").strip().lower())
    if not name_norm:
        raise HTTPException(status_code=400, detail="name required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name_norm):
        raise HTTPException(status_code=400, detail="invalid domain name")

    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO domains(name, created_at, created_by) VALUES (?,?,?)",
                (name_norm, utc_now_iso(), request.state.user),
            )
            audit("domain", name_norm, 1, "create", request.state.user, conn=conn)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="domain already exists")

    return RedirectResponse(url="/admin/domains", status_code=303)


@router.post("/admin/domains/disable")
def admin_domains_disable(request: Request, name: str = Form("")):
    require_admin(request)
    name_norm = (name or "").strip().lower()
    with db() as conn:
        row = conn.execute("SELECT 1 FROM domains WHERE name=?", (name_norm,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="domain not found")
        conn.execute("UPDATE domains SET disabled_at=? WHERE name=?", (utc_now_iso(), name_norm))
        audit("domain", name_norm, 1, "disable", request.state.user, conn=conn)

    return RedirectResponse(url="/admin/domains", status_code=303)


@router.post("/admin/domains/enable")
def admin_domains_enable(request: Request, name: str = Form("")):
    require_admin(request)
    name_norm = (name or "").strip().lower()
    with db() as conn:
        row = conn.execute("SELECT 1 FROM domains WHERE name=?", (name_norm,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="domain not found")
        conn.execute("UPDATE domains SET disabled_at=NULL WHERE name=?", (name_norm,))
        audit("domain", name_norm, 1, "enable", request.state.user, conn=conn)

    return RedirectResponse(url="/admin/domains", status_code=303)


@router.post("/admin/domains/delete")
def admin_domains_delete(request: Request, name: str = Form("")):
    require_admin(request)
    name_norm = (name or "").strip().lower()
    with db() as conn:
        row = conn.execute("SELECT 1 FROM domains WHERE name=?", (name_norm,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="domain not found")
        try:
            conn.execute("DELETE FROM domains WHERE name=?", (name_norm,))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="domain is referenced by user entitlements; disable it instead")
        audit("domain", name_norm, 1, "delete", request.state.user, conn=conn)

    return RedirectResponse(url="/admin/domains", status_code=303)


# ---------------------------------------------------------------------------
# LLM provider config
# ---------------------------------------------------------------------------

@router.get("/admin/llm", response_class=HTMLResponse)
def admin_llm(request: Request):
    require_admin(request)
    with db() as conn:
        cfg = _get_llm_config(conn)
    api_key_set = bool((cfg.get("llm_api_key") or "").strip())
    return templates.TemplateResponse(request, "admin/llm.html", {"cfg": cfg, "api_key_set": api_key_set})


@router.post("/admin/llm/save")
def admin_llm_save(
    request: Request,
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    llm_model: str = Form(""),
    llm_timeout_seconds: str = Form("120"),
    llm_max_tokens: str = Form("2000"),
    llm_max_tasks_per_chunk: str = Form("5"),
    llm_max_chunks_per_run: str = Form("8"),
):
    require_admin(request)
    actor = request.state.user
    with db() as conn:
        _set_system_setting(conn, "llm_base_url", llm_base_url.strip(), actor)
        _set_system_setting(conn, "llm_model", llm_model.strip(), actor)
        _set_system_setting(conn, "llm_timeout_seconds", llm_timeout_seconds.strip() or "120", actor)
        _set_system_setting(conn, "llm_max_tokens", llm_max_tokens.strip() or "2000", actor)
        _set_system_setting(conn, "llm_max_tasks_per_chunk", llm_max_tasks_per_chunk.strip() or "5", actor)
        _set_system_setting(conn, "llm_max_chunks_per_run", llm_max_chunks_per_run.strip() or "8", actor)
        if llm_api_key.strip():
            _set_system_setting(conn, "llm_api_key", llm_api_key.strip(), actor)
    return RedirectResponse(url="/admin/llm", status_code=303)


@router.get("/admin/llm/probe")
def admin_llm_probe(request: Request, base_url: str = "", api_key: str = ""):
    """Probe the LLM endpoint. Accepts optional base_url/api_key query params
    so the admin can test values before saving. Falls back to saved config."""
    from fastapi.responses import JSONResponse
    require_admin(request)
    with db() as conn:
        cfg = _get_llm_config(conn)
    bu = base_url.strip() or cfg["llm_base_url"]
    key = api_key.strip() or cfg["llm_api_key"]
    result = _llm_probe(bu, key)
    return JSONResponse(result)


@router.get("/admin/llm/models")
def admin_llm_models(request: Request, base_url: str = "", api_key: str = ""):
    """Fetch available model IDs from the configured LLM endpoint.

    base_url and api_key can be passed as query params (pre-save preview).
    If api_key is blank, falls back to the saved key so the admin doesn't
    have to re-enter a key they've already stored.
    """
    from fastapi.responses import JSONResponse
    import httpx as _httpx
    require_admin(request)
    with db() as conn:
        cfg = _get_llm_config(conn)

    bu = (base_url.strip() or cfg["llm_base_url"]).rstrip("/")
    key = api_key.strip() or cfg["llm_api_key"]
    if not bu:
        return JSONResponse({"ok": False, "models": [], "detail": "No base URL provided."})

    from ..ingestion import _llm_candidate_urls
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with _httpx.Client(timeout=_httpx.Timeout(6.0, connect=3.0), verify=False) as client:
            r = None
            for url in _llm_candidate_urls(bu, "models"):
                resp = client.get(url, headers=headers)
                if resp.status_code < 400:
                    r = resp
                    break
            if r is None:
                return JSONResponse({"ok": False, "models": [], "detail": f"No models endpoint found at {bu}"})
            data = r.json()
            # Handle multiple response shapes:
            # OpenAI: {"data": [{"id": "..."}, ...]}
            # Ollama:  {"models": [{"name": "..."}, ...]}
            # Plain list: [{"id": "..."}, ...] or ["model-name", ...]
            model_list: list = []
            if isinstance(data, dict):
                if "data" in data:
                    model_list = data["data"]
                elif "models" in data:
                    model_list = data["models"]
            elif isinstance(data, list):
                model_list = data
            models = sorted(set(
                m.get("id") or m.get("name") or ""
                if isinstance(m, dict) else str(m)
                for m in model_list
                if (isinstance(m, dict) and (m.get("id") or m.get("name"))) or isinstance(m, str)
            ))
            if not models:
                return JSONResponse({"ok": False, "models": [], "detail": "Connected but no models returned"})
            return JSONResponse({"ok": True, "models": models})
    except Exception as e:
        return JSONResponse({"ok": False, "models": [], "detail": str(e)})


# ---------------------------------------------------------------------------
# Operational rules panel
# ---------------------------------------------------------------------------

@router.get("/admin/rules", response_class=HTMLResponse)
def admin_rules(request: Request):
    require_admin(request)
    with db() as conn:
        settings = _get_app_settings(conn)
    return templates.TemplateResponse(request, "admin/rules.html", {"settings": settings})


@router.post("/admin/rules/save")
def admin_rules_save(
    request: Request,
    auto_submit_on_import: str = Form("false"),
    import_select_all: str = Form("false"),
):
    require_admin(request)
    if auto_submit_on_import not in ("true", "false"):
        raise HTTPException(status_code=400, detail="Invalid auto_submit_on_import value")
    if import_select_all not in ("true", "false"):
        raise HTTPException(status_code=400, detail="Invalid import_select_all value")
    actor = request.state.user
    with db() as conn:
        _set_system_setting(conn, "auto_submit_on_import", auto_submit_on_import, actor)
        _set_system_setting(conn, "import_select_all", import_select_all, actor)
    return RedirectResponse(url="/admin/rules", status_code=303)


# ---------------------------------------------------------------------------
# Application log viewer
# ---------------------------------------------------------------------------

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _read_log_tail(log_path: str, lines: int, level_filter: str) -> list[dict]:
    """Read the last `lines` entries from the log file, optionally filtered by level."""
    _level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    min_level = _level_order.get(level_filter.upper(), 0)

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    except FileNotFoundError:
        return []

    entries = []
    for raw in reversed(raw_lines):
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split(" | ", 3)
        if len(parts) == 4:
            ts, lvl, name, msg = parts
            lvl = lvl.strip()
            line_level = _level_order.get(lvl, 0)
            if line_level < min_level:
                continue
            entries.append({"ts": ts, "level": lvl, "logger": name, "message": msg})
        else:
            # continuation line (e.g. traceback) — attach to previous entry
            if entries:
                entries[-1]["message"] += "\n" + line
            continue
        if len(entries) >= lines:
            break

    return entries  # already newest-first


# ---------------------------------------------------------------------------
# Admin task edit (in-place metadata UPDATE, no new version)
# ---------------------------------------------------------------------------

@router.get("/admin/tasks/{record_id}/{version}/edit", response_class=HTMLResponse)
def admin_task_edit_form(request: Request, record_id: str, version: int):
    require_admin(request)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        domains = _active_domains(conn)
    return templates.TemplateResponse(
        request,
        "admin/task_edit.html",
        {"task": dict(row), "domains": domains},
    )


@router.post("/admin/tasks/{record_id}/{version}/edit")
def admin_task_edit_save(
    request: Request,
    record_id: str,
    version: int,
    title: str = Form(...),
    outcome: str = Form(...),
    procedure_name: str = Form(""),
    domain: str = Form(""),
    software_name: str = Form(""),
    software_version: str = Form(""),
    needs_review_flag: bool = Form(False),
    needs_review_note: str = Form(""),
    irreversible_flag: bool = Form(False),
):
    require_admin(request)
    actor = request.state.user
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE record_id=? AND version=?", (record_id, version)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute(
            """
            UPDATE tasks SET
                title=?, outcome=?, procedure_name=?, domain=?,
                software_name=?, software_version=?,
                needs_review_flag=?, needs_review_note=?,
                irreversible_flag=?,
                updated_at=?, updated_by=?
            WHERE record_id=? AND version=?
            """,
            (
                title.strip(),
                outcome.strip(),
                procedure_name.strip(),
                (domain or "").strip().lower(),
                software_name.strip() or None,
                software_version.strip() or None,
                1 if needs_review_flag else 0,
                needs_review_note.strip() or None,
                1 if irreversible_flag else 0,
                utc_now_iso(),
                actor,
                record_id,
                version,
            ),
        )
    audit("task", record_id, version, "admin_edit", actor, note="in-place metadata edit")
    return RedirectResponse(url=f"/tasks/{record_id}/{version}", status_code=303)


# ---------------------------------------------------------------------------
# Admin bulk delete
# ---------------------------------------------------------------------------

@router.post("/admin/tasks/bulk-delete")
def admin_tasks_bulk_delete(
    request: Request,
    task_ids: list[str] = Form([]),
    confirm_text: str = Form(""),
):
    require_admin(request)
    if confirm_text != "DELETE":
        raise HTTPException(status_code=400, detail="Confirmation text must be exactly 'DELETE'")
    if not task_ids:
        raise HTTPException(status_code=400, detail="No tasks selected")

    actor = request.state.user
    deleted = 0
    with db() as conn:
        for tid in task_ids:
            parts = tid.split("::", 1)
            if len(parts) != 2:
                continue
            rid, ver_str = parts
            try:
                ver = int(ver_str)
            except ValueError:
                continue
            conn.execute(
                "DELETE FROM workflow_task_refs WHERE task_record_id=? AND task_version=?",
                (rid, ver),
            )
            conn.execute(
                "DELETE FROM tasks WHERE record_id=? AND version=?",
                (rid, ver),
            )
            audit("task", rid, ver, "delete", actor, note="admin bulk delete", conn=conn)
            deleted += 1

    return RedirectResponse(url=f"/tasks?deleted={deleted}", status_code=303)


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_logs(
    request: Request,
    lines: int = 200,
    level: str = "WARNING",
):
    require_admin(request)
    lines = max(10, min(lines, 2000))
    if level.upper() not in _LOG_LEVELS:
        level = "WARNING"
    log_path = os.path.join(DATA_DIR, "app.log")
    entries = _read_log_tail(log_path, lines, level)
    return templates.TemplateResponse(
        request,
        "admin/logs.html",
        {
            "entries": entries,
            "lines": lines,
            "level": level.upper(),
            "log_path": log_path,
            "log_exists": os.path.exists(log_path),
        },
    )


# ---------------------------------------------------------------------------
# System status / disk space
# ---------------------------------------------------------------------------

def _dir_size(path: str) -> tuple[int, int]:
    """Return (total_bytes, file_count) for a directory tree. Returns (0, 0) if missing."""
    total = count = 0
    if not os.path.isdir(path):
        return 0, 0
    for dirpath, _, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
                count += 1
            except OSError:
                pass
    return total, count


def _fmt_bytes(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.2f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request):
    require_admin(request)

    # Partition stats
    try:
        usage = shutil.disk_usage(DATA_DIR)
        disk_total = usage.total
        disk_used = usage.used
        disk_free = usage.free
        disk_pct = round(disk_used / disk_total * 100, 1) if disk_total else 0
        disk_warn = disk_free < 1_073_741_824 or disk_pct > 90  # < 1 GB or > 90%
    except Exception:
        disk_total = disk_used = disk_free = 0
        disk_pct = 0
        disk_warn = False

    # Per-directory breakdown
    db_bytes = db_count = 0
    for fname in os.listdir(DATA_DIR) if os.path.isdir(DATA_DIR) else []:
        if fname.endswith(".db"):
            try:
                db_bytes += os.path.getsize(os.path.join(DATA_DIR, fname))
                db_count += 1
            except OSError:
                pass

    log_bytes = 0
    for fname in os.listdir(DATA_DIR) if os.path.isdir(DATA_DIR) else []:
        if fname.startswith("app.log"):
            try:
                log_bytes += os.path.getsize(os.path.join(DATA_DIR, fname))
            except OSError:
                pass

    uploads_bytes, uploads_count = _dir_size(UPLOADS_DIR)
    exports_bytes, exports_count = _dir_size(EXPORTS_DIR)
    images_bytes, images_count = _dir_size(TASK_IMAGES_DIR)

    dirs = [
        {"name": "Databases (.db)", "path": DATA_DIR + "/*.db", "size": _fmt_bytes(db_bytes), "count": db_count},
        {"name": "Uploaded PDFs", "path": UPLOADS_DIR, "size": _fmt_bytes(uploads_bytes), "count": uploads_count},
        {"name": "Exports", "path": EXPORTS_DIR, "size": _fmt_bytes(exports_bytes), "count": exports_count},
        {"name": "Task images", "path": TASK_IMAGES_DIR, "size": _fmt_bytes(images_bytes), "count": images_count},
        {"name": "App logs", "path": DATA_DIR + "/app.log*", "size": _fmt_bytes(log_bytes), "count": None},
    ]

    # DB record counts
    record_counts: list[dict[str, Any]] = []
    try:
        with db() as conn:
            for label, table in [("Tasks", "tasks"), ("Workflows", "workflows"), ("Ingestions", "ingestions")]:
                n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                record_counts.append({"label": label, "count": n})
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "admin/system.html",
        {
            "disk_total": _fmt_bytes(disk_total),
            "disk_used": _fmt_bytes(disk_used),
            "disk_free": _fmt_bytes(disk_free),
            "disk_pct": disk_pct,
            "disk_warn": disk_warn,
            "dirs": dirs,
            "record_counts": record_counts,
        },
    )
