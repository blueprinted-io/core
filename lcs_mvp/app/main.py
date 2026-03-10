from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    STATIC_ASSET_VERSION, templates,
)
from .database import init_db
from .auth import can, AuthMiddleware
from .routes import auth as auth_routes, admin as admin_routes
from .routes import home as home_routes
from .routes import tasks as tasks_routes
from .routes import workflows as workflows_routes
from .routes import assessments as assessments_routes
from .routes import imports as imports_routes
from .routes import exports as exports_routes

# ---------------------------------------------------------------------------
# Re-exports for test and external compatibility
# (tests access these as app_main.X after importing lcs_mvp.app.main as app_main)
# ---------------------------------------------------------------------------
from .config import (  # noqa: F401
    DATA_DIR, UPLOADS_DIR, EXPORTS_DIR,
    DB_DEBIAN_PATH, DB_BLANK_PATH, DB_OLD_DEBIAN_PATH, DB_DEMO_LEGACY_PATH,
    DB_PATH_CTX, DB_KEY_CTX, DB_KEY_DEBIAN,
)
from .database import init_db as _init_db_alias  # noqa: F401 (init_db already imported above)

@asynccontextmanager
async def _lifespan(application: FastAPI):
    init_db()
    yield


app = FastAPI(title="Learning Content System MVP", lifespan=_lifespan)


_ERROR_COPY: dict[int, tuple[str, str]] = {
    400: (
        "That didn't work",
        "Something about that request wasn't quite right. "
        "Check what you submitted and try again.",
    ),
    403: (
        "Access denied",
        "You don't have permission to view this. "
        "If you think you should, contact your administrator.",
    ),
    404: (
        "Nothing here",
        "That page or record doesn't exist — it may have been moved, "
        "deleted, or you followed a stale link.",
    ),
    429: (
        "Slow down",
        "Too many failed attempts in a short window. "
        "Wait a few minutes and try again.",
    ),
    500: (
        "Something went wrong",
        "An unexpected error occurred on our end. "
        "The issue has been logged. If it keeps happening, contact your administrator.",
    ),
}
_ERROR_DEFAULT = (
    "Unexpected error",
    "Something went wrong. Please try again or contact your administrator.",
)


def _html_error_response(request: Request, status_code: int, detail: str | None = None):
    title, message = _ERROR_COPY.get(status_code, _ERROR_DEFAULT)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": status_code, "title": title, "message": message, "detail": detail},
        status_code=status_code,
    )


def _import_error_response(request: Request, detail: str, status_code: int):
    """Render the appropriate import form with an error message."""
    path = str(request.url.path)
    if path.startswith("/import/json"):
        template = "import_json.html"
        ctx: dict[str, Any] = {"error": detail}
    else:
        template = "import_pdf.html"
        ctx = {"error": detail}
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    path = str(request.url.path)
    if path.startswith("/import/pdf") or path.startswith("/import/json"):
        return _import_error_response(request, str(exc.detail), exc.status_code)
    return _html_error_response(request, exc.status_code)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return JSONResponse(status_code=500, content={"detail": "An unexpected error occurred."})
    path = str(request.url.path)
    if path.startswith("/import/pdf") or path.startswith("/import/json"):
        return _import_error_response(request, "An unexpected error occurred.", 500)
    return _html_error_response(request, 500)


static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.add_middleware(AuthMiddleware)

templates.env.globals["can"] = can
templates.env.globals["asset_v"] = STATIC_ASSET_VERSION

for _r in (
    auth_routes, admin_routes, home_routes, tasks_routes,
    workflows_routes, assessments_routes, imports_routes, exports_routes,
):
    app.include_router(_r.router)


