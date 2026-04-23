from __future__ import annotations

import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import DB_KEY_BLANK, DB_KEY_COOKIE, DB_KEY_DEBIAN, templates
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
):
    require_admin(request)
    if auto_submit_on_import not in ("true", "false"):
        raise HTTPException(status_code=400, detail="Invalid auto_submit_on_import value")
    actor = request.state.user
    with db() as conn:
        _set_system_setting(conn, "auto_submit_on_import", auto_submit_on_import, actor)
    return RedirectResponse(url="/admin/rules", status_code=303)
