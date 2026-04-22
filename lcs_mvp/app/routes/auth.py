from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..config import (
    DB_KEY_BLANK, DB_KEY_COOKIE, DB_KEY_DEBIAN, DB_KEY_PRODUCTION, DB_PATH_CTX, UPLOADS_DIR, templates,
)
from ..auth import SESSION_COOKIE
from ..database import (
    db, utc_now_iso, session_expires_iso,
    _active_domains, _available_db_keys, _db_profile_label, _db_path_for_key, _list_custom_db_keys,
    _normalize_db_key, _user_id, _user_domains,
    _verify_password, _hash_password, _new_session_token,
)
from ..audit import audit
from ..achievements import get_user_achievements

router = APIRouter()

# ---------------------------------------------------------------------------
# Login rate limiting (in-memory, per username)
# ---------------------------------------------------------------------------

_RATE_WINDOW = 300   # seconds (5 minutes)
_RATE_MAX    = 5     # max failures before 429

_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()


def _check_rate_limit(username: str) -> None:
    now = time.monotonic()
    with _login_failures_lock:
        recent = [t for t in _login_failures.get(username, []) if now - t < _RATE_WINDOW]
        _login_failures[username] = recent
        if len(recent) >= _RATE_MAX:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Please wait a few minutes before trying again.",
            )


def _record_failure(username: str) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(username, []).append(time.monotonic())


def _clear_failures(username: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(username, None)


# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_splash(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/login/demo", response_class=HTMLResponse)
def login_demo(request: Request):
    # Never serve production data on the demo page.
    if request.state.db_key == DB_KEY_PRODUCTION:
        DB_PATH_CTX.set(_db_path_for_key(DB_KEY_DEBIAN))
    with db() as conn:
        users = conn.execute(
            "SELECT id, username, role, COALESCE(demo_password, '') AS demo_password FROM users WHERE disabled_at IS NULL ORDER BY role DESC, username ASC"
        ).fetchall()
        users_with_domains: list[dict] = []
        for u in users:
            user_dict = dict(u)
            if user_dict["role"] == "admin":
                user_dict["domains"] = _active_domains(conn)
            else:
                user_dict["domains"] = _user_domains(conn, user_dict["username"])
            users_with_domains.append(user_dict)
    custom = _list_custom_db_keys()
    profiles = [{"key": k, "label": _db_profile_label(k)} for k in [DB_KEY_DEBIAN, DB_KEY_BLANK] + custom]
    return templates.TemplateResponse(
        request,
        "login_demo.html",
        {"users": users_with_domains, "profiles": profiles, "db_key": request.state.db_key},
    )


@router.get("/login/password", response_class=HTMLResponse)
def login_password(request: Request):
    resp = templates.TemplateResponse(request, "login_password.html", {})
    resp.set_cookie(DB_KEY_COOKIE, DB_KEY_PRODUCTION, httponly=False, samesite="lax")
    return resp


@router.post("/login")
def login_run(request: Request, username: str = Form(""), password: str = Form(""), target_db: str = Form("")):
    username = (username or "").strip()
    password = password or ""
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    if target_db == DB_KEY_PRODUCTION:
        DB_PATH_CTX.set(_db_path_for_key(DB_KEY_PRODUCTION))

    _check_rate_limit(username)

    # Avoid raising inside `with db()` — sqlite3 context manager rolls back on
    # exception, which would discard the audit write. Collect outcome first.
    failure_note: str | None = None
    token: str | None = None

    with db() as conn:
        u = conn.execute(
            """
            SELECT id, username, role, password_salt_hex, password_hash_hex
            FROM users
            WHERE username=? AND disabled_at IS NULL
            """,
            (username,),
        ).fetchone()

        if not u:
            failure_note = "user not found"
        elif not _verify_password(password, str(u["password_salt_hex"]), str(u["password_hash_hex"])):
            failure_note = "invalid password"

        if failure_note:
            audit("auth", username, 0, "login_failure", username, note=failure_note, conn=conn)
        else:
            _clear_failures(username)
            token = _new_session_token()
            conn.execute(
                "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (token, int(u["id"]), utc_now_iso(), session_expires_iso()),  # type: ignore[index]
            )
            audit("auth", username, 0, "login_success", username, note="session created", conn=conn)

    if failure_note:
        _record_failure(username)
        raise HTTPException(status_code=403, detail="Invalid credentials")

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    if target_db == DB_KEY_PRODUCTION:
        resp.set_cookie(DB_KEY_COOKIE, DB_KEY_PRODUCTION, httponly=False, samesite="lax")
    return resp


@router.post("/logout")
def logout(request: Request):
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if token:
        with db() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE token=? AND revoked_at IS NULL", (utc_now_iso(), token))

    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.post("/db/pick")
def db_pick(request: Request, db_key: str = Form(DB_KEY_DEBIAN)):
    """Unauthenticated DB profile picker for demo convenience.

    This is intentionally only a cookie setter; it does not create profiles.
    """
    key = _normalize_db_key(db_key or DB_KEY_DEBIAN)
    if key not in _available_db_keys():
        raise HTTPException(status_code=400, detail="Invalid db_key")

    resp = RedirectResponse(url="/login", status_code=303)
    resp.set_cookie(DB_KEY_COOKIE, key, httponly=False, samesite="lax")
    return resp


@router.get("/profile", response_class=HTMLResponse)
def profile_view(request: Request, msg: str | None = None):
    actor = request.state.user
    with db() as conn:
        u = conn.execute(
            "SELECT username, COALESCE(display_name,'') AS display_name, COALESCE(bio,'') AS bio, COALESCE(avatar_path,'') AS avatar_path FROM users WHERE username=? AND disabled_at IS NULL",
            (actor,),
        ).fetchone()
        if not u:
            raise HTTPException(404)

        domains = _active_domains(conn)
        uid = _user_id(conn, actor)
        selected_rows = conn.execute("SELECT domain FROM user_domains WHERE user_id=?", (uid,)).fetchall() if uid else []
        selected = {str(r["domain"]) for r in selected_rows}
        user_badges = get_user_achievements(conn, actor)

    return templates.TemplateResponse(
        request,
        "profile.html",
        {"user": dict(u), "domains": domains, "selected": selected, "msg": msg, "role": request.state.role, "badges": user_badges},
    )


def _avatar_file_response(avatar_path: str, *, no_store: bool) -> FileResponse:
    p = str(avatar_path or "").strip()
    if not p:
        raise HTTPException(status_code=404, detail="No avatar")

    base = Path(UPLOADS_DIR).resolve()
    f = Path(p)
    try:
        f_abs = f.resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid avatar path")

    if base not in f_abs.parents:
        raise HTTPException(status_code=400, detail="Invalid avatar path")
    if not f_abs.exists():
        raise HTTPException(status_code=404, detail="Avatar missing")

    mt = "application/octet-stream"
    low = f_abs.name.lower()
    if low.endswith(".png"):
        mt = "image/png"
    elif low.endswith(".jpg") or low.endswith(".jpeg"):
        mt = "image/jpeg"
    elif low.endswith(".webp"):
        mt = "image/webp"

    headers = {
        "Pragma": "no-cache",
    }
    if no_store:
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        headers["Expires"] = "0"
    else:
        headers["Cache-Control"] = "no-cache, must-revalidate"

    return FileResponse(str(f_abs), media_type=mt, headers=headers)


@router.get("/profile/avatar")
def profile_avatar(request: Request):
    actor = request.state.user
    with db() as conn:
        row = conn.execute("SELECT COALESCE(avatar_path,'') AS avatar_path FROM users WHERE username=? AND disabled_at IS NULL", (actor,)).fetchone()
        if not row:
            raise HTTPException(404)
        p = str(row["avatar_path"] or "")
    return _avatar_file_response(p, no_store=True)


@router.get("/avatar/{username}")
def public_avatar(username: str):
    """Public avatar endpoint for login page (no auth required)."""
    with db() as conn:
        row = conn.execute("SELECT COALESCE(avatar_path,'') AS avatar_path FROM users WHERE username=? AND disabled_at IS NULL", (username,)).fetchone()
        if not row:
            raise HTTPException(404)
        p = str(row["avatar_path"] or "")
    return _avatar_file_response(p, no_store=False)


@router.post("/profile/save")
def profile_save(
    request: Request,
    display_name: str = Form(""),
    bio: str = Form(""),
    avatar: UploadFile | None = File(None),
):
    actor = request.state.user
    dn = (display_name or "").strip()
    b = (bio or "").strip()

    avatar_path = None
    if avatar is not None and avatar.filename:
        # Accept small set of image types
        ct = (avatar.content_type or "").lower()
        if ct not in ("image/png", "image/jpeg", "image/webp"):
            raise HTTPException(status_code=400, detail="Avatar must be PNG, JPG, or WebP")

        ext = ".png" if ct == "image/png" else (".webp" if ct == "image/webp" else ".jpg")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_user = re.sub(r"[^a-zA-Z0-9_-]+", "_", actor)[:48]
        out_dir = Path(UPLOADS_DIR) / "avatars"
        out_dir.mkdir(parents=True, exist_ok=True)
        nonce = uuid4().hex[:10]
        out_path = (out_dir / f"avatar__{safe_user}__{ts}__{nonce}{ext}").resolve()

        data = avatar.file.read()
        if len(data) > 2_000_000:
            raise HTTPException(status_code=400, detail="Avatar too large (max 2MB)")
        out_path.write_bytes(data)
        avatar_path = str(out_path)

    with db() as conn:
        if avatar_path is None:
            conn.execute(
                "UPDATE users SET display_name=?, bio=? WHERE username=? AND disabled_at IS NULL",
                (dn, b, actor),
            )
        else:
            conn.execute(
                "UPDATE users SET display_name=?, bio=?, avatar_path=? WHERE username=? AND disabled_at IS NULL",
                (dn, b, avatar_path, actor),
            )
        audit("user", actor, 1, "profile_update", actor, note="profile fields updated", conn=conn)

    return RedirectResponse(url="/profile?msg=saved", status_code=303)


@router.post("/profile/domains/save")
def profile_domains_save(request: Request, domain: list[str] = Form([])):
    raise HTTPException(
        status_code=403,
        detail="Domain entitlements are admin-managed. Use the admin users page.",
    )


@router.post("/profile/password")
def profile_password_change(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    import secrets as _secrets
    actor = request.state.user
    new_pw = (new_password or "").strip()
    confirm_pw = (confirm_password or "").strip()

    if new_pw != confirm_pw:
        return RedirectResponse(url="/profile?msg=pw_mismatch", status_code=303)
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    with db() as conn:
        u = conn.execute(
            "SELECT id, password_salt_hex, password_hash_hex FROM users WHERE username=? AND disabled_at IS NULL",
            (actor,),
        ).fetchone()
        if not u:
            raise HTTPException(404)
        if not _verify_password(current_password or "", str(u["password_salt_hex"]), str(u["password_hash_hex"])):
            return RedirectResponse(url="/profile?msg=pw_wrong", status_code=303)

        new_salt = _secrets.token_bytes(16).hex()
        new_hash = _hash_password(new_pw, new_salt)
        conn.execute(
            "UPDATE users SET password_salt_hex=?, password_hash_hex=?, demo_password=NULL WHERE id=?",
            (new_salt, new_hash, int(u["id"])),
        )
        # Revoke all other sessions so the old password can't be used via cached cookies
        current_token = (request.cookies.get("lcs_session") or "").strip()
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND token != ?",
            (utc_now_iso(), int(u["id"]), current_token),
        )
        audit("user", actor, 1, "password_change", actor, note="password changed by user", conn=conn)

    return RedirectResponse(url="/profile?msg=pw_saved", status_code=303)


@router.get("/profile/badges-meta")
def badges_meta(codes: str = ""):
    """Return badge catalog metadata for a comma-separated list of codes.

    Used by the achievement toast JS. Auth required via middleware.
    """
    code_list = [c.strip() for c in (codes or "").split(",") if c.strip()]
    if not code_list:
        return []
    qmarks = ",".join(["?"] * len(code_list))
    with db() as conn:
        rows = conn.execute(
            f"SELECT code, name, description, icon FROM achievements WHERE code IN ({qmarks}) AND enabled=1",
            code_list,
        ).fetchall()
    return [{"code": r["code"], "name": r["name"], "description": r["description"], "icon": r["icon"]} for r in rows]
