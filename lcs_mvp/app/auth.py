from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import (
    Role,
    DB_KEY_CTX, DB_PATH_CTX,
)
from .database import db, _selected_db_key, _db_path_for_key


# ---------------------------------------------------------------------------
# Auth constants
# ---------------------------------------------------------------------------

DEFAULT_ROLE: Role = "viewer"
SESSION_COOKIE = "lcs_session"

# Lightweight enterprise fields (MVP+) — stored as JSON blobs to avoid migrations churn.
DEFAULT_TAGS: list[str] = []
DEFAULT_META: dict[str, str] = {}

ROLE_ORDER: dict[str, int] = {
    "viewer": 0,
    "contributor": 1,
    "assessment_author": 2,
    "content_publisher": 3,
    "audit": 4,
    "admin": 5,
}


# ---------------------------------------------------------------------------
# Public-path check
# ---------------------------------------------------------------------------

def _is_public_path(path: str) -> bool:
    return path.startswith("/static/") or path.startswith("/avatar/") or path in ("/login", "/login/demo", "/login/password", "/logout", "/db/pick")


# ---------------------------------------------------------------------------
# RBAC matrix
# ---------------------------------------------------------------------------

def can(role: Role, action: str) -> bool:
    """Very small RBAC matrix.

    Design rule (important):
    - Domain entitlements are used to gate *authoring* and *review/confirm* operations.
    - Domain entitlements must NOT gate read-only viewing/browsing. Any authenticated user
      may view records across domains.
    - Delivery/publishing is confirmed-only. Export authorization is role-based, not domain-based.
    - Contributors can both author and confirm, but CANNOT confirm their own content.
      The self-review check (created_by != actor) is enforced in route handlers, not here,
      because can() has no access to the record being acted on.

    Actions:
      - task:create, task:revise, task:submit, task:confirm
      - workflow:create, workflow:revise, workflow:submit, workflow:confirm
      - assessment:create, assessment:revise, assessment:submit, assessment:confirm
      - delivery:view, delivery:export
      - export:library, export:cleanup
      - import:pdf
      - import:json
      - db:switch
      - audit:view
      - task:force_submit, task:force_confirm
      - workflow:force_submit, workflow:force_confirm
    """
    if role == "admin":
        return True

    if action == "audit:view":
        return role in ("audit", "admin")

    if action == "delivery:view":
        return role in ("viewer", "contributor", "assessment_author", "content_publisher")

    if action == "delivery:export":
        return role in ("content_publisher",)

    if action == "export:library":
        return role in ("audit", "admin")

    if action == "export:cleanup":
        return role in ("admin",)

    if action.endswith(":force_confirm") or action.endswith(":force_submit"):
        return role in ("admin",)

    if action.endswith(":confirm"):
        # Contributors can confirm. Self-review prohibition enforced in route handlers.
        return role in ("contributor",)

    if action.startswith("assessment:"):
        # assessment_author creates/revises/submits assessments.
        # contributor can confirm assessments (handled by :confirm above).
        if action.endswith(":submit"):
            return role in ("assessment_author",)
        if action.endswith(":create") or action.endswith(":revise"):
            return role in ("assessment_author",)

    if action.endswith(":submit"):
        return role in ("contributor",)

    if action.endswith(":create"):
        return role in ("contributor",)

    if action.endswith(":revise"):
        return role in ("contributor",)

    if action in ("import:pdf", "import:json"):
        return role in ("contributor",)

    if action == "db:switch":
        return role in ("admin",)

    return False


def require(role: Role, action: str) -> None:
    if not can(role, action):
        raise HTTPException(status_code=403, detail=f"Forbidden: requires permission {action}")


def require_admin(request: Request) -> None:
    if request.state.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden: admin only")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

def _require_login(request: Request) -> bool:
    # Login page should be reachable without a session.
    return not _is_public_path(str(request.url.path))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # DB selection (cookie). Default to demo DB.
        key = _selected_db_key(request)
        request.state.db_key = key
        request.state.db_path = _db_path_for_key(key)
        DB_KEY_CTX.set(key)
        DB_PATH_CTX.set(request.state.db_path)

        # Default unauth state (used by /login rendering).
        request.state.user = ""
        request.state.role = DEFAULT_ROLE

        if _require_login(request):
            token = (request.cookies.get(SESSION_COOKIE) or "").strip()
            if token:
                with db() as conn:
                    row = conn.execute(
                        """
                        SELECT u.username, u.role
                        FROM sessions s
                        JOIN users u ON u.id = s.user_id
                        WHERE s.token=? AND s.revoked_at IS NULL
                          AND (s.expires_at IS NULL OR s.expires_at > datetime('now'))
                        """,
                        (token,),
                    ).fetchone()
                if row:
                    request.state.user = str(row["username"])
                    request.state.role = str(row["role"])  # type: ignore[assignment]

            if not request.state.user:
                accept = (request.headers.get("accept") or "").lower()
                if "text/html" in accept:
                    return RedirectResponse(url="/login", status_code=303)
                # Don't raise inside middleware (can produce noisy exception groups).
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        return await call_next(request)
